"""FastAPI server for a persistent authenticated classroom LAN chat."""
from __future__ import annotations
import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Deque, Dict, Optional, Set
from urllib.parse import unquote, urlsplit

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.auth import (hash_secret, new_session_token, normalize_username, secret_needs_rehash,
                        token_hash, validate_channel_description, validate_channel_display_name,
                        validate_channel_name, validate_display_name, validate_password,
                        validate_username, verify_secret)
from app.config import load_config, save_config
from app.database import (attachment_is_visible_to_user, channel_exists, claim_attachments, configure_storage, count_active_admins,
    create_channel, create_session, create_user, delete_owned_attachment, delete_session,
    delete_user_sessions, get_attachment_record, get_channel_by_id, get_channel_by_name,
    get_direct_messages_between, get_recent_direct_messages, get_recent_messages, get_session_user,
    get_storage_status, get_upload_usage, get_user_by_id, get_user_by_username, init_db,
    list_channels, list_mentionable_users, list_users, prune_expired_sessions, save_attachment,
    save_direct_message, save_message, set_user_active,
    set_user_role, update_channel, archive_channel, delete_channel,
    get_message_by_id, update_message_content, set_message_hidden, move_message_channel,
    get_user_conversation_states, update_user_read_state, set_conversation_muted, get_user_unread_counts,
    update_display_name, update_password_hash,
    ALLOWED_REACTION_EMOJIS, toggle_message_reaction, get_message_reactions, get_direct_message_by_id,
    update_direct_message_content,
    normalize_dm_conversation_id, pin_message, unpin_message, get_pinned_messages, get_pinned_message_ids,
    get_daily_quizzes, submit_quiz_answer, get_user_quiz_stats, get_quiz_leaderboard,
    get_user_quiz_badge, get_user_quiz_badges_map, get_user_quiz_title_options,
    update_quiz_badge_selection, create_quiz, create_quiz_batch,
    get_all_quizzes_admin, delete_quiz, update_quiz, save_quiz_source_document, get_quiz_source_documents,
    toggle_quiz_bookmark, get_quiz_review_list, retry_quiz_answer,
    get_quiz_categories_summary, get_quiz_sidebar_counts, search_conversation_history,
    QUIZ_EXPERTISES, normalize_quiz_import, analyze_quiz_set, create_user_quiz_set, update_user_quiz_set, list_user_quiz_sets, submit_user_quiz_set,
    review_user_quiz_set, update_pending_user_quiz_set, assign_daily_quizzes)
from app.quiz_ai import generate_quizzes_with_gemini, check_quiz_answer, normalize_quiz_answer
from app.chess_manager import chess_manager

CONFIG = load_config()
configure_storage(CONFIG.data_path, CONFIG.database_limit_bytes)
SERVICE_NAME = CONFIG.server_name
MAX_CONTENT_LEN = 2000
MAX_ATTACHMENTS_PER_MESSAGE = 5
MAX_REPLY_CONTENT_LEN = 180
MAX_RAW_MESSAGE_LEN = 8192
MAX_CONNECTIONS_TOTAL = 50
MAX_CONNECTIONS_PER_IP = 4
RATE_LIMIT_MESSAGES = 30
RATE_LIMIT_WINDOW_SECONDS = 10
UPLOAD_RATE_LIMIT = 8
UPLOAD_RATE_WINDOW_SECONDS = 60
LOGIN_RATE_LIMIT = 12
LOGIN_RATE_WINDOW_SECONDS = 300
MAX_AUTH_BODY_BYTES = 16 * 1024
PUBLIC_HISTORY_PAGE_SIZE = 50
DM_HISTORY_PAGE_SIZE = 30
MAX_UPLOAD_BYTES = int(os.getenv("CLASSROOM_MAX_FILE_MB", "50")) * 1024 * 1024
MAX_UPLOAD_BYTES_PER_USER = CONFIG.per_user_attachment_limit_bytes
MAX_TOTAL_UPLOAD_BYTES = CONFIG.attachment_limit_bytes
SESSION_COOKIE = "bamboochat_session"
BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = CONFIG.data_path / "uploads"
QUIZ_SOURCES_DIR = CONFIG.data_path / "quiz_sources"
QUIZ_IMAGES_DIR = CONFIG.data_path / "quiz_images"
EXPLICIT_ALLOWED_HOSTS = {h.strip().casefold() for h in
    os.getenv("CLASSROOM_ALLOWED_HOSTS", "").split(",") if h.strip()}
BLOCKED_UPLOAD_SUFFIXES = {".app",".bat",".cmd",".com",".cpl",".dll",".dmg",".exe",".hta",
    ".htm",".html",".jar",".js",".jse",".lnk",".mjs",".msi",".msp",".pif",".ps1",".reg",
    ".scr",".svg",".vbe",".vbs",".wsf",".wsh"}

DUMMY_LOGIN_HASH = hash_secret("bamboochat-dummy-login-value")
logger = logging.getLogger("bamboochat")
upload_lock = asyncio.Lock()

@dataclass(frozen=True)
class ClientInfo:
    user_id: int
    username: str
    role: str
    ip: str

connected_clients: Dict[WebSocket, ClientInfo] = {}
user_registry: Dict[str, Set[WebSocket]] = defaultdict(set)
message_timestamps: Dict[str, Deque[float]] = defaultdict(deque)
upload_timestamps: Dict[str, Deque[float]] = defaultdict(deque)
login_timestamps: Dict[str, Deque[float]] = defaultdict(deque)

def get_client_ip(connection) -> str:
    try:
        return connection.client.host or ""
    except Exception:
        return ""

def host_is_allowed(host_header: str) -> bool:
    try:
        hostname = urlsplit(f"//{host_header}").hostname
    except ValueError:
        return False
    if not hostname:
        return False
    hostname = hostname.casefold().rstrip(".")
    if hostname in EXPLICIT_ALLOWED_HOSTS or hostname == "localhost":
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return "." not in hostname or hostname.endswith(".local")
    return address.is_private or address.is_loopback or address.is_link_local

def origin_matches_host(origin: str, host: str) -> bool:
    if not origin or not host or not host_is_allowed(host):
        return False
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return parsed.scheme in {"http","https"} and parsed.netloc.casefold() == host.casefold()

def request_origin_is_allowed(request: Request) -> bool:
    return origin_matches_host(request.headers.get("origin",""), request.headers.get("host",""))

def websocket_origin_is_allowed(ws: WebSocket) -> bool:
    return origin_matches_host(ws.headers.get("origin",""), ws.headers.get("host",""))

def sliding_window_allowed(registry: Dict[str,Deque[float]], key: str, limit: int,
                            window: int, now: Optional[float]=None) -> bool:
    current = time.monotonic() if now is None else now
    values = registry[key]
    cutoff = current-window
    while values and values[0] <= cutoff:
        values.popleft()
    if len(values) >= limit:
        return False
    values.append(current)
    return True

def clean_reply(value) -> Optional[dict]:
    if not isinstance(value, dict):
        return None
    nickname = str(value.get("nickname","")).strip()[:30]
    content = str(value.get("content","")).strip()[:MAX_REPLY_CONTENT_LEN]
    return {"nickname":nickname,"content":content} if nickname or content else None

def clean_original_filename(encoded_name: str) -> str:
    decoded = unquote(encoded_name).replace("\\","/").split("/")[-1]
    decoded = "".join(c for c in decoded if ord(c)>=32 and c not in '<>:"|?*').strip(" .")
    if not decoded:
        raise HTTPException(400, "파일 이름이 필요합니다.")
    if len(decoded)>180:
        suffix=Path(decoded).suffix[:20]
        decoded=decoded[:180-len(suffix)]+suffix
    return decoded

def upload_is_blocked(filename: str) -> bool:
    return any(filename.casefold().endswith(s) for s in BLOCKED_UPLOAD_SUFFIXES)

def detect_preview_type(header: bytes) -> Optional[str]:
    if header.startswith(b"\x89PNG\r\n\x1a\n"): return "image/png"
    if header.startswith(b"\xff\xd8\xff"): return "image/jpeg"
    if header.startswith((b"GIF87a",b"GIF89a")): return "image/gif"
    if len(header)>=12 and header[:4]==b"RIFF" and header[8:12]==b"WEBP": return "image/webp"
    return None

def attachment_path(stored_name: str) -> Path:
    return UPLOAD_DIR/stored_name

def remove_stored_file(stored_name: str) -> None:
    try:
        attachment_path(stored_name).unlink(missing_ok=True)
    except OSError:
        logger.exception("Failed to remove attachment %s", stored_name)

def session_user_from_token(raw_token: str) -> Optional[dict]:
    return get_session_user(token_hash(raw_token)) if raw_token else None

def request_user(request: Request) -> dict:
    user = session_user_from_token(request.cookies.get(SESSION_COOKIE,""))
    if not user:
        raise HTTPException(401, "로그인이 필요합니다.")
    return user

def public_user(user: dict) -> dict:
    return {"id":user["id"],"username":user["username"],
            "display_name":user.get("display_name") or user["username"],
            "role":user["role"],
            "session_expires_at":user.get("session_expires_at")}

def require_admin(request: Request) -> dict:
    user = request_user(request)
    if user["role"] != "admin":
        raise HTTPException(403, "관리자 권한이 필요합니다.")
    return user

