"""In-memory Chess Room and Multiplayer Game State Manager."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Dict, List, Optional, Set
from fastapi import WebSocket
import chess
from app.database import get_chess_stats, get_user_by_id, record_chess_result

logger = logging.getLogger("bamboochat.chess")

START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

class ChessManager:
    def __init__(self) -> None:
        self.rooms: Dict[str, dict] = {}
        self.lobby_sockets: Set[WebSocket] = set()
        self.room_sockets: Dict[str, Set[WebSocket]] = {}
        self.socket_user: Dict[WebSocket, dict] = {}
        self.socket_room: Dict[WebSocket, str] = {}
        self.clock_task: Optional[asyncio.Task] = None
        self.result_reset_tasks: Dict[str, asyncio.Task] = {}
        self.result_display_seconds = 3.0
        self.disconnect_tasks: Dict[tuple[str, str], asyncio.Task] = {}
        self.disconnect_grace_seconds = 5.0

    def start_clock_monitor(self) -> None:
        if self.clock_task is None or self.clock_task.done():
            self.clock_task = asyncio.create_task(self._clock_monitor())

    async def _clock_monitor(self) -> None:
        while True:
            await asyncio.sleep(0.2)
            for room_id in list(self.rooms):
                room = self.rooms.get(room_id)
                if not room or not room["game_started"] or room["result"]:
                    continue
                if not room["white"] or not room["black"] or room["draw_offer"]:
                    continue
                now = time.time()
                self._refresh_clock(room, now)
                if room["clock"][f"{room['active_turn']}_remain"] <= 0:
                    expected_color = room["active_turn"]
                    winner = "b" if expected_color == "w" else "w"
                    self._complete_game(room, {"type": "timeout", "winner": winner,
                                               "desc": f"{'백' if winner == 'w' else '흑'} 시간승"})
                await self.broadcast_room(room_id)

    def register_client(self, ws: WebSocket, user: dict) -> None:
        self.socket_user[ws] = user
        self.lobby_sockets.add(ws)

    @staticmethod
    def _player_for(user: dict) -> dict:
        current = get_user_by_id(user["id"])
        display_name = (current or user).get("display_name") or user["username"]
        return {"id": user["id"], "username": user["username"], "name": display_name}

    async def unregister_client(self, ws: WebSocket) -> None:
        user = self.socket_user.pop(ws, None)
        self.lobby_sockets.discard(ws)
        room_id = self.socket_room.pop(ws, None)
        if room_id and room_id in self.room_sockets:
            self.room_sockets[room_id].discard(ws)
            if user:
                self._schedule_disconnect(user, room_id)

    def _schedule_disconnect(self, user: dict, room_id: str) -> None:
        key = (room_id, str(user["id"]))
        previous = self.disconnect_tasks.pop(key, None)
        if previous and not previous.done():
            previous.cancel()
        self.disconnect_tasks[key] = asyncio.create_task(
            self._handle_disconnect_after_grace(user, room_id, key)
        )

    async def _handle_disconnect_after_grace(self, user: dict, room_id: str,
                                             key: tuple[str, str]) -> None:
        try:
            await asyncio.sleep(self.disconnect_grace_seconds)
            room = self.rooms.get(room_id)
            if not room:
                return
            if any(str(self.socket_user.get(ws, {}).get("id")) == key[1]
                   for ws in self.room_sockets.get(room_id, set())):
                return
            await self.handle_disconnect_from_room(user, room_id)
        except asyncio.CancelledError:
            return
        finally:
            if self.disconnect_tasks.get(key) is asyncio.current_task():
                self.disconnect_tasks.pop(key, None)

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
        self._refresh_clock(room, now)
        self._refresh_player_stats(room)
        clock = dict(room["clock"])

        payload = json.dumps({
            "type": "room_state",
            "server_now": now,
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
                    "w_deadline": clock.get("w_deadline"),
                    "b_deadline": clock.get("b_deadline"),
                    "last_tick_at": now,
                },
                "stats": room["stats"],
                "completed_games": room["completed_games"],
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

        player = self._player_for(user)

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
                    "w_deadline": None,
                    "b_deadline": None,
                "last_tick_at": time.time(),
            },
            "stats": {},
            "completed_games": [],
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

        pending = self.disconnect_tasks.pop((room_id, str(user["id"])), None)
        if pending and not pending.done():
            pending.cancel()

        player = self._player_for(user)

        if room["white"] and room["white"]["id"] == user["id"]:
            if role_pref == "spectator" and not room["game_started"]:
                room["white"] = None
                room["spectators"] = [s for s in room["spectators"] if s["id"] != user["id"]]
                room["spectators"].append(player)
            else:
                room["white"] = player
        elif room["black"] and room["black"]["id"] == user["id"]:
            if role_pref == "spectator" and not room["game_started"]:
                room["black"] = None
                room["spectators"] = [s for s in room["spectators"] if s["id"] != user["id"]]
                room["spectators"].append(player)
            else:
                room["black"] = player
        else:
            room["spectators"] = [s for s in room["spectators"] if s["id"] != user["id"]]
            if role_pref == "spectator":
                room["spectators"].append(player)
            elif role_pref == "w" and not room["white"] and not room["game_started"]:
                room["white"] = player
            elif role_pref == "b" and not room["black"] and not room["game_started"]:
                room["black"] = player
            elif role_pref == "spectator":
                room["spectators"].append(player)
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
                self._complete_game(room, {"type": "disconnect", "winner": "b", "desc": "백 플레이어 이탈로 인한 흑 부전승"})
            elif room["black"] and room["black"]["id"] == user_id:
                room["black"] = None
                self._complete_game(room, {"type": "disconnect", "winner": "w", "desc": "흑 플레이어 이탈로 인한 백 부전승"})
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
        player = self._player_for(user)

        if room["white"] and str(room["white"]["id"]) == str(user_id):
            room["white"] = None
        if room["black"] and str(room["black"]["id"]) == str(user_id):
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
            "w_deadline": time.time() + mins * 60,
            "b_deadline": None,
            "last_tick_at": time.time(),
        }

        await self.broadcast_room(room_id)
        await self.broadcast_lobby()

    def _complete_game(self, room: dict, result: dict) -> None:
        room["result"] = result
        self._record_game_stats(room, result.get("winner"))
        room["completed_games"].append({
            "result": result,
            "move_history": room["move_history"],
        })
        room_id = room["id"]
        previous = self.result_reset_tasks.pop(room_id, None)
        if previous and not previous.done():
            previous.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self.result_reset_tasks[room_id] = loop.create_task(self._reset_after_result(room_id))

    async def _reset_after_result(self, room_id: str) -> None:
        try:
            await asyncio.sleep(self.result_display_seconds)
            room = self.rooms.get(room_id)
            if not room or not room["result"]:
                return
            for player in (room["white"], room["black"]):
                if player:
                    room["spectators"] = [s for s in room["spectators"] if str(s["id"]) != str(player["id"])]
                    room["spectators"].append(player)
            room["white"] = None
            room["black"] = None
            room["game_started"] = False
            room["result"] = None
            room["draw_offer"] = None
            room["active_turn"] = "w"
            room["clock"]["last_tick_at"] = time.time()
            room["clock"]["w_deadline"] = None
            room["clock"]["b_deadline"] = None
            await self.broadcast_room(room_id)
            await self.broadcast_lobby()
        finally:
            self.result_reset_tasks.pop(room_id, None)

    def _refresh_clock(self, room: dict, now: float) -> None:
        if room["game_started"] and not room["result"] and room["white"] and room["black"] and not room["draw_offer"]:
            turn = room["active_turn"]
            deadline_key = f"{turn}_deadline"
            deadline = room["clock"].get(deadline_key)
            if deadline is None:
                deadline = now + room["clock"][f"{turn}_remain"]
                room["clock"][deadline_key] = deadline
            room["clock"][f"{turn}_remain"] = max(0.0, deadline - now)
        room["clock"]["last_tick_at"] = now

    def _refresh_player_stats(self, room: dict) -> None:
        for player in (room.get("white"), room.get("black"), *room.get("spectators", [])):
            if player:
                player_id = str(player["id"])
                try:
                    current = get_user_by_id(int(player_id))
                    if current:
                        player["name"] = current.get("display_name") or current["username"]
                except Exception:
                    pass
                if player_id in room["stats"]:
                    continue
                try:
                    room["stats"][player_id] = get_chess_stats(int(player_id))
                except Exception:
                    room["stats"].setdefault(player_id, {"wins": 0, "draws": 0, "losses": 0})

    async def make_move(self, user: dict, room_id: str, data: dict) -> None:
        room = self.rooms.get(room_id)
        if not room or not room["game_started"] or room["result"]:
            return

        user_id = user["id"]
        is_white = room["white"] and str(room["white"]["id"]) == str(user_id)
        is_black = room["black"] and str(room["black"]["id"]) == str(user_id)

        if not is_white and not is_black:
            return

        expected_color = room["active_turn"]
        if (expected_color == "w" and not is_white) or (expected_color == "b" and not is_black):
            return

        try:
            board = chess.Board(room["fen"])
            if board.turn != (chess.WHITE if expected_color == "w" else chess.BLACK):
                await self._send_error(user, "대국 상태가 올바르지 않습니다.")
                return
            from_square = chess.parse_square(str(data.get("from", "")))
            to_square = chess.parse_square(str(data.get("to", "")))
            promotion = data.get("promotion")
            promotion_piece = None
            if promotion:
                promotion_piece = chess.Piece.from_symbol(str(promotion).lower()).piece_type
            move = chess.Move(from_square, to_square, promotion=promotion_piece)
            if move not in board.legal_moves:
                await self._send_error(user, "둘 수 없는 수입니다.")
                return
            san = board.san(move)
            board.push(move)
        except (ValueError, TypeError, chess.InvalidMoveError, chess.IllegalMoveError):
            await self._send_error(user, "올바르지 않은 체스 수입니다.")
            return

        now = time.time()
        self._refresh_clock(room, now)

        if room["clock"][f"{expected_color}_remain"] <= 0:
            winner = "b" if expected_color == "w" else "w"
            self._complete_game(room, {"type": "timeout", "winner": winner,
                                       "desc": f"{'백' if winner == 'w' else '흑'} 시간승"})
            await self.broadcast_room(room_id)
            return

        room["fen"] = board.fen()
        room["active_turn"] = "b" if expected_color == "w" else "w"
        room["clock"][f"{expected_color}_deadline"] = None
        room["clock"][f"{room['active_turn']}_deadline"] = now + room["clock"][f"{room['active_turn']}_remain"]
        room["last_from"] = data.get("from")
        room["last_to"] = data.get("to")
        room["last_flags"] = data.get("flags")

        room["move_history"].append({"fen": room["fen"], "move": san})

        result = self._get_game_result(board, room["move_history"])
        if result:
            self._complete_game(room, result)

        room["draw_offer"] = None
        await self.broadcast_room(room_id)

    async def claim_timeout(self, user: dict, room_id: str) -> None:
        room = self.rooms.get(room_id)
        if not room or not room["game_started"] or room["result"]:
            return

        user_id = user["id"]
        expected_color = room["active_turn"]
        is_player = ((expected_color == "w" and room["white"] and room["white"]["id"] == user_id) or
                     (expected_color == "b" and room["black"] and room["black"]["id"] == user_id))
        if not is_player:
            return

        now = time.time()
        self._refresh_clock(room, now)
        if room["clock"][f"{expected_color}_remain"] <= 0:
            winner = "b" if expected_color == "w" else "w"
            self._complete_game(room, {"type": "timeout", "winner": winner,
                                       "desc": f"{'백' if winner == 'w' else '흑'} 시간승"})
        await self.broadcast_room(room_id)

    async def _send_error(self, user: dict, message: str) -> None:
        for ws, socket_user in list(self.socket_user.items()):
            if socket_user.get("id") == user.get("id"):
                try:
                    await ws.send_text(json.dumps({"type": "error", "message": message}, ensure_ascii=False))
                except Exception:
                    logger.debug("Failed to send chess error", exc_info=True)
                return

    @staticmethod
    def _get_game_result(board: chess.Board, history: list[dict]) -> Optional[dict]:
        if board.is_checkmate():
            winner = "b" if board.turn == chess.WHITE else "w"
            return {"type": "checkmate", "winner": winner,
                    "desc": f"{'백' if winner == 'w' else '흑'} 체크메이트 승리"}
        if board.is_stalemate():
            return {"type": "draw", "winner": None, "desc": "스테일메이트 무승부 (둘 수 있는 수가 없음)"}
        if board.is_insufficient_material():
            return {"type": "draw", "winner": None, "desc": "기물 부족 무승부 (체크메이트 불가)"}
        if board.halfmove_clock >= 100:
            return {"type": "draw", "winner": None, "desc": "50수 규칙 무승부 (50수간 폰 전진 및 기물 포획 없음)"}

        counts: Dict[str, int] = {}
        for item in history:
            fen = item.get("fen", "")
            if fen:
                position_key = " ".join(fen.split()[:4])
                counts[position_key] = counts.get(position_key, 0) + 1
                if counts[position_key] >= 3:
                    return {"type": "draw", "winner": None,
                            "desc": "3회 동형 반복 무승부 (동일한 국면 3회 발생)"}
        return None

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
            self._complete_game(room, {"type": "draw", "winner": None, "desc": "상호 합의에 의한 무승부"})

        room["draw_offer"] = None
        if not accept and room["game_started"]:
            room["clock"][f"{room['active_turn']}_deadline"] = time.time() + room["clock"][f"{room['active_turn']}_remain"]
        await self.broadcast_room(room_id)

    async def resign(self, user: dict, room_id: str) -> None:
        room = self.rooms.get(room_id)
        if not room or not room["game_started"] or room["result"]:
            return

        user_id = user["id"]
        if room["white"] and room["white"]["id"] == user_id:
            self._complete_game(room, {"type": "resign", "winner": "b", "desc": f"{room['white']['name']} 기권 (흑 승리)"})
        elif room["black"] and room["black"]["id"] == user_id:
            self._complete_game(room, {"type": "resign", "winner": "w", "desc": f"{room['black']['name']} 기권 (백 승리)"})
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
        try:
            record_chess_result(int(w_id), int(b_id), winner)
        except Exception:
            logger.exception("Failed to persist chess result")

chess_manager = ChessManager()