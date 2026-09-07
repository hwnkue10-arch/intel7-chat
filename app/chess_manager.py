"""In-memory Chess Room and Multiplayer Game State Manager."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Dict, List, Optional, Set
from fastapi import WebSocket

logger = logging.getLogger("bamboochat.chess")

START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

class ChessManager:
    def __init__(self) -> None:
        self.rooms: Dict[str, dict] = {}
        self.lobby_sockets: Set[WebSocket] = set()
        self.room_sockets: Dict[str, Set[WebSocket]] = {}
        self.socket_user: Dict[WebSocket, dict] = {}
        self.socket_room: Dict[WebSocket, str] = {}

    def register_client(self, ws: WebSocket, user: dict) -> None:
        self.socket_user[ws] = user
        self.lobby_sockets.add(ws)

    async def unregister_client(self, ws: WebSocket) -> None:
        user = self.socket_user.pop(ws, None)
        self.lobby_sockets.discard(ws)
        room_id = self.socket_room.pop(ws, None)
        if room_id and room_id in self.room_sockets:
            self.room_sockets[room_id].discard(ws)
            if user:
                await self.handle_disconnect_from_room(user, room_id)

    def get_lobby_summary(self) -> List[dict]:
        summary = []
        for r_id, room in self.rooms.items():
            summary.append({
                "id": r_id,
                "title": room["title"],
                "created_by": room["created_by"],
                "time_minutes": room["time_minutes"],
                "white": room["white"]["name"] if room["white"] else None,
                "black": room["black"]["name"] if room["black"] else None,
                "spectator_count": len(room["spectators"]),
                "game_started": room["game_started"],
                "result": room["result"],
                "created_at": room["created_at"],
            })
        summary.sort(key=lambda x: x["created_at"], reverse=True)
        return summary

    async def broadcast_lobby(self) -> None:
        payload = json.dumps({"type": "lobby_update", "rooms": self.get_lobby_summary()}, ensure_ascii=False)
        stale = []
        for ws in list(self.lobby_sockets):
            try:
                await ws.send_text(payload)
            except Exception:
                stale.append(ws)
        for ws in stale:
            self.lobby_sockets.discard(ws)

    async def broadcast_room(self, room_id: str) -> None:
        room = self.rooms.get(room_id)
        if not room:
            return
        now = time.time()
        clock = dict(room["clock"])
        if room["game_started"] and not room["result"] and room["white"] and room["black"] and not room["draw_offer"]:
            delta = now - clock["last_tick_at"]
            turn = room["active_turn"]
            if turn == "w":
                clock["w_remain"] = max(0.0, clock["w_remain"] - delta)
            else:
                clock["b_remain"] = max(0.0, clock["b_remain"] - delta)

        payload = json.dumps({
            "type": "room_state",
            "room": {
                "id": room["id"],
                "title": room["title"],
                "time_minutes": room["time_minutes"],
                "white": room["white"],
                "black": room["black"],
                "spectators": room["spectators"],
                "match_queue": room["match_queue"],
                "fen": room["fen"],
                "active_turn": room["active_turn"],
                "move_history": room["move_history"],
                "last_from": room["last_from"],
                "last_to": room["last_to"],
                "last_flags": room["last_flags"],
                "game_started": room["game_started"],
                "result": room["result"],
                "draw_offer": room["draw_offer"],
                "clock": {
                    "w_remain": round(clock["w_remain"], 1),
                    "b_remain": round(clock["b_remain"], 1),
                    "last_tick_at": now,
                },
                "stats": room["stats"],
            }
        }, ensure_ascii=False)

        stale = []
        for ws in list(self.room_sockets.get(room_id, set())):
            try:
                await ws.send_text(payload)
            except Exception:
                stale.append(ws)
        for ws in stale:
            self.room_sockets[room_id].discard(ws)

    async def create_room(self, ws: WebSocket, user: dict, title: str, time_minutes: int) -> dict:
        room_id = f"chess-{uuid.uuid4().hex[:6]}"
        title = (title or f"{user.get('display_name') or user['username']}의 체스방").strip()[:30]
        try:
            time_minutes = max(1, min(180, int(time_minutes or 10)))
        except (ValueError, TypeError):
            time_minutes = 10

        player = {
            "id": user["id"],
            "username": user["username"],
            "name": user.get("display_name") or user["username"]
        }

        room = {
            "id": room_id,
            "title": title,
            "created_by": player["name"],
            "time_minutes": time_minutes,
            "white": player,
            "black": None,
            "spectators": [],
            "match_queue": [],
            "fen": START_FEN,
            "active_turn": "w",
            "move_history": [{"fen": START_FEN, "move": "Start"}],
            "last_from": None,
            "last_to": None,
            "last_flags": None,
            "game_started": False,
            "result": None,
            "draw_offer": None,
            "draw_request_used_this_turn": False,
            "clock": {
                "w_remain": float(time_minutes * 60),
                "b_remain": float(time_minutes * 60),
                "last_tick_at": time.time(),
            },
            "stats": {},
            "created_at": time.time(),
        }

        self.rooms[room_id] = room
        if room_id not in self.room_sockets:
            self.room_sockets[room_id] = set()

        self.lobby_sockets.discard(ws)
        self.room_sockets[room_id].add(ws)
        self.socket_room[ws] = room_id

        await self.broadcast_lobby()
        await self.broadcast_room(room_id)
        return room

    async def join_room(self, ws: WebSocket, user: dict, room_id: str, role_pref: Optional[str] = None) -> Optional[dict]:
        room = self.rooms.get(room_id)
        if not room:
            await ws.send_text(json.dumps({"type": "error", "message": "방이 존재하지 않습니다."}, ensure_ascii=False))
            return None

        player = {
            "id": user["id"],
            "username": user["username"],
            "name": user.get("display_name") or user["username"]
        }

        if room["white"] and room["white"]["id"] == user["id"]:
            room["white"] = player
        elif room["black"] and room["black"]["id"] == user["id"]:
            room["black"] = player
        else:
            room["spectators"] = [s for s in room["spectators"] if s["id"] != user["id"]]
            if role_pref == "w" and not room["white"] and not room["game_started"]:
                room["white"] = player
            elif role_pref == "b" and not room["black"] and not room["game_started"]:
                room["black"] = player
            elif not room["white"] and not room["game_started"]:
                room["white"] = player
            elif not room["black"] and not room["game_started"]:
                room["black"] = player
            else:
                room["spectators"].append(player)

        if room_id not in self.room_sockets:
            self.room_sockets[room_id] = set()
        self.lobby_sockets.discard(ws)
        self.room_sockets[room_id].add(ws)
        self.socket_room[ws] = room_id

        await self.broadcast_room(room_id)
        await self.broadcast_lobby()
        return room

    async def leave_room(self, ws: WebSocket, user: dict, room_id: str) -> None:
        self.socket_room.pop(ws, None)
        if room_id in self.room_sockets:
            self.room_sockets[room_id].discard(ws)
        self.lobby_sockets.add(ws)

        await self.handle_disconnect_from_room(user, room_id)
        await self.broadcast_lobby()

    async def handle_disconnect_from_room(self, user: dict, room_id: str) -> None:
        room = self.rooms.get(room_id)
        if not room:
            return

        user_id = user["id"]
        has_other_connection = False
        for client_ws in self.room_sockets.get(room_id, set()):
            u = self.socket_user.get(client_ws)
            if u and u["id"] == user_id:
                has_other_connection = True
                break

        if has_other_connection:
            return

        if room["game_started"] and not room["result"]:
            if room["white"] and room["white"]["id"] == user_id:
                room["white"] = None
                room["result"] = {"type": "disconnect", "winner": "b", "desc": "백 플레이어 이탈로 인한 흑 부전승"}
                self._record_game_stats(room, "b")
            elif room["black"] and room["black"]["id"] == user_id:
                room["black"] = None
                room["result"] = {"type": "disconnect", "winner": "w", "desc": "흑 플레이어 이탈로 인한 백 부전승"}
                self._record_game_stats(room, "w")
        else:
            if room["white"] and room["white"]["id"] == user_id:
                room["white"] = None
            if room["black"] and room["black"]["id"] == user_id:
                room["black"] = None

        room["spectators"] = [s for s in room["spectators"] if s["id"] != user_id]
        room["match_queue"] = [m for m in room["match_queue"] if m["id"] != user_id]

        if not room["white"] and not room["black"] and not room["spectators"]:
            self.rooms.pop(room_id, None)
            self.room_sockets.pop(room_id, None)
            await self.broadcast_lobby()
            return

        await self.broadcast_room(room_id)
        await self.broadcast_lobby()

    async def pick_role(self, user: dict, room_id: str, role: str) -> None:
        room = self.rooms.get(room_id)
        if not room or room["game_started"]:
            return

        user_id = user["id"]
        player = {"id": user_id, "username": user["username"], "name": user.get("display_name") or user["username"]}

        if room["white"] and room["white"]["id"] == user_id:
            room["white"] = None
        if room["black"] and room["black"]["id"] == user_id:
            room["black"] = None
        room["spectators"] = [s for s in room["spectators"] if s["id"] != user_id]
        room["match_queue"] = [m for m in room["match_queue"] if m["id"] != user_id]

        if role == "w" and not room["white"]:
            room["white"] = player
        elif role == "b" and not room["black"]:
            room["black"] = player
        else:
            room["spectators"].append(player)

        await self.broadcast_room(room_id)
        await self.broadcast_lobby()

    async def start_game(self, user: dict, room_id: str) -> None:
        room = self.rooms.get(room_id)
        if not room or not room["white"] or not room["black"]:
            return

        user_id = user["id"]
        if room["white"]["id"] != user_id and room["black"]["id"] != user_id:
            return

        mins = room["time_minutes"]
        room["fen"] = START_FEN
        room["active_turn"] = "w"
        room["move_history"] = [{"fen": START_FEN, "move": "Start"}]
        room["last_from"] = None
        room["last_to"] = None
        room["last_flags"] = None
        room["game_started"] = True
        room["result"] = None
        room["draw_offer"] = None
        room["clock"] = {
            "w_remain": float(mins * 60),
            "b_remain": float(mins * 60),
            "last_tick_at": time.time(),
        }

        await self.broadcast_room(room_id)
        await self.broadcast_lobby()

    async def make_move(self, user: dict, room_id: str, data: dict) -> None:
        room = self.rooms.get(room_id)
        if not room or not room["game_started"] or room["result"]:
            return

        user_id = user["id"]
        is_white = room["white"] and room["white"]["id"] == user_id
        is_black = room["black"] and room["black"]["id"] == user_id

        if not is_white and not is_black:
            return

        expected_color = room["active_turn"]
        if (expected_color == "w" and not is_white) or (expected_color == "b" and not is_black):
            return

        now = time.time()
        delta = now - room["clock"]["last_tick_at"]
        if expected_color == "w":
            room["clock"]["w_remain"] = max(0.0, room["clock"]["w_remain"] - delta)
        else:
            room["clock"]["b_remain"] = max(0.0, room["clock"]["b_remain"] - delta)
        room["clock"]["last_tick_at"] = now

        room["fen"] = data.get("fen", room["fen"])
        room["active_turn"] = "b" if expected_color == "w" else "w"
        room["last_from"] = data.get("from")
        room["last_to"] = data.get("to")
        room["last_flags"] = data.get("flags")

        san = data.get("san", "")
        if san:
            room["move_history"].append({"fen": room["fen"], "move": san})

        result = data.get("result")
        if result and isinstance(result, dict):
            room["result"] = result
            self._record_game_stats(room, result.get("winner"))
        else:
            # 1. 3회 동형 반복 검사 (Threefold Repetition)
            counts: Dict[str, int] = {}
            for item in room["move_history"]:
                f = item.get("fen", "")
                if f:
                    pos_key = " ".join(f.split()[:4])
                    counts[pos_key] = counts.get(pos_key, 0) + 1
                    if counts[pos_key] >= 3:
                        room["result"] = {"type": "draw", "winner": None, "desc": "3회 동형 반복 무승부 (동일한 국면 3회 발생)"}
                        self._record_game_stats(room, None)
                        break

            # 2. 50수 규칙 검사 (50-Move Rule)
            if not room["result"]:
                parts = room["fen"].split()
                if len(parts) >= 5:
                    try:
                        half_moves = int(parts[4])
                        if half_moves >= 100:
                            room["result"] = {"type": "draw", "winner": None, "desc": "50수 규칙 무승부 (50수간 폰 전진 및 기물 포획 없음)"}
                            self._record_game_stats(room, None)
                    except (ValueError, IndexError):
                        pass

        room["draw_offer"] = None
        await self.broadcast_room(room_id)

    async def offer_draw(self, user: dict, room_id: str) -> None:
        room = self.rooms.get(room_id)
        if not room or not room["game_started"] or room["result"]:
            return

        user_id = user["id"]
        if room["white"] and room["white"]["id"] == user_id:
            room["draw_offer"] = "w"
        elif room["black"] and room["black"]["id"] == user_id:
            room["draw_offer"] = "b"
        else:
            return

        await self.broadcast_room(room_id)

    async def respond_draw(self, user: dict, room_id: str, accept: bool) -> None:
        room = self.rooms.get(room_id)
        if not room or not room["draw_offer"]:
            return

        user_id = user["id"]
        is_white = room["white"] and room["white"]["id"] == user_id
        is_black = room["black"] and room["black"]["id"] == user_id

        if room["draw_offer"] == "w" and not is_black:
            return
        if room["draw_offer"] == "b" and not is_white:
            return

        if accept:
            room["result"] = {"type": "draw", "winner": None, "desc": "상호 합의에 의한 무승부"}
            self._record_game_stats(room, None)

        room["draw_offer"] = None
        await self.broadcast_room(room_id)

    async def resign(self, user: dict, room_id: str) -> None:
        room = self.rooms.get(room_id)
        if not room or not room["game_started"] or room["result"]:
            return

        user_id = user["id"]
        if room["white"] and room["white"]["id"] == user_id:
            room["result"] = {"type": "resign", "winner": "b", "desc": f"{room['white']['name']} 기권 (흑 승리)"}
            self._record_game_stats(room, "b")
        elif room["black"] and room["black"]["id"] == user_id:
            room["result"] = {"type": "resign", "winner": "w", "desc": f"{room['black']['name']} 기권 (백 승리)"}
            self._record_game_stats(room, "w")
        else:
            return

        await self.broadcast_room(room_id)
        await self.broadcast_lobby()

    async def join_match_queue(self, user: dict, room_id: str) -> None:
        room = self.rooms.get(room_id)
        if not room:
            return
        user_id = user["id"]
        if not any(q["id"] == user_id for q in room["match_queue"]):
            room["match_queue"].append({
                "id": user_id,
                "name": user.get("display_name") or user["username"]
            })
            await self.broadcast_room(room_id)

    def _record_game_stats(self, room: dict, winner: Optional[str]) -> None:
        w = room.get("white")
        b = room.get("black")
        if not w or not b:
            return
        w_id = str(w["id"])
        b_id = str(b["id"])
        stats = room["stats"]
        if w_id not in stats:
            stats[w_id] = {"wins": 0, "draws": 0, "losses": 0}
        if b_id not in stats:
            stats[b_id] = {"wins": 0, "draws": 0, "losses": 0}

        if winner == "w":
            stats[w_id]["wins"] += 1
            stats[b_id]["losses"] += 1
        elif winner == "b":
            stats[b_id]["wins"] += 1
            stats[w_id]["losses"] += 1
        else:
            stats[w_id]["draws"] += 1
            stats[b_id]["draws"] += 1

chess_manager = ChessManager()