async def read_json_body(request: Request) -> dict:
    content_length = request.headers.get("content-length", "")
    if content_length.isdigit() and int(content_length) > MAX_AUTH_BODY_BYTES:
        raise HTTPException(413, "요청 본문이 너무 큽니다.")
    body = await request.body()
    if len(body) > MAX_AUTH_BODY_BYTES:
        raise HTTPException(413, "요청 본문이 너무 큽니다.")
    try:
        data = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "올바른 JSON 요청이 필요합니다.") from exc
    if not isinstance(data, dict):
        raise HTTPException(400, "올바른 JSON 객체가 필요합니다.")
    return data

def current_user_ip(user_id: int) -> Optional[str]:
    ips = sorted({info.ip for info in connected_clients.values()
                  if info.user_id == user_id and info.ip})
    return ", ".join(ips) if ips else None

def find_mentions(content: str, users: Optional[list[dict]] = None,
                  exclude_user_id: Optional[int] = None) -> list[dict]:
    matches = []
    for user in users if users is not None else list_mentionable_users():
        if exclude_user_id is not None and user["id"] == exclude_user_id:
            continue
        username = user["username"]
        display_name = user.get("display_name") or username
        # Match on @username
        pattern = rf"(?<![\w.@-])@{re.escape(username)}(?![\w.-])"
        if re.search(pattern, content, flags=re.IGNORECASE):
            matches.append({"user_id": user["id"], "username": username,
                            "display_name": display_name})
            continue
        # Also match on @display_name if it differs from username
        if display_name != username:
            display_pattern = rf"(?<![\w.@-])@{re.escape(display_name)}(?![\w.-])"
            if re.search(display_pattern, content, flags=re.IGNORECASE):
                matches.append({"user_id": user["id"], "username": username,
                                "display_name": display_name})
    return matches

def with_mentions(message: dict, users: Optional[list[dict]] = None) -> dict:
    result = dict(message)
    mentions = find_mentions(result.get("content", ""), users, result.get("author_id"))
    result["mentions"] = mentions
    result["mentioned_user_ids"] = [mention["user_id"] for mention in mentions]
    if result.get("author_id") and "quiz_badge" not in result:
        result["quiz_badge"] = get_user_quiz_badge(result["author_id"])
    return result

def account_public(user: dict) -> dict:
    return {"id": user["id"], "username": user["username"],
            "display_name": user.get("display_name") or user["username"],
            "role": user["role"],
            "active": bool(user["active"]), "created_at": user["created_at"],
            "last_login": user.get("last_login"),
            "last_login_ip": user.get("last_login_ip"),
            "message_count": user.get("message_count", 0),
            "attachment_bytes": user.get("attachment_bytes", 0),
            "current_ip": current_user_ip(user["id"])}

async def disconnect_user(user_id: int, reason: str) -> None:
    targets = [ws for ws, info in connected_clients.items() if info.user_id == user_id]
    for ws in targets:
        try:
            await ws.close(code=1008, reason=reason)
        except Exception:
            pass

def set_session_cookie(response: Response, raw_token: str, request: Request) -> None:
    response.set_cookie(SESSION_COOKIE, raw_token, max_age=CONFIG.session_hours*3600,
        httponly=True, samesite="strict", secure=request.url.scheme=="https", path="/")

def connection_limit_reason(ip: str) -> str:
    if len(connected_clients)>=MAX_CONNECTIONS_TOTAL:
        return "서버의 최대 접속 인원에 도달했습니다."
    if sum(info.ip==ip for info in connected_clients.values())>=MAX_CONNECTIONS_PER_IP:
        return "같은 기기에서 너무 많은 연결이 열려 있습니다."
    return ""

async def broadcast(payload: dict) -> None:
    encoded=json.dumps(payload,ensure_ascii=False)
    dead=set()
    for ws in list(connected_clients):
        try: await ws.send_text(encoded)
        except Exception: dead.add(ws)
    for ws in dead: _remove_client(ws)

async def send_to_user_id(user_id: int, payload: dict) -> None:
    encoded = json.dumps(payload, ensure_ascii=False)
    dead = set()
    for ws, info in list(connected_clients.items()):
        if info.user_id == user_id:
            try:
                await ws.send_text(encoded)
            except Exception:
                dead.add(ws)
    for ws in dead:
        _remove_client(ws)

def _remove_client(ws: WebSocket) -> None:
    info=connected_clients.pop(ws,None)
    if info:
        sockets=user_registry.get(info.username,set())
        sockets.discard(ws)
        if not sockets: user_registry.pop(info.username,None)

async def broadcast_presence() -> None:
    await broadcast({"type":"presence","count":len(user_registry)})

async def broadcast_users() -> None:
    online_user_ids = {info.user_id for info in connected_clients.values()}
    raw_list = list_mentionable_users()
    uids = [u["id"] for u in raw_list]
    badges_map = get_user_quiz_badges_map(uids)
    mention_list = [
        {**user, "online": user["id"] in online_user_ids, "quiz_badge": badges_map.get(user["id"])}
        for user in raw_list
    ]
    await broadcast({
        "type":"users",
        "list":[{"nickname":name, "display_name":
                 next((u.get("display_name") or name for u in mention_list if u.get("username")==name), name)}
                for name in sorted(user_registry)],
        "mention_list":mention_list,
    })

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    UPLOAD_DIR.mkdir(parents=True,exist_ok=True)
    QUIZ_SOURCES_DIR.mkdir(parents=True,exist_ok=True)
    QUIZ_IMAGES_DIR.mkdir(parents=True,exist_ok=True)
    prune_expired_sessions()
    logger.info("Persistent storage initialized at %s", CONFIG.data_path)
    yield

app=FastAPI(title=SERVICE_NAME,lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)

@app.middleware("http")
async def security_headers(request: Request, call_next):
    if not host_is_allowed(request.headers.get("host","")):
        return PlainTextResponse("Invalid host",status_code=400)
    response=await call_next(request)
    response.headers["Content-Security-Policy"]=("default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'; img-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self' ws: wss:")
    response.headers["Referrer-Policy"]="no-referrer"
    response.headers["X-Content-Type-Options"]="nosniff"
    response.headers["X-Frame-Options"]="DENY"
    response.headers["Cache-Control"]="no-store"
    return response

app.mount("/static",StaticFiles(directory=BASE_DIR/"static"),name="static")
templates=Jinja2Templates(directory=BASE_DIR/"templates")

@app.get("/",response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request,name="index.html",context={
        "service_name":SERVICE_NAME,"max_message_len":MAX_CONTENT_LEN,
        "max_file_mb":MAX_UPLOAD_BYTES//(1024*1024),"max_files":MAX_ATTACHMENTS_PER_MESSAGE,
        "registration_enabled":CONFIG.registration_enabled})

@app.post("/api/auth/register")
async def register(request: Request):
    if not request_origin_is_allowed(request): raise HTTPException(403,"허용되지 않은 요청입니다.")
    if not CONFIG.registration_enabled or not CONFIG.enrollment_code_hash:
        raise HTTPException(403,"현재 신규 가입이 닫혀 있습니다.")
    ip=get_client_ip(request)
    if not sliding_window_allowed(login_timestamps,f"register:{ip}",LOGIN_RATE_LIMIT,LOGIN_RATE_WINDOW_SECONDS):
        raise HTTPException(429,"요청이 너무 많습니다. 잠시 후 다시 시도하세요.")
    data=await read_json_body(request)
    try:
        username=validate_username(str(data.get("username","")))
        password=str(data.get("password",""))
        validate_password(password)
    except ValueError as exc:
        raise HTTPException(400,str(exc)) from exc
    enrollment=str(data.get("enrollment_code",""))
    if not await asyncio.to_thread(verify_secret,CONFIG.enrollment_code_hash,enrollment):
        raise HTTPException(403,"가입 코드가 올바르지 않습니다.")
    password_hash=await asyncio.to_thread(hash_secret,password)
    try:
        user=create_user(username,password_hash)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409,"이미 사용 중인 아이디입니다.") from exc
    raw=new_session_token()
    expiry=(datetime.now(timezone.utc)+timedelta(hours=CONFIG.session_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ip=get_client_ip(request)
    create_session(token_hash(raw),user["id"],expiry,client_ip=ip)
    user=get_session_user(token_hash(raw))
    response=JSONResponse(public_user(user),status_code=201)
    set_session_cookie(response,raw,request)
    return response

@app.post("/api/auth/login")
async def login(request: Request):
    if not request_origin_is_allowed(request): raise HTTPException(403,"허용되지 않은 요청입니다.")
    data=await read_json_body(request)
    username=str(data.get("username",""))
    password=str(data.get("password",""))
    ip=get_client_ip(request)
    key=f"{ip}:{normalize_username(username)[:50]}"
    if not sliding_window_allowed(login_timestamps,key,LOGIN_RATE_LIMIT,LOGIN_RATE_WINDOW_SECONDS):
        raise HTTPException(429,"로그인 시도가 너무 많습니다. 잠시 후 다시 시도하세요.")
    user=get_user_by_username(username)
    candidate_hash = user["password_hash"] if user else DUMMY_LOGIN_HASH
    password_ok = await asyncio.to_thread(verify_secret, candidate_hash, password)
    if user and password_ok and not user["active"]:
        raise HTTPException(401, "이 아이디는 비활성화되어 있습니다. 관리자에게 문의하세요.")
    valid=bool(user and user["active"] and password_ok)
    if not valid:
        raise HTTPException(401,"아이디 또는 비밀번호가 올바르지 않습니다.")
    if secret_needs_rehash(user["password_hash"]):
        update_password_hash(user["id"],await asyncio.to_thread(hash_secret,password))
    raw=new_session_token()
    expiry=(datetime.now(timezone.utc)+timedelta(hours=CONFIG.session_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    create_session(token_hash(raw),user["id"],expiry,client_ip=ip)
    user=get_session_user(token_hash(raw))
    response=JSONResponse(public_user(user))
    set_session_cookie(response,raw,request)
    return response

@app.post("/api/auth/logout",status_code=204)
async def logout(request: Request):
    if not request_origin_is_allowed(request): raise HTTPException(403,"허용되지 않은 요청입니다.")
    raw=request.cookies.get(SESSION_COOKIE,"")
    if raw: delete_session(token_hash(raw))
    response=Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE,path="/",samesite="strict")
    return response

@app.get("/api/auth/me")
async def auth_me(request: Request):
    return public_user(request_user(request))

@app.post("/api/auth/display-name")
async def change_display_name(request: Request):
    if not request_origin_is_allowed(request):
        raise HTTPException(403, "허용되지 않은 요청입니다.")
    user = request_user(request)
    data = await read_json_body(request)
    raw_name = str(data.get("display_name", ""))
    try:
        display_name = validate_display_name(raw_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    updated = update_display_name(user["id"], display_name)
    if not updated:
        raise HTTPException(404, "사용자를 찾을 수 없습니다.")
    await broadcast_users()
    return public_user(updated)

@app.get("/api/auth/quiz-titles")
async def get_my_quiz_titles(request: Request):
    user = request_user(request)
    titles = get_user_quiz_title_options(user["id"])
    selected = user.get("quiz_badge_selection") or "score"
    if selected not in {"score", "none"} and selected not in {item["selection"] for item in titles}:
        selected = "score"
    return {
        "selected": selected,
        "titles": titles,
    }

@app.post("/api/auth/quiz-title")
async def change_quiz_title(request: Request):
    if not request_origin_is_allowed(request):
        raise HTTPException(403, "허용되지 않은 요청입니다.")
    user = request_user(request)
    data = await read_json_body(request)
    try:
        updated = update_quiz_badge_selection(user["id"], str(data.get("selection", "score")))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not updated:
        raise HTTPException(404, "사용자를 찾을 수 없습니다.")
    await broadcast_users()
    return {
        "selected": updated.get("quiz_badge_selection") or "score",
        "quiz_badge": get_user_quiz_badge(user["id"]),
    }

@app.get("/api/channels")
async def api_channels_list(request: Request, include_archived: bool = False):
    user = request_user(request)
    if include_archived and user["role"] != "admin":
        raise HTTPException(403, "관리자 권한이 필요합니다.")
    return list_channels(include_archived=include_archived)

@app.post("/api/channels", status_code=201)
async def api_create_channel(request: Request):
    if not request_origin_is_allowed(request):
        raise HTTPException(403, "허용되지 않은 요청입니다.")
    user = request_user(request)
    data = await read_json_body(request)
    name = str(data.get("name", ""))
    display_name = str(data.get("display_name", ""))
    description = str(data.get("description", ""))
    try:
        clean_name = validate_channel_name(name)
        clean_display = validate_channel_display_name(display_name)
        clean_desc = validate_channel_description(description)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if get_channel_by_name(clean_name):
        raise HTTPException(409, "이미 존재하는 채널 이름입니다.")
    channel = create_channel(clean_name, clean_display, clean_desc, user["id"])
    await broadcast({"type": "channel_created", "channel": channel})
    return channel

@app.get("/api/channels/{channel_id}")
async def api_get_channel(channel_id: int, request: Request):
    request_user(request)
    channel = get_channel_by_id(channel_id)
    if not channel:
        raise HTTPException(404, "채널을 찾을 수 없습니다.")
    return channel

@app.patch("/api/channels/{channel_id}")
async def api_update_channel(channel_id: int, request: Request):
    if not request_origin_is_allowed(request):
        raise HTTPException(403, "허용되지 않은 요청입니다.")
    require_admin(request)
    current = get_channel_by_id(channel_id)
    if not current:
        raise HTTPException(404, "채널을 찾을 수 없습니다.")
    data = await read_json_body(request)
    name = data.get("name")
    display_name = data.get("display_name")
    description = data.get("description")

    if name is not None:
        try:
            clean_name = validate_channel_name(str(name))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        existing = get_channel_by_name(clean_name)
        if existing and existing["id"] != channel_id:
            raise HTTPException(409, "이미 존재하는 채널 이름입니다.")
        name = clean_name

    if display_name is not None:
        try:
            display_name = validate_channel_display_name(str(display_name))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    if description is not None:
        try:
            description = validate_channel_description(str(description))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    updated = update_channel(channel_id, name=name, display_name=display_name, description=description)
    if not updated:
        raise HTTPException(404, "채널을 찾을 수 없습니다.")
    await broadcast({"type": "channel_updated", "channel": updated})
    return updated

@app.post("/api/channels/{channel_id}/archive")
@app.post("/api/channels/{channel_id}/unarchive")
async def api_archive_channel(channel_id: int, request: Request):
    if not request_origin_is_allowed(request):
        raise HTTPException(403, "허용되지 않은 요청입니다.")
    require_admin(request)
    current = get_channel_by_id(channel_id)
    if not current:
        raise HTTPException(404, "채널을 찾을 수 없습니다.")
    if current.get("is_default"):
        raise HTTPException(400, "기본 채널은 보관할 수 없습니다.")
    data = await read_json_body(request) if request.headers.get("content-type", "").startswith("application/json") else {}
    unarchive = bool(data.get("unarchive", False)) or request.url.path.endswith("/unarchive")
    try:
        updated = archive_channel(channel_id, unarchive=unarchive)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not updated:
        raise HTTPException(404, "채널을 찾을 수 없습니다.")
    event_type = "channel_unarchived" if unarchive else "channel_archived"
    await broadcast({"type": event_type, "channel_id": channel_id, "channel": updated})
    return updated

@app.delete("/api/channels/{channel_id}")
async def api_delete_channel(channel_id: int, request: Request):
    if not request_origin_is_allowed(request):
        raise HTTPException(403, "허용되지 않은 요청입니다.")
    require_admin(request)
    current = get_channel_by_id(channel_id)
    if not current:
        raise HTTPException(404, "채널을 찾을 수 없습니다.")
    if current.get("is_default"):
        raise HTTPException(400, "기본 채널은 삭제할 수 없습니다.")
    try:
        deleted_files = delete_channel(channel_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if deleted_files is None:
        raise HTTPException(404, "채널을 찾을 수 없습니다.")
    for stored_name in deleted_files:
        remove_stored_file(stored_name)
    await broadcast({"type": "channel_deleted", "channel_id": channel_id})
    return {"status": "ok", "deleted_channel_id": channel_id}

@app.get("/api/channels/{channel_id}/messages")
async def channel_message_history(channel_id: int, request: Request, before_id: Optional[int] = None):
    user = request_user(request)
    if not channel_exists(channel_id):
        raise HTTPException(404, "채널을 찾을 수 없습니다.")
    messages = get_recent_messages(PUBLIC_HISTORY_PAGE_SIZE + 1, before_id, channel_id=channel_id, current_user_id=user["id"])
    has_more = len(messages) > PUBLIC_HISTORY_PAGE_SIZE
    messages = messages[-PUBLIC_HISTORY_PAGE_SIZE:]
    users = list_mentionable_users()
    return {"messages": [with_mentions(message, users) for message in messages],
            "has_more": has_more}

@app.patch("/api/messages/{message_id}")
async def api_update_message(message_id: int, request: Request):
    if not request_origin_is_allowed(request):
        raise HTTPException(403, "허용되지 않은 요청입니다.")
    user = request_user(request)
    data = await read_json_body(request)
    content = str(data.get("content", "")).strip()
    if not content:
        raise HTTPException(400, "메시지 내용을 입력하세요.")
    if len(content) > MAX_CONTENT_LEN:
        raise HTTPException(400, f"메시지는 {MAX_CONTENT_LEN}자 이하여야 합니다.")
    try:
        updated = update_message_content(message_id, content, user["id"], is_admin=user["role"] == "admin")
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    if not updated:
        raise HTTPException(404, "메시지를 찾을 수 없습니다.")
    users = list_mentionable_users()
    enriched = with_mentions(updated, users)
    await broadcast({"type": "message_edited", "message": enriched})
    return enriched

@app.patch("/api/dms/{dm_id}")
async def api_update_direct_message(dm_id: int, request: Request):
    if not request_origin_is_allowed(request):
        raise HTTPException(403, "허용되지 않은 요청입니다.")
    user = request_user(request)
    data = await read_json_body(request)
    content = str(data.get("content", "")).strip()
    if not content:
        raise HTTPException(400, "메시지 내용을 입력하세요.")
    if len(content) > MAX_CONTENT_LEN:
        raise HTTPException(400, f"메시지는 {MAX_CONTENT_LEN}자 이하여야 합니다.")
    try:
        updated = update_direct_message_content(dm_id, content, user["id"])
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    if not updated:
        raise HTTPException(404, "1:1 메시지를 찾을 수 없습니다.")

    payload = {"type": "dm_edited", "message": updated}
    encoded = json.dumps(payload, ensure_ascii=False)

    sender_targets = list(user_registry.get(updated["from_nick"], set()))
    recipient_targets = list(user_registry.get(updated["to_nick"], set()))
    for target_ws in set(sender_targets + recipient_targets):
        try:
            await target_ws.send_text(encoded)
        except Exception:
            pass

    return updated

@app.post("/api/messages/{message_id}/hide")
async def api_hide_message(message_id: int, request: Request):
    if not request_origin_is_allowed(request):
        raise HTTPException(403, "허용되지 않은 요청입니다.")
    require_admin(request)
    data = await read_json_body(request) if request.headers.get("content-type", "").startswith("application/json") else {}
    hidden = bool(data.get("hidden", True))
    updated = set_message_hidden(message_id, hidden)
    if not updated:
        raise HTTPException(404, "메시지를 찾을 수 없습니다.")
    users = list_mentionable_users()
    enriched = with_mentions(updated, users)
    await broadcast({"type": "message_hidden", "message": enriched, "is_hidden": hidden})
    return enriched

@app.post("/api/messages/{message_id}/move")
async def api_move_message(message_id: int, request: Request):
    if not request_origin_is_allowed(request):
        raise HTTPException(403, "허용되지 않은 요청입니다.")
    require_admin(request)
    data = await read_json_body(request)
    to_channel_id = data.get("to_channel_id")
    if not to_channel_id:
        raise HTTPException(400, "이동할 채널을 지정하세요.")
    try:
        to_channel_id = int(to_channel_id)
    except (ValueError, TypeError):
        raise HTTPException(400, "유효한 채널 ID가 아닙니다.")
    target_chan = get_channel_by_id(to_channel_id)
    if not target_chan:
        raise HTTPException(404, "대상 채널을 찾을 수 없습니다.")
    if target_chan.get("archived"):
        raise HTTPException(400, "보관된 채널로는 메시지를 이동할 수 없습니다.")
    current_msg = get_message_by_id(message_id)
    if not current_msg:
        raise HTTPException(404, "메시지를 찾을 수 없습니다.")
    from_channel_id = current_msg["channel_id"]
    if from_channel_id == to_channel_id:
        raise HTTPException(400, "이미 해당 채널에 위치한 메시지입니다.")
    updated = move_message_channel(message_id, to_channel_id)
    if not updated:
        raise HTTPException(404, "메시지를 찾을 수 없습니다.")
    users = list_mentionable_users()
    enriched = with_mentions(updated, users)
    await broadcast({
        "type": "message_moved",
        "message_id": enriched["message_id"],
        "from_channel_id": from_channel_id,
        "to_channel_id": to_channel_id,
        "message": enriched
    })
    return enriched

@app.post("/api/messages/{message_type}/{message_id}/reactions/toggle")
async def api_toggle_reaction(message_type: str, message_id: int, request: Request):
    if not request_origin_is_allowed(request):
        raise HTTPException(403, "허용되지 않은 요청입니다.")
    user = request_user(request)
    if message_type not in ("channel", "dm"):
        raise HTTPException(400, "유효하지 않은 메시지 유형입니다.")

    data = await read_json_body(request)
    emoji = str(data.get("emoji", "")).strip()
    if not emoji or emoji not in ALLOWED_REACTION_EMOJIS:
        raise HTTPException(400, "유효하지 않은 이모지입니다.")

    if message_type == "channel":
        msg = get_message_by_id(message_id, current_user_id=user["id"])
        if not msg:
            raise HTTPException(404, "메시지를 찾을 수 없습니다.")
        if msg.get("is_hidden") and user["role"] != "admin":
            raise HTTPException(403, "숨겨진 메시지에는 반응할 수 없습니다.")

        toggle_message_reaction("channel", message_id, user["id"], emoji)
        updated_reactions = get_message_reactions("channel", message_id, current_user_id=user["id"])

        all_reactions = get_message_reactions("channel", message_id, current_user_id=None)
        await broadcast({
            "type": "reaction_updated",
            "message_type": "channel",
            "message_id": message_id,
            "channel_id": msg["channel_id"],
            "reactions": all_reactions,
        })
        return {
            "message_type": "channel",
            "message_id": message_id,
            "reactions": updated_reactions,
        }
    else:  # dm
        dm = get_direct_message_by_id(message_id, current_user_id=user["id"])
        if not dm:
            raise HTTPException(404, "대화 메시지를 찾을 수 없습니다.")
        if user["id"] not in (dm["from_user_id"], dm["to_user_id"]):
            raise HTTPException(403, "대화 참여자만 반응할 수 있습니다.")

        toggle_message_reaction("dm", message_id, user["id"], emoji)
        user_reactions = get_message_reactions("dm", message_id, current_user_id=user["id"])

        partner_id = dm["to_user_id"] if dm["from_user_id"] == user["id"] else dm["from_user_id"]

        user_event = {
            "type": "reaction_updated",
            "message_type": "dm",
            "message_id": message_id,
            "reactions": user_reactions,
        }
        await send_to_user_id(user["id"], user_event)

        partner_reactions = get_message_reactions("dm", message_id, current_user_id=partner_id)
        partner_event = {
            "type": "reaction_updated",
            "message_type": "dm",
            "message_id": message_id,
            "reactions": partner_reactions,
        }
        await send_to_user_id(partner_id, partner_event)

        return {
            "message_type": "dm",
            "message_id": message_id,
            "reactions": user_reactions,
        }

@app.get("/api/read-states")
async def api_get_read_states(request: Request):
    user = request_user(request)
    states = get_user_conversation_states(user["id"])
    unread_counts = get_user_unread_counts(user["id"])
    return {"states": states, "unread_counts": unread_counts}

@app.post("/api/read-states/ack")
async def api_ack_read_state(request: Request):
    if not request_origin_is_allowed(request):
        raise HTTPException(403, "허용되지 않은 요청입니다.")
    user = request_user(request)
    data = await read_json_body(request)
    conv_type = str(data.get("conversation_type", "")).strip()
    conv_id = str(data.get("conversation_id", "")).strip()
    last_read_id = data.get("last_read_message_id")
    if conv_type not in ("channel", "dm") or not conv_id or last_read_id is None:
        raise HTTPException(400, "유효한 대화 정보와 메시지 ID가 필요합니다.")
    try:
        last_read_id = int(last_read_id)
    except (ValueError, TypeError):
        raise HTTPException(400, "유효한 메시지 ID가 아닙니다.")
    
    if conv_type == "dm":
        if not conv_id.isdigit():
            partner = get_user_by_username(conv_id)
            if partner:
                conv_id = str(partner["id"])
            else:
                raise HTTPException(404, "대화 상대를 찾을 수 없습니다.")
    
    updated = update_user_read_state(user["id"], conv_type, conv_id, last_read_id)
    unread_counts = get_user_unread_counts(user["id"])
    payload = {
        "type": "read_state_updated",
        "state": updated,
        "unread_counts": unread_counts
    }
    await send_to_user_id(user["id"], payload)
    return {"state": updated, "unread_counts": unread_counts}

@app.post("/api/read-states/mute")
async def api_set_muted(request: Request):
    if not request_origin_is_allowed(request):
        raise HTTPException(403, "허용되지 않은 요청입니다.")
    user = request_user(request)
    data = await read_json_body(request)
    conv_type = str(data.get("conversation_type", "")).strip()
    conv_id = str(data.get("conversation_id", "")).strip()
    muted = bool(data.get("muted", True))
    if conv_type not in ("channel", "dm") or not conv_id:
        raise HTTPException(400, "유효한 대화 정보가 필요합니다.")
    
    if conv_type == "dm":
        if not conv_id.isdigit():
            partner = get_user_by_username(conv_id)
            if partner:
                conv_id = str(partner["id"])
            else:
                raise HTTPException(404, "대화 상대를 찾을 수 없습니다.")
    
    updated = set_conversation_muted(user["id"], conv_type, conv_id, muted)
    payload = {
        "type": "conversation_muted_updated",
        "state": updated
    }
    await send_to_user_id(user["id"], payload)
    return {"state": updated}

@app.get("/api/conversations/{conv_type}/{conv_id}/pins")
async def api_get_pins(conv_type: str, conv_id: str, request: Request):
    user = request_user(request)
    if conv_type not in ("channel", "dm"):
        raise HTTPException(400, "유효하지 않은 대화 유형입니다.")

    if conv_type == "channel":
        try:
            cid = int(conv_id)
        except (ValueError, TypeError):
            raise HTTPException(400, "유효하지 않은 채널 ID입니다.")
        chan = get_channel_by_id(cid)
        if not chan:
            raise HTTPException(404, "채널을 찾을 수 없습니다.")
        pins = get_pinned_messages("channel", str(cid), current_user_id=user["id"])
    else:
        if conv_id.isdigit():
            partner = get_user_by_id(int(conv_id))
        else:
            partner = get_user_by_username(conv_id)
        if not partner or partner["id"] == user["id"]:
            raise HTTPException(404, "대화 상대를 찾을 수 없습니다.")
        norm_id = normalize_dm_conversation_id(user["id"], partner["id"])
        pins = get_pinned_messages("dm", norm_id, current_user_id=user["id"])

    users = list_mentionable_users()
    enriched_pins = []
    for pin in pins:
        p = dict(pin)
        p["message"] = with_mentions(p["message"], users)
        enriched_pins.append(p)
    return {"pins": enriched_pins}

@app.post("/api/conversations/{conv_type}/{conv_id}/pins/{message_id}")
async def api_pin_message(conv_type: str, conv_id: str, message_id: int, request: Request):
    if not request_origin_is_allowed(request):
        raise HTTPException(403, "허용되지 않은 요청입니다.")
    user = request_user(request)
    if conv_type not in ("channel", "dm"):
        raise HTTPException(400, "유효하지 않은 대화 유형입니다.")

    if conv_type == "channel":
        try:
            cid = int(conv_id)
        except (ValueError, TypeError):
            raise HTTPException(400, "유효하지 않은 채널 ID입니다.")
        chan = get_channel_by_id(cid)
        if not chan:
            raise HTTPException(404, "채널을 찾을 수 없습니다.")
        try:
            pin = pin_message("channel", str(cid), message_id, user["id"])
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc

        users = list_mentionable_users()
        pin["message"] = with_mentions(pin["message"], users)

        await broadcast({
            "type": "pin_updated",
            "conversation_type": "channel",
            "conversation_id": str(cid),
            "message_id": message_id,
            "is_pinned": True,
            "pin": pin,
        })
        return pin
    else:
        if conv_id.isdigit():
            partner = get_user_by_id(int(conv_id))
        else:
            partner = get_user_by_username(conv_id)
        if not partner or partner["id"] == user["id"]:
            raise HTTPException(404, "대화 상대를 찾을 수 없습니다.")
        norm_id = normalize_dm_conversation_id(user["id"], partner["id"])
        try:
            pin = pin_message("dm", norm_id, message_id, user["id"])
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc

        users = list_mentionable_users()
        pin["message"] = with_mentions(pin["message"], users)

        payload = {
            "type": "pin_updated",
            "conversation_type": "dm",
            "conversation_id": norm_id,
            "partner_id": partner["id"],
            "partner_username": partner["username"],
            "sender_id": user["id"],
            "sender_username": user["username"],
            "message_id": message_id,
            "is_pinned": True,
            "pin": pin,
        }
        encoded = json.dumps(payload, ensure_ascii=False)
        targets = set(user_registry.get(user["username"], set()) | user_registry.get(partner["username"], set()))
        for target_ws in targets:
            try:
                await target_ws.send_text(encoded)
            except Exception:
                pass
        return pin

@app.delete("/api/conversations/{conv_type}/{conv_id}/pins/{message_id}")
async def api_unpin_message(conv_type: str, conv_id: str, message_id: int, request: Request):
    if not request_origin_is_allowed(request):
        raise HTTPException(403, "허용되지 않은 요청입니다.")
    user = request_user(request)
    if conv_type not in ("channel", "dm"):
        raise HTTPException(400, "유효하지 않은 대화 유형입니다.")

    if conv_type == "channel":
        try:
            cid = int(conv_id)
        except (ValueError, TypeError):
            raise HTTPException(400, "유효하지 않은 채널 ID입니다.")
        chan = get_channel_by_id(cid)
        if not chan:
            raise HTTPException(404, "채널을 찾을 수 없습니다.")
        unpinned = unpin_message("channel", str(cid), message_id, user["id"])
        await broadcast({
            "type": "pin_updated",
            "conversation_type": "channel",
            "conversation_id": str(cid),
            "message_id": message_id,
            "is_pinned": False,
            "pin": None,
        })
        return {"success": unpinned}
    else:
        if conv_id.isdigit():
            partner = get_user_by_id(int(conv_id))
        else:
            partner = get_user_by_username(conv_id)
        if not partner or partner["id"] == user["id"]:
            raise HTTPException(404, "대화 상대를 찾을 수 없습니다.")
        norm_id = normalize_dm_conversation_id(user["id"], partner["id"])
        try:
            unpinned = unpin_message("dm", norm_id, message_id, user["id"])
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc

        payload = {
            "type": "pin_updated",
            "conversation_type": "dm",
            "conversation_id": norm_id,
            "partner_id": partner["id"],
            "partner_username": partner["username"],
            "sender_id": user["id"],
            "sender_username": user["username"],
            "message_id": message_id,
            "is_pinned": False,
            "pin": None,
        }
        encoded = json.dumps(payload, ensure_ascii=False)
        targets = set(user_registry.get(user["username"], set()) | user_registry.get(partner["username"], set()))
        for target_ws in targets:
            try:
                await target_ws.send_text(encoded)
            except Exception:
                pass
        return {"success": unpinned}

@app.get("/api/messages")
async def api_messages(request: Request):
    request_user(request)
    users = list_mentionable_users()
    return [with_mentions(message, users) for message in get_recent_messages()]

@app.get("/api/search")
async def api_search_messages(request: Request, q: str, scope: str = "current",
                              conversation_type: Optional[str] = None,
                              conversation_id: Optional[str] = None, limit: int = 50):
    user = request_user(request)
    query = q.strip()
    if len(query) < 2:
        raise HTTPException(400, "검색어는 두 글자 이상 입력하세요.")
    if scope not in {"current", "global"}:
        raise HTTPException(400, "유효하지 않은 검색 범위입니다.")
    if scope == "current":
        if conversation_type not in {"channel", "dm"} or not conversation_id:
            raise HTTPException(400, "현재 대화 정보가 필요합니다.")
        if conversation_type == "dm":
            partner = get_user_by_username(conversation_id)
            if not partner or partner["id"] == user["id"]:
                raise HTTPException(404, "대화 상대를 찾을 수 없습니다.")
            conversation_id = str(partner["id"])
    else:
        conversation_type = None
        conversation_id = None
    results = search_conversation_history(user["id"], query,
        is_admin=user["role"] == "admin", conversation_type=conversation_type,
        conversation_id=conversation_id, limit=limit)
    return {"query": query, "scope": scope, "results": results, "count": len(results)}

@app.get("/api/history/public")
async def public_message_history(request: Request, before_id: Optional[int] = None):
    user = request_user(request)
    messages = get_recent_messages(PUBLIC_HISTORY_PAGE_SIZE + 1, before_id, current_user_id=user["id"])
    has_more = len(messages) > PUBLIC_HISTORY_PAGE_SIZE
    messages = messages[-PUBLIC_HISTORY_PAGE_SIZE:]
    users = list_mentionable_users()
    return {"messages": [with_mentions(message, users) for message in messages],
            "has_more": has_more}

@app.get("/api/history/dm/{partner_username}")
async def direct_message_history(partner_username: str, request: Request,
                                 before_id: Optional[int] = None):
    user = request_user(request)
    partner = get_user_by_username(partner_username)
    if not partner or partner["id"] == user["id"]:
        raise HTTPException(404, "대화 상대를 찾을 수 없습니다.")
    messages = get_direct_messages_between(
        user["id"], partner["id"], PUBLIC_HISTORY_PAGE_SIZE + 1, before_id
    )
    has_more = len(messages) > PUBLIC_HISTORY_PAGE_SIZE
    return {"messages": messages[-PUBLIC_HISTORY_PAGE_SIZE:], "has_more": has_more}

@app.get("/api/storage")
async def storage_status(request: Request):
    user=request_user(request)
    status=get_storage_status()
    mine,_=get_upload_usage(user["id"])
    status.update({"attachment_limit_bytes":MAX_TOTAL_UPLOAD_BYTES,
        "user_attachment_bytes":mine,"user_attachment_limit_bytes":MAX_UPLOAD_BYTES_PER_USER})
    ratios = [
        status["database_bytes"] / max(1, status["database_limit_bytes"]),
        status["attachment_bytes"] / max(1, status["attachment_limit_bytes"]),
        status["user_attachment_bytes"] / max(1, status["user_attachment_limit_bytes"]),
    ]
    usage = max(ratios)
    status["warning_level"] = 95 if usage >= .95 else 85 if usage >= .85 else 70 if usage >= .70 else 0
    return status

@app.get("/api/admin/overview")
async def admin_overview(request: Request):
    require_admin(request)
    status = get_storage_status()
    status["attachment_limit_bytes"] = MAX_TOTAL_UPLOAD_BYTES
    status["database_limit_bytes"] = CONFIG.database_limit_bytes
    return {
        "registration_enabled": CONFIG.registration_enabled,
        "storage": status,
        "users": [account_public(user) for user in list_users()],
    }

@app.post("/api/admin/registration")
async def admin_registration(request: Request):
    require_admin(request)
    if not request_origin_is_allowed(request):
        raise HTTPException(403, "허용되지 않은 요청입니다.")
    data = await read_json_body(request)
    if not isinstance(data.get("enabled"), bool):
        raise HTTPException(400, "enabled 값은 true 또는 false여야 합니다.")
    CONFIG.registration_enabled = data["enabled"]
    save_config(CONFIG)
    return {"registration_enabled": CONFIG.registration_enabled}

@app.post("/api/admin/enrollment-code", status_code=204)
async def admin_enrollment_code(request: Request):
    require_admin(request)
    if not request_origin_is_allowed(request):
        raise HTTPException(403, "허용되지 않은 요청입니다.")
    data = await read_json_body(request)
    code = str(data.get("enrollment_code", ""))
    try:
        validate_password(code)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    CONFIG.enrollment_code_hash = await asyncio.to_thread(hash_secret, code)
    save_config(CONFIG)
    return Response(status_code=204)

@app.post("/api/admin/users/{user_id}")
async def admin_update_user(user_id: int, request: Request):
    admin = require_admin(request)
    if not request_origin_is_allowed(request):
        raise HTTPException(403, "허용되지 않은 요청입니다.")
    data = await read_json_body(request)
    target = get_user_by_id(user_id)
    if not target:
        raise HTTPException(404, "사용자를 찾을 수 없습니다.")

    requested_role = data.get("role", target["role"])
    requested_active = data.get("active", bool(target["active"]))
    new_password = data.get("new_password")
    if requested_role not in {"student", "admin"}:
        raise HTTPException(400, "올바른 역할이 아닙니다.")
    if not isinstance(requested_active, bool):
        raise HTTPException(400, "active 값은 true 또는 false여야 합니다.")
    if user_id == admin["id"] and (
        requested_role != target["role"] or requested_active != bool(target["active"])
    ):
        raise HTTPException(400, "현재 로그인한 관리자 자신의 역할이나 상태는 변경할 수 없습니다.")
    removes_active_admin = (
        target["role"] == "admin" and bool(target["active"])
        and (requested_role != "admin" or not requested_active)
    )
    if removes_active_admin and count_active_admins() <= 1:
        raise HTTPException(400, "마지막 활성 관리자는 비활성화하거나 강등할 수 없습니다.")

    password_changed = new_password is not None and str(new_password) != ""
    if password_changed:
        try:
            validate_password(str(new_password))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    if requested_role != target["role"]:
        set_user_role(user_id, requested_role)
    if requested_active != bool(target["active"]):
        set_user_active(user_id, requested_active)
    if password_changed:
        update_password_hash(user_id, await asyncio.to_thread(hash_secret, str(new_password)))
        delete_user_sessions(user_id)

    new_display_name = data.get("display_name")
    if new_display_name is not None and str(new_display_name).strip():
        try:
            validated_name = validate_display_name(str(new_display_name))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        update_display_name(user_id, validated_name)

    if not requested_active or password_changed:
        await disconnect_user(user_id, "계정 설정이 변경되었습니다. 다시 로그인해 주세요.")
    if requested_active != bool(target["active"]):
        await broadcast_users()
    updated = get_user_by_id(user_id)
    return account_public(updated)

@app.post("/api/files",status_code=201)
async def upload_file(request: Request,x_file_name: str=Header("",alias="X-File-Name")):
    if not request_origin_is_allowed(request): raise HTTPException(403,"허용되지 않은 요청입니다.")
    user=request_user(request)
    ip=get_client_ip(request)
    if not sliding_window_allowed(upload_timestamps,str(user["id"]),UPLOAD_RATE_LIMIT,UPLOAD_RATE_WINDOW_SECONDS):
        raise HTTPException(429,"업로드가 너무 빠릅니다.")
    name=clean_original_filename(x_file_name)
    if upload_is_blocked(name): raise HTTPException(415,"실행 파일 또는 활성 웹 파일은 공유할 수 없습니다.")
    length=request.headers.get("content-length","")
    if length.isdigit() and int(length)>MAX_UPLOAD_BYTES: raise HTTPException(413,"파일 크기 제한을 초과했습니다.")
    async with upload_lock:
        mine,total=get_upload_usage(user["id"])
        if mine>=MAX_UPLOAD_BYTES_PER_USER: raise HTTPException(413,"개인 파일 보관 한도를 초과했습니다.")
        if total>=MAX_TOTAL_UPLOAD_BYTES: raise HTTPException(507,"서버 파일 보관 공간이 가득 찼습니다.")
        attachment_id=uuid.uuid4().hex
        stored_name=f"{attachment_id}.upload"
        destination=attachment_path(stored_name)
        digest=hashlib.sha256(); size=0; header=bytearray()
        try:
            with destination.open("xb") as output:
                async for chunk in request.stream():
                    if not chunk: continue
                    size+=len(chunk)
                    if size>MAX_UPLOAD_BYTES: raise HTTPException(413,"파일 크기 제한을 초과했습니다.")
                    if len(header)<16: header.extend(chunk[:16-len(header)])
                    digest.update(chunk); output.write(chunk)
            if not size: raise HTTPException(400,"빈 파일은 공유할 수 없습니다.")
            if mine+size>MAX_UPLOAD_BYTES_PER_USER: raise HTTPException(413,"개인 파일 보관 한도를 초과했습니다.")
            if total+size>MAX_TOTAL_UPLOAD_BYTES: raise HTTPException(507,"서버 파일 보관 공간이 가득 찼습니다.")
            preview=detect_preview_type(bytes(header))
            attachment=save_attachment({"id":attachment_id,"original_name":name,"stored_name":stored_name,
                "size":size,"sha256":digest.hexdigest(),"content_type":preview or "application/octet-stream",
                "previewable":bool(preview),"uploader_nickname":user["username"],"uploader_user_id":user["id"],"ip":ip})
        except Exception:
            destination.unlink(missing_ok=True)
            raise
    logger.info("Upload id=%s user=%s ip=%s size=%s",attachment_id,user["id"],ip,size)
    return attachment

@app.delete("/api/files/{attachment_id}",status_code=204)
async def discard_file(attachment_id: str,request: Request):
    if not request_origin_is_allowed(request): raise HTTPException(403,"허용되지 않은 요청입니다.")
    user=request_user(request)
    deleted=delete_owned_attachment(attachment_id,user["id"],user["role"]=="admin")
    if not deleted: raise HTTPException(404,"삭제할 수 있는 파일이 없습니다.")
    remove_stored_file(deleted["stored_name"])
    await broadcast({"type":"attachment_deleted","attachment_id":attachment_id})

@app.get("/api/files/{attachment_id}")
async def download_file(attachment_id: str,request: Request):
    user=request_user(request)
    record=get_attachment_record(attachment_id)
    if not record: raise HTTPException(404,"파일을 찾을 수 없습니다.")
    if not attachment_is_visible_to_user(attachment_id,user["id"]):
        raise HTTPException(404,"파일을 찾을 수 없습니다.")
    path=attachment_path(record["stored_name"])
    if not path.is_file(): raise HTTPException(404,"파일을 찾을 수 없습니다.")
    preview=bool(record["previewable"])
    return FileResponse(path,media_type=record["content_type"] if preview else "application/octet-stream",
        filename=record["original_name"],content_disposition_type="inline" if preview else "attachment",
        headers={"Cache-Control":"private, no-store"})


# ==========================================
# Educational Quiz & Leaderboard Endpoints
# ==========================================

@app.get("/api/quiz/today")
async def api_quiz_today(
    request: Request,
    category: Optional[str] = None,
    count: int = 5,
    offset: int = 0,
    exclude: Optional[str] = None,
):
    user = request_user(request)
    count = max(1, min(int(count), 200))
    offset = max(0, int(offset))
    exclude_ids = []
    if exclude:
        try:
            exclude_ids = [int(value) for value in exclude.split(",") if value.strip()]
        except ValueError:
            raise HTTPException(400, "exclude 문항 ID 형식이 올바르지 않습니다.")
    quizzes = get_daily_quizzes(user["id"], count=count, category=category, offset=offset, exclude_ids=exclude_ids)
    stats = get_user_quiz_stats(user["id"])
    return {
        "quizzes": quizzes,
        "stats": stats,
        "category": category or "daily",
    }


@app.get("/api/quiz/categories")
async def api_quiz_categories(request: Request):
    request_user(request)
    return {
        "categories": get_quiz_categories_summary(),
    }


@app.get("/api/quiz/sidebar-counts")
async def api_quiz_sidebar_counts(request: Request):
    user = request_user(request)
    return get_quiz_sidebar_counts(user["id"])



@app.post("/api/quiz/submit")
async def api_quiz_submit(request: Request):
    if not request_origin_is_allowed(request):
        raise HTTPException(403, "허용되지 않은 요청입니다.")
    user = request_user(request)
    data = await read_json_body(request)
    try:
        quiz_id = int(data.get("quiz_id", 0))
    except (ValueError, TypeError):
        raise HTTPException(400, "올바른 퀴즈 ID가 필요합니다.")
    user_answer = str(data.get("answer", "")).strip()
    if not user_answer:
        raise HTTPException(400, "답안을 입력하세요.")
    try:
        result = submit_quiz_answer(user["id"], quiz_id, user_answer)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    await broadcast_users()
    await broadcast({
        "type": "quiz_leaderboard_updated",
        "user_id": user["id"],
    })
    return result


@app.post("/api/quiz/retry")
async def api_quiz_retry(request: Request):
    if not request_origin_is_allowed(request):
        raise HTTPException(403, "허용되지 않은 요청입니다.")
    user = request_user(request)
    data = await read_json_body(request)
    try:
        quiz_id = int(data.get("quiz_id", 0))
    except (ValueError, TypeError):
        raise HTTPException(400, "올바른 퀴즈 ID가 필요합니다.")
    user_answer = str(data.get("answer", "")).strip()
    if not user_answer:
        raise HTTPException(400, "답안을 입력하세요.")
    try:
        result = retry_quiz_answer(user["id"], quiz_id, user_answer)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return result


@app.post("/api/quiz/bookmark/{quiz_id}")
async def api_quiz_bookmark(quiz_id: int, request: Request):
    if not request_origin_is_allowed(request):
        raise HTTPException(403, "허용되지 않은 요청입니다.")
    user = request_user(request)
    is_starred = toggle_quiz_bookmark(user["id"], quiz_id)
    return {"status": "ok", "quiz_id": quiz_id, "is_starred": is_starred}


@app.get("/api/quiz/review")
async def api_quiz_review(request: Request, mode: str = "wrong"):
    user = request_user(request)
    if mode not in {"wrong", "starred", "history"}:
        mode = "wrong"
    quizzes = get_quiz_review_list(user["id"], mode=mode)
    return {"mode": mode, "quizzes": quizzes, "count": len(quizzes)}


@app.get("/api/quiz/leaderboard")
async def api_quiz_leaderboard(request: Request, period: str = "weekly"):
    request_user(request)
    if period not in {"daily", "weekly", "all", "streak"}:
        period = "weekly"
    leaderboard = get_quiz_leaderboard(period)
    return {
        "period": period,
        "leaderboard": leaderboard,
    }


@app.get("/api/quiz/stats")
async def api_quiz_stats(request: Request):
    user = request_user(request)
    return get_user_quiz_stats(user["id"])

@app.get("/api/quiz/expertises")
async def api_quiz_expertises(request: Request):
    request_user(request)
    return {"expertises": list(QUIZ_EXPERTISES)}

@app.get("/api/quiz/my-sets")
async def api_my_quiz_sets(request: Request):
    user=request_user(request)
    return {"sets": list_user_quiz_sets(owner_user_id=user["id"])}

@app.post("/api/quiz/my-sets/validate")
async def api_validate_my_quiz_set(request: Request):
    if not request_origin_is_allowed(request):
        raise HTTPException(403, "허용되지 않은 요청입니다.")
    request_user(request)
    data = await read_json_body(request)
    try:
        return analyze_quiz_set(data.get("quizzes"), str(data.get("expertise", "")))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

@app.post("/api/quiz/my-sets", status_code=201)
async def api_create_my_quiz_set(request: Request):
    if not request_origin_is_allowed(request): raise HTTPException(403, "허용되지 않은 요청입니다.")
    user=request_user(request); data=await read_json_body(request)
    try:
        created=create_user_quiz_set(user["id"], str(data.get("expertise", "")), str(data.get("title", "")), data.get("quizzes"))
    except ValueError as exc: raise HTTPException(400, str(exc)) from exc
    return created

@app.patch("/api/quiz/my-sets/{set_id}")
async def api_update_my_quiz_set(set_id: int, request: Request):
    if not request_origin_is_allowed(request): raise HTTPException(403, "허용되지 않은 요청입니다.")
    user=request_user(request); data=await read_json_body(request)
    try:
        updated=update_user_quiz_set(set_id, user["id"], str(data.get("expertise", "")), str(data.get("title", "")), data.get("quizzes"))
    except ValueError as exc: raise HTTPException(400, str(exc)) from exc
    if not updated: raise HTTPException(409, "초안 또는 반려된 문제집만 수정할 수 있습니다.")
    return updated

@app.post("/api/quiz/my-sets/{set_id}/submit")
async def api_submit_my_quiz_set(set_id: int, request: Request):
    if not request_origin_is_allowed(request): raise HTTPException(403, "허용되지 않은 요청입니다.")
    user=request_user(request)
    if not submit_user_quiz_set(set_id, user["id"]): raise HTTPException(409, "제출할 수 없는 문제집입니다.")
    return {"status":"pending_review", "set_id":set_id}

@app.get("/api/admin/quiz/submissions")
async def api_admin_quiz_submissions(request: Request):
    require_admin(request)
    return {"sets": list_user_quiz_sets(status="pending_review")}

@app.post("/api/admin/quiz/submissions/{set_id}/review")
async def api_admin_review_quiz_submission(set_id: int, request: Request):
    if not request_origin_is_allowed(request): raise HTTPException(403, "허용되지 않은 요청입니다.")
    admin=require_admin(request); data=await read_json_body(request); approve=data.get("approve"); note=str(data.get("note", ""))
    if not isinstance(approve, bool):
        raise HTTPException(400, "approve는 true 또는 false여야 합니다.")
    try:
        created_ids=review_user_quiz_set(
            set_id, admin["id"], approve, note,
            items=data.get("quizzes"), expertise=data.get("expertise"), title=data.get("title"),
        )
    except ValueError as exc: raise HTTPException(409, str(exc)) from exc
    return {"status":"approved" if approve else "rejected", "created_ids":created_ids}

@app.patch("/api/admin/quiz/submissions/{set_id}")
async def api_admin_update_quiz_submission(set_id: int, request: Request):
    if not request_origin_is_allowed(request):
        raise HTTPException(403, "허용되지 않은 요청입니다.")
    require_admin(request)
    data = await read_json_body(request)
    try:
        updated = update_pending_user_quiz_set(
            set_id, str(data.get("expertise", "")), str(data.get("title", "")), data.get("quizzes"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"status": "ok", "set": updated}

@app.post("/api/admin/quiz/daily-sets")
async def api_admin_assign_daily_set(request: Request):
    if not request_origin_is_allowed(request): raise HTTPException(403, "허용되지 않은 요청입니다.")
    admin=require_admin(request); data=await read_json_body(request)
    assigned_date=str(data.get("assigned_date", "")).strip(); quiz_ids=data.get("quiz_ids", [])
    try:
        if not isinstance(quiz_ids, list): raise ValueError("quiz_ids는 배열이어야 합니다.")
        date.fromisoformat(assigned_date); ids=[int(value) for value in quiz_ids]
        set_id=assign_daily_quizzes(assigned_date, ids, admin["id"])
    except (ValueError, TypeError) as exc: raise HTTPException(400, str(exc)) from exc
    return {"status":"published", "set_id":set_id, "assigned_date":assigned_date}


@app.post("/api/admin/quiz/ai-generate")
async def api_admin_quiz_ai_generate(request: Request):
    if not request_origin_is_allowed(request):
        raise HTTPException(403, "허용되지 않은 요청입니다.")
    admin = require_admin(request)
    api_key = os.getenv("GEMINI_API_KEY", "") or getattr(CONFIG, "gemini_api_key", "")
    if not api_key:
        raise HTTPException(400, "GEMINI_API_KEY 환경변수 또는 설정에 Gemini API 키가 등록되지 않았습니다.")

    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        category = str(form.get("category", "PLC")).strip()
        count = int(form.get("count", 5))
        file_obj = form.get("file")
        text_content = str(form.get("text_content", "")).strip()

        if file_obj and hasattr(file_obj, "read"):
            filename = getattr(file_obj, "filename", "document.pdf")
            file_bytes = await file_obj.read()
            is_pdf = filename.lower().endswith(".pdf")
        elif text_content:
            filename = "notes.txt"
            file_bytes = text_content.encode("utf-8")
            is_pdf = False
        else:
            raise HTTPException(400, "파일 또는 텍스트 내용을 입력하세요.")
    else:
        data = await read_json_body(request)
        category = str(data.get("category", "PLC")).strip()
        count = int(data.get("count", 5))
        text_content = str(data.get("text_content", "")).strip()
        if not text_content:
            raise HTTPException(400, "텍스트 내용을 입력하세요.")
        filename = "notes.txt"
        file_bytes = text_content.encode("utf-8")
        is_pdf = False

    sha256 = hashlib.sha256(file_bytes).hexdigest()
    stored_name = f"doc_{uuid.uuid4().hex}_{filename}"
    doc_path = QUIZ_SOURCES_DIR / stored_name
    doc_path.write_bytes(file_bytes)
    doc_id = save_quiz_source_document(
        filename=filename,
        stored_filename=stored_name,
        file_type="pdf" if is_pdf else "txt",
        sha256=sha256,
        size=len(file_bytes),
        uploaded_by_user_id=admin["id"]
    )

    try:
        quizzes = await asyncio.to_thread(
            generate_quizzes_with_gemini,
            api_key,
            file_bytes,
            is_pdf=is_pdf,
            count=count,
            category=category,
        )
    except Exception as exc:
        raise HTTPException(500, f"AI 퀴즈 생성 중 오류 발생: {exc}") from exc

    created_ids = create_quiz_batch(quizzes, source_doc_id=doc_id)
    return {
        "status": "ok",
        "created_count": len(created_ids),
        "source_doc_id": doc_id,
        "quizzes": quizzes,
    }


@app.post("/api/admin/quiz/import-json")
async def api_admin_quiz_import_json(request: Request):
    if not request_origin_is_allowed(request):
        raise HTTPException(403, "허용되지 않은 요청입니다.")
    require_admin(request)
    data = await read_json_body(request)
    raw_quizzes = data.get("quizzes", [])
    if isinstance(raw_quizzes, str):
        try:
            raw_quizzes = json.loads(raw_quizzes)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "올바른 JSON 배열 형식이어야 합니다.") from exc
    if not isinstance(raw_quizzes, list) or not raw_quizzes:
        raise HTTPException(400, "퀴즈 목록(배열)이 비어 있거나 올바르지 않습니다.")

    created_ids = create_quiz_batch(raw_quizzes)
    return {"status": "ok", "created_count": len(created_ids), "ids": created_ids}


@app.get("/api/admin/quiz/list")
async def api_admin_quiz_list(request: Request):
    require_admin(request)
    return {
        "quizzes": get_all_quizzes_admin(),
        "source_documents": get_quiz_source_documents(),
    }

@app.post("/api/admin/quiz")
async def api_admin_quiz_create(request: Request):
    if not request_origin_is_allowed(request):
        raise HTTPException(403, "허용되지 않은 요청입니다.")
    require_admin(request)
    data = await read_json_body(request)
    category = str(data.get("category", "")).strip()
    if not category or len(category) > 80:
        raise HTTPException(400, "과목은 1~80자여야 합니다.")
    try:
        normalized = normalize_quiz_import([data], "PLC")[0]
        normalized["category"] = category
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    created_ids = create_quiz_batch([normalized])
    return {"status": "ok", "quiz_id": created_ids[0]}

@app.patch("/api/admin/quiz/{quiz_id}")
async def api_admin_quiz_update(quiz_id: int, request: Request):
    if not request_origin_is_allowed(request):
        raise HTTPException(403, "허용되지 않은 요청입니다.")
    require_admin(request)
    data = await read_json_body(request)
    try:
        updated = update_quiz(quiz_id, data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not updated:
        raise HTTPException(404, "퀴즈를 찾을 수 없습니다.")
    return {"status": "ok", "quiz": updated}


@app.delete("/api/admin/quiz/{quiz_id}")
async def api_admin_quiz_delete(quiz_id: int, request: Request):
    if not request_origin_is_allowed(request):
        raise HTTPException(403, "허용되지 않은 요청입니다.")
    require_admin(request)
    success = delete_quiz(quiz_id)
    if not success:
        raise HTTPException(404, "퀴즈를 찾을 수 없습니다.")
    return {"status": "ok", "deleted_quiz_id": quiz_id}


@app.get("/api/quiz/images/{filename}")
async def api_quiz_image(filename: str, request: Request):
    request_user(request)
    safe_name = Path(filename).name
    img_path = QUIZ_IMAGES_DIR / safe_name
    if not img_path.exists() or not img_path.is_file():
        raise HTTPException(404, "이미지를 찾을 수 없습니다.")
    return FileResponse(img_path)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    ip=get_client_ip(ws)
    if not websocket_origin_is_allowed(ws):
        await ws.close(code=1008,reason="허용되지 않은 WebSocket origin입니다."); return
    user=session_user_from_token(ws.cookies.get(SESSION_COOKIE,""))
    if not user:
        await ws.close(code=1008,reason="로그인이 필요합니다."); return
    reason=connection_limit_reason(ip)
    if reason:
        await ws.close(code=1013,reason=reason); return
    await ws.accept()
    info=ClientInfo(user["id"],user["username"],user["role"],ip)
    connected_clients[ws]=info; user_registry[info.username].add(ws)
    logger.info("Connected user=%s ip=%s",info.user_id,ip)
    await broadcast_presence(); await broadcast_users()
    users = list_mentionable_users()
    public_candidates = get_recent_messages(PUBLIC_HISTORY_PAGE_SIZE + 1)
    public_history = public_candidates[-PUBLIC_HISTORY_PAGE_SIZE:]
    for message in public_history:
        await ws.send_text(json.dumps(
            {"type":"chat", "history":True, **with_mentions(message, users)}, ensure_ascii=False
        ))
    dm_candidates = get_recent_direct_messages(info.user_id, DM_HISTORY_PAGE_SIZE + 1)
    dm_groups: Dict[str, list[dict]] = defaultdict(list)
    for message in dm_candidates:
        partner = message["to_nick"] if message["from_user_id"] == info.user_id else message["from_nick"]
        dm_groups[partner].append(message)
    selected_dm_ids = {
        message["message_id"]
        for messages in dm_groups.values()
        for message in messages[-DM_HISTORY_PAGE_SIZE:]
    }
    dm_history = [message for message in dm_candidates
                  if message["message_id"] in selected_dm_ids]
    for message in dm_history:
        await ws.send_text(json.dumps({"type":"dm", "history":True, **message}, ensure_ascii=False))
    states = get_user_conversation_states(info.user_id)
    unread_counts = get_user_unread_counts(info.user_id)
    await ws.send_text(json.dumps({
        "type":"history_ready",
        "public_has_older":len(public_candidates) > PUBLIC_HISTORY_PAGE_SIZE,
        "dm_has_older":{partner: len(messages) > DM_HISTORY_PAGE_SIZE
                        for partner, messages in dm_groups.items()},
        "read_states": states,
        "unread_counts": unread_counts,
    },ensure_ascii=False))
    try:
        while True:
            raw=await ws.receive_text()
            if len(raw)>MAX_RAW_MESSAGE_LEN:
                await ws.close(code=1009,reason="메시지 프레임이 너무 큽니다."); break
            if not sliding_window_allowed(message_timestamps,str(info.user_id),RATE_LIMIT_MESSAGES,RATE_LIMIT_WINDOW_SECONDS):
                await ws.send_text(json.dumps({"type":"error","message":"메시지를 너무 빠르게 보내고 있습니다."},ensure_ascii=False))
                await ws.close(code=1008); break
            try: data=json.loads(raw)
            except json.JSONDecodeError: continue
            if not isinstance(data,dict): continue
            content=str(data.get("content","")).strip()
            values=data.get("attachment_ids",[])
            if not isinstance(values,list): values=[data.get("attachment_id","")]
            attachment_ids=[]
            for value in values:
                item=str(value).strip()
                if item and item not in attachment_ids: attachment_ids.append(item)
            if len(attachment_ids)>MAX_ATTACHMENTS_PER_MESSAGE:
                await ws.send_text(json.dumps({"type":"error","message":f"파일은 최대 {MAX_ATTACHMENTS_PER_MESSAGE}개까지 첨부할 수 있습니다."},ensure_ascii=False)); continue
            if not content and not attachment_ids: continue
            if len(content)>MAX_CONTENT_LEN:
                await ws.send_text(json.dumps({"type":"error","message":f"메시지는 {MAX_CONTENT_LEN}자 이하여야 합니다."},ensure_ascii=False)); continue
            msg_type = data.get("type")
            if msg_type not in {"chat", "dm"}:
                continue
            target_user=None; targets=[]
            if msg_type=="dm":
                target_user=get_user_by_username(str(data.get("to","")).strip())
                if not target_user or not target_user["active"] or target_user["id"]==info.user_id:
                    await ws.send_text(json.dumps({"type":"error","message":"DM 대상을 찾을 수 없습니다."},ensure_ascii=False)); continue
                targets=list(user_registry.get(target_user["username"],set()))
            attachments=claim_attachments(attachment_ids,info.user_id)
            if attachments is None:
                await ws.send_text(json.dumps({"type":"error","message":"첨부 파일을 사용할 수 없거나 이미 전송했습니다."},ensure_ascii=False)); continue
            reply=clean_reply(data.get("reply"))
            if msg_type=="chat":
                raw_channel_id = data.get("channel_id", 1)
                try:
                    channel_id = int(raw_channel_id)
                except (ValueError, TypeError):
                    channel_id = 1
                chan = get_channel_by_id(channel_id)
                if not chan:
                    await ws.send_text(json.dumps({"type":"error","message":"채널을 찾을 수 없습니다."},ensure_ascii=False))
                    continue
                if chan.get("archived"):
                    await ws.send_text(json.dumps({"type":"error","message":"보관된 채널에는 메시지를 작성할 수 없습니다."},ensure_ascii=False))
                    continue
                saved=save_message(info.username,content,ip=ip,reply=reply,
                    attachment_ids=[a["id"] for a in attachments],user_id=info.user_id,
                    channel_id=channel_id)
                saved.pop("ip",None)
                saved=with_mentions(saved)
                await broadcast({"type":"chat",**saved})
            elif msg_type=="dm":
                dm_res = save_direct_message(
                    user,target_user,content,reply=reply,
                    attachment_ids=[a["id"] for a in attachments])
                dm_res["quiz_badge"] = get_user_quiz_badge(info.user_id)
                payload={"type":"dm",**dm_res}
                encoded=json.dumps(payload,ensure_ascii=False)
                sender_targets=list(user_registry.get(info.username,set()))
                for target_ws in set(targets+sender_targets):
                    try: await target_ws.send_text(encoded)
                    except Exception: pass
                await send_to_user_id(target_user["id"], {
                    "type": "read_state_updated",
                    "unread_counts": get_user_unread_counts(target_user["id"]),
                })
    except WebSocketDisconnect:
        pass
    finally:
        _remove_client(ws)
        await broadcast_presence(); await broadcast_users()
        logger.info("Disconnected user=%s ip=%s",info.user_id,ip)

@app.websocket("/ws/chess")
async def chess_websocket_endpoint(ws: WebSocket):
    ip = get_client_ip(ws)
    if not websocket_origin_is_allowed(ws):
        await ws.close(code=1008, reason="허용되지 않은 WebSocket origin입니다.")
        return
    user = session_user_from_token(ws.cookies.get(SESSION_COOKIE, ""))
    if not user:
        await ws.close(code=1008, reason="로그인이 필요합니다.")
        return
    await ws.accept()
    chess_manager.register_client(ws, user)
    await ws.send_text(json.dumps({"type": "lobby_update", "rooms": chess_manager.get_lobby_summary()}, ensure_ascii=False))

    try:
        while True:
            raw = await ws.receive_text()
            if len(raw) > MAX_RAW_MESSAGE_LEN:
                await ws.close(code=1009, reason="메시지 프레임이 너무 큽니다.")
                break
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue

            action = data.get("action")
            room_id = data.get("room_id")

            if action == "list_rooms":
                await ws.send_text(json.dumps({"type": "lobby_update", "rooms": chess_manager.get_lobby_summary()}, ensure_ascii=False))
            elif action == "create_room":
                await chess_manager.create_room(ws, user, data.get("title", ""), data.get("time_minutes", 10))
            elif action == "join_room" and room_id:
                await chess_manager.join_room(ws, user, room_id, data.get("role_pref"))
            elif action == "leave_room" and room_id:
                await chess_manager.leave_room(ws, user, room_id)
            elif action == "pick_role" and room_id:
                await chess_manager.pick_role(user, room_id, data.get("role", "spectator"))
            elif action == "start_game" and room_id:
                await chess_manager.start_game(user, room_id)
            elif action == "move" and room_id:
                await chess_manager.make_move(user, room_id, data)
            elif action == "offer_draw" and room_id:
                await chess_manager.offer_draw(user, room_id)
            elif action == "respond_draw" and room_id:
                await chess_manager.respond_draw(user, room_id, bool(data.get("accept")))
            elif action == "resign" and room_id:
                await chess_manager.resign(user, room_id)
            elif action == "join_queue" and room_id:
                await chess_manager.join_match_queue(user, room_id)
            elif action == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        pass
    finally:
        await chess_manager.unregister_client(ws)
