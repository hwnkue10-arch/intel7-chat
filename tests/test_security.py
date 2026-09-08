import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import database, main
from app.auth import hash_secret, token_hash
from app.config import GIB, RoomConfig, load_config, save_config

ORIGIN = {"origin": "http://testserver"}

@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "chat.db")
    monkeypatch.setattr(database, "DB_MAX_BYTES", 3 * GIB)
    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setenv("BAMBOOCHAT_CONFIG", str(tmp_path / "bamboochat.json"))
    monkeypatch.setattr(main.CONFIG, "registration_enabled", True)
    monkeypatch.setattr(main.CONFIG, "enrollment_code_hash", "")
    main.connected_clients.clear()
    main.user_registry.clear()
    main.message_timestamps.clear()
    main.upload_timestamps.clear()
    main.login_timestamps.clear()
    main.UPLOAD_DIR.mkdir(parents=True)
    database.init_db()
    yield

def session_client(username="student", role="student"):
    user = database.create_user(username, "not-used-in-session-tests", role=role)
    raw = f"session-token-for-user-{user['id']}"
    expires = "2999-01-01T00:00:00Z"
    database.create_session(token_hash(raw), user["id"], expires)
    client = TestClient(main.app)
    client.cookies.set(main.SESSION_COOKIE, raw)
    return client, user

def upload(client, filename, body=b"PLC ladder bytes"):
    return client.post("/api/files", content=body,
        headers={**ORIGIN, "X-File-Name": quote(filename), "Content-Type": "application/octet-stream"})

def test_host_allowlist_accepts_lan_and_rejects_public_dns():
    assert main.host_is_allowed("localhost:8000")
    assert main.host_is_allowed("192.168.0.72:8000")
    assert main.host_is_allowed("bamboochat.local:8000")
    assert not main.host_is_allowed("example.com")

def test_page_has_security_headers_and_no_api_docs():
    client = TestClient(main.app)
    response = client.get("/")
    assert response.status_code == 200
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "암호화되지 않습니다" in response.text
    assert client.get("/docs").status_code == 404

def test_protected_http_routes_require_login():
    client = TestClient(main.app)
    assert client.get("/api/messages").status_code == 401
    assert client.get("/api/storage").status_code == 401
    assert client.get("/api/files/missing").status_code == 401
    assert client.get("/api/search?q=test&scope=global").status_code == 401
    assert client.post("/api/files", content=b"x", headers={**ORIGIN, "X-File-Name": "x.gwx"}).status_code == 401

def test_registration_requires_correct_enrollment_code(monkeypatch):
    monkeypatch.setattr(main.CONFIG, "registration_enabled", True)
    monkeypatch.setattr(main.CONFIG, "enrollment_code_hash", hash_secret("intel7-7777"))
    client = TestClient(main.app)
    bad = client.post("/api/auth/register", headers=ORIGIN,
        json={"username": "Ronaldo", "password": "class-pass-1", "enrollment_code": "wrong-code"})
    assert bad.status_code == 403
    good = client.post("/api/auth/register", headers=ORIGIN,
        json={"username": "Ronaldo", "password": "class-pass-1", "enrollment_code": "intel7-7777"})
    assert good.status_code == 201
    assert good.json()["username"] == "Ronaldo"
    assert main.SESSION_COOKIE in good.cookies
    duplicate = TestClient(main.app).post("/api/auth/register", headers=ORIGIN,
        json={"username": "ronaldo", "password": "another-pass", "enrollment_code": "intel7-7777"})
    assert duplicate.status_code == 409

def test_login_cookie_and_logout():
    database.create_user("student1", hash_secret("private-pass"))
    client = TestClient(main.app)
    response = client.post("/api/auth/login", headers=ORIGIN,
        json={"username": "STUDENT1", "password": "private-pass"})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert "secure" not in cookie
    assert client.get("/api/auth/me").json()["username"] == "student1"
    assert client.post("/api/auth/logout", headers=ORIGIN).status_code == 204
    assert client.get("/api/auth/me").status_code == 401

def test_inactive_account_login_explains_that_admin_help_is_needed():
    user = database.create_user("inactive-user", hash_secret("private-pass"))
    database.set_user_active(user["id"], False)
    client = TestClient(main.app)
    inactive = client.post("/api/auth/login", headers=ORIGIN,
        json={"username": "inactive-user", "password": "private-pass"})
    assert inactive.status_code == 401
    assert inactive.json()["detail"] == "이 아이디는 비활성화되어 있습니다. 관리자에게 문의하세요."
    wrong_password = client.post("/api/auth/login", headers=ORIGIN,
        json={"username": "inactive-user", "password": "wrong-pass"})
    assert wrong_password.status_code == 401
    assert "비활성화" not in wrong_password.json()["detail"]

def test_state_changes_reject_cross_origin():
    client, _ = session_client()
    assert client.post("/api/auth/logout", headers={"origin": "http://evil.example"}).status_code == 403
    assert upload(client, "ladder.gwx").status_code == 201
    attachment_id = upload(client, "second.gwx").json()["id"]
    assert client.delete(f"/api/files/{attachment_id}", headers={"origin": "http://evil.example"}).status_code == 403

def test_websocket_requires_session_and_same_origin():
    client = TestClient(main.app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers={"origin": "http://testserver"}):
            pass
    authenticated, _ = session_client()
    with pytest.raises(WebSocketDisconnect):
        with authenticated.websocket_connect("/ws", headers={"origin": "http://evil.example"}):
            pass

def test_public_chat_is_persistent_and_exposes_no_ip_suffix():
    client, user = session_client("홍길동")
    with client.websocket_connect("/ws", headers=ORIGIN) as ws:
        ws.receive_json()  # presence
        ws.receive_json()  # users
        ws.send_json({"type": "chat", "content": "**수업 자료**"})
        message = ws.receive_json()
        while message.get("type") != "chat":
            message = ws.receive_json()
    assert message["nickname"] == "홍길동"
    assert message["author_id"] == user["id"]
    assert "ip" not in message and "ip_suffix" not in message
    with database.get_connection() as conn:
        conn.execute("UPDATE messages SET created_at='2020-01-01T00:00:00Z'")
        conn.commit()
    history = client.get("/api/messages").json()
    assert history[0]["content"] == "**수업 자료**"

def test_public_mentions_are_validated_and_restored_in_history():
    target = database.create_user("target-user", "unused")
    inactive = database.create_user("inactive-target", "unused")
    database.set_user_active(inactive["id"], False)
    sender, _ = session_client("mention-sender")
    with sender.websocket_connect("/ws", headers=ORIGIN) as ws:
        ws.receive_json()
        users_event = ws.receive_json()
        mention_users = {user["username"]: user for user in users_event["mention_list"]}
        assert set(mention_users) >= {
            "target-user", "mention-sender"
        }
        assert "inactive-target" not in mention_users
        assert mention_users["target-user"]["online"] is False
        assert mention_users["mention-sender"]["online"] is True
        ws.send_json({
            "type": "chat",
            "content": "@mention-sender @TARGET-USER 확인 부탁해요",
        })
        message = ws.receive_json()
        while message.get("type") != "chat":
            message = ws.receive_json()
    assert message["mentioned_user_ids"] == [target["id"]]
    assert message["mentions"] == [{"user_id": target["id"], "username": "target-user",
                                     "display_name": "target-user"}]
    history = sender.get("/api/messages").json()
    assert history[-1]["mentioned_user_ids"] == [target["id"]]
    assert main.find_mentions("mail@target-user.invalid @target-user2", [target]) == []

def test_direct_messages_are_saved_and_restored_after_reconnect():
    recipient, _ = session_client("dm-recipient")
    sender, _ = session_client("dm-sender")
    with recipient.websocket_connect("/ws", headers=ORIGIN):
        with sender.websocket_connect("/ws", headers=ORIGIN) as sender_ws:
            sender_ws.receive_json(); sender_ws.receive_json()
            sender_ws.send_json({"type": "dm", "to": "dm-recipient", "content": "영구 DM"})
            live = sender_ws.receive_json()
            while live.get("type") != "dm":
                live = sender_ws.receive_json()
            assert live["message_id"].startswith("dm:")
            assert live["content"] == "영구 DM"

    with recipient.websocket_connect("/ws", headers=ORIGIN) as reconnected:
        assert reconnected.receive_json()["type"] == "presence"
        assert reconnected.receive_json()["type"] == "users"
        history = reconnected.receive_json()
        assert history["type"] == "dm"
        assert history["history"] is True
        assert history["message_id"] == live["message_id"]
        assert history["from_nick"] == "dm-sender"

def test_direct_message_attachments_are_visible_only_to_participants():
    sender, sender_user = session_client("dm-file-sender")
    recipient, recipient_user = session_client("dm-file-recipient")
    outsider, _ = session_client("dm-file-outsider")
    attachment = upload(sender, "private.pdf", b"private dm file").json()
    assert database.claim_attachments([attachment["id"]], sender_user["id"])
    database.save_direct_message(sender_user, recipient_user, "첨부", attachment_ids=[attachment["id"]])

    assert sender.get(attachment["url"]).status_code == 200
    assert recipient.get(attachment["url"]).status_code == 200
    assert outsider.get(attachment["url"]).status_code == 404
    assert database.get_recent_direct_messages(outsider.get("/api/auth/me").json()["id"]) == []

def test_search_current_and_global_history_includes_attachment_names():
    alice, alice_user = session_client("search-alice")
    bob, bob_user = session_client("search-bob")
    outsider, _ = session_client("search-outsider")
    attachment = upload(alice, "ladder-diagram.pdf", b"diagram").json()
    assert database.claim_attachments([attachment["id"]], alice_user["id"])
    database.save_message("search-alice", "공개 제어 회로", user_id=alice_user["id"],
                          attachment_ids=[attachment["id"]], channel_id=1)
    database.save_direct_message(alice_user, bob_user, "private calibration note")

    current = alice.get("/api/search", params={"q": "ladder", "scope": "current",
        "conversation_type": "channel", "conversation_id": "1"})
    assert current.status_code == 200
    assert current.json()["results"][0]["attachments"][0]["name"] == "ladder-diagram.pdf"

    bob_global = bob.get("/api/search", params={"q": "calibration", "scope": "global"}).json()
    assert any(result["message_type"] == "dm" for result in bob_global["results"])
    outsider_global = outsider.get("/api/search", params={"q": "calibration", "scope": "global"}).json()
    assert outsider_global["results"] == []
    assert alice.get("/api/search", params={"q": "__", "scope": "global"}).json()["results"] == []

def test_search_hides_moderated_messages_from_students():
    student, student_user = session_client("search-student")
    admin, _ = session_client("search-admin", role="admin")
    message = database.save_message("search-student", "hidden searchable phrase", user_id=student_user["id"])
    database.set_message_hidden(int(message["message_id"].split(":")[1]), True)

    params = {"q": "searchable", "scope": "global"}
    assert student.get("/api/search", params=params).json()["results"] == []
    assert len(admin.get("/api/search", params=params).json()["results"]) == 1

def test_public_and_direct_history_can_load_older_pages():
    viewer, viewer_user = session_client("history-viewer")
    _, partner_user = session_client("history-partner")
    outsider, _ = session_client("history-outsider")
    for index in range(55):
        database.save_message("history-viewer", f"public-{index}", user_id=viewer_user["id"])
        database.save_direct_message(
            viewer_user, partner_user, f"dm-{index}"
        )

    public_page = viewer.get("/api/history/public").json()
    assert len(public_page["messages"]) == 50
    assert public_page["has_more"] is True
    assert public_page["messages"][0]["content"] == "public-5"
    public_before = int(public_page["messages"][0]["message_id"].split(":")[1])
    older_public = viewer.get(f"/api/history/public?before_id={public_before}").json()
    assert [message["content"] for message in older_public["messages"]] == [
        f"public-{index}" for index in range(5)
    ]
    assert older_public["has_more"] is False

    dm_page = viewer.get("/api/history/dm/history-partner").json()
    assert len(dm_page["messages"]) == 50
    assert dm_page["has_more"] is True
    assert dm_page["messages"][0]["content"] == "dm-5"
    dm_before = int(dm_page["messages"][0]["message_id"].split(":")[1])
    older_dm = viewer.get(f"/api/history/dm/history-partner?before_id={dm_before}").json()
    assert [message["content"] for message in older_dm["messages"]] == [
        f"dm-{index}" for index in range(5)
    ]
    assert older_dm["has_more"] is False
    assert outsider.get("/api/history/dm/history-partner").json()["messages"] == []

@pytest.mark.parametrize("filename", ["machine-config.vendorx", "ladder.gwx", "slides.pptx", "lecture.pdf"])
def test_classroom_and_unknown_extensions_are_allowed(filename):
    client, _ = session_client("uploader" + filename.split(".")[0][:8])
    response = upload(client, filename)
    assert response.status_code == 201
    assert response.json()["name"] == filename

@pytest.mark.parametrize("filename", ["malware.exe", "lesson.pdf.cmd", "page.html", "vector.svg"])
def test_active_or_executable_uploads_are_blocked(filename):
    client, _ = session_client("blocked" + filename.split(".")[0][:8])
    assert upload(client, filename).status_code == 415

def test_file_magic_controls_preview_and_download_requires_login():
    client, _ = session_client()
    fake = upload(client, "fake.png", b"not an image")
    assert fake.json()["previewable"] is False
    real = upload(client, "real.bin", b"\x89PNG\r\n\x1a\nrest")
    assert real.json()["previewable"] is True
    assert TestClient(main.app).get(real.json()["url"]).status_code == 401
    downloaded = client.get(real.json()["url"])
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"].startswith("image/png")

def test_account_ownership_works_across_clients_and_blocks_other_accounts():
    owner, owner_user = session_client("owner")
    other, _ = session_client("other")
    attachment = upload(owner, "program.gwx").json()
    assert attachment["owner_id"] == owner_user["id"]
    assert other.delete(f"/api/files/{attachment['id']}", headers=ORIGIN).status_code == 404
    second_owner = TestClient(main.app)
    raw = f"session-token-for-user-{owner_user['id']}"
    second_owner.cookies.set(main.SESSION_COOKIE, raw)
    assert second_owner.delete(f"/api/files/{attachment['id']}", headers=ORIGIN).status_code == 204

def test_admin_can_delete_student_file():
    student, _ = session_client("student2")
    admin, _ = session_client("teacher", role="admin")
    attachment = upload(student, "notes.pdf").json()
    assert admin.delete(f"/api/files/{attachment['id']}", headers=ORIGIN).status_code == 204

def test_sent_file_can_be_deleted_without_removing_message():
    client, _ = session_client()
    attachment = upload(client, "ladder.gwx").json()
    with client.websocket_connect("/ws", headers=ORIGIN) as ws:
        ws.receive_json(); ws.receive_json()
        ws.send_json({"type": "chat", "content": "ladder", "attachment_ids": [attachment["id"]]})
        data = ws.receive_json()
        while data.get("type") != "chat": data = ws.receive_json()
    assert client.delete(f"/api/files/{attachment['id']}", headers=ORIGIN).status_code == 204
    history = client.get("/api/messages").json()
    assert history[-1]["content"] == "ladder"
    assert history[-1]["attachments"][0]["removed"] is True

def test_multi_file_claim_is_ordered_and_atomic():
    client, user = session_client()
    first = upload(client, "one.gwx").json()
    second = upload(client, "two.pdf").json()
    assert database.claim_attachments([first["id"], "missing"], user["id"]) is None
    claimed = database.claim_attachments([second["id"], first["id"]], user["id"])
    assert [item["name"] for item in claimed] == ["two.pdf", "one.gwx"]

def test_upload_caps_block_new_files_without_deleting_existing(monkeypatch):
    client, _ = session_client()
    monkeypatch.setattr(main, "MAX_UPLOAD_BYTES_PER_USER", 5)
    first = upload(client, "tiny.gwx", b"12345")
    assert first.status_code == 201
    second = upload(client, "next.gwx", b"x")
    assert second.status_code == 413
    assert client.get(first.json()["url"]).status_code == 200

def test_storage_status_reports_10gb_3gb_and_user_cap():
    client, _ = session_client(role="admin")
    status = client.get("/api/storage").json()
    assert status["attachment_limit_bytes"] == 10 * GIB
    assert status["database_limit_bytes"] == 3 * GIB
    assert status["user_attachment_limit_bytes"] == 2 * GIB

def test_config_round_trip_contains_hash_not_plaintext(tmp_path):
    path = tmp_path / "room.json"
    config = RoomConfig(server_name="PLC 교실", data_dir=str(tmp_path / "room-data"),
        enrollment_code_hash="$argon2id$stored-hash")
    save_config(config, path)
    loaded = load_config(path)
    assert loaded.server_name == "PLC 교실"
    assert loaded.attachment_limit_bytes == 10 * GIB
    assert loaded.database_limit_bytes == 3 * GIB
    assert "intel7-7777" not in path.read_text(encoding="utf-8")

def test_filename_cleanup_reply_limits_and_sliding_window():
    assert main.clean_original_filename("../../ladder.gwx") == "ladder.gwx"
    reply = main.clean_reply({"nickname": "n" * 100, "content": "x" * 300})
    assert len(reply["nickname"]) == 30 and len(reply["content"]) == main.MAX_REPLY_CONTENT_LEN
    registry = {}
    from collections import defaultdict, deque
    registry = defaultdict(deque)
    assert main.sliding_window_allowed(registry, "user", 2, 10, now=0)
    assert main.sliding_window_allowed(registry, "user", 2, 10, now=1)
    assert not main.sliding_window_allowed(registry, "user", 2, 10, now=2)
    assert main.sliding_window_allowed(registry, "user", 2, 10, now=11)


def test_relaxed_credential_rules_and_unbounded_validator():
    from app.auth import validate_password, validate_username
    assert validate_username("ab") == "ab"
    validate_password("12345")
    validate_password("x" * 10000)
    with pytest.raises(ValueError):
        validate_username("a")
    with pytest.raises(ValueError):
        validate_password("1234")

def test_auth_request_body_has_safety_ceiling():
    client = TestClient(main.app)
    response = client.post("/api/auth/login", headers={**ORIGIN, "Content-Type": "application/json"},
        content=b"x" * (main.MAX_AUTH_BODY_BYTES + 1))
    assert response.status_code == 413

def test_text_files_are_allowed():
    client, _ = session_client("txt-user")
    response = upload(client, "requirements.txt", b"fastapi\nuvicorn\n")
    assert response.status_code == 201
    assert response.json()["name"] == "requirements.txt"

def test_admin_overview_is_admin_only():
    student, student_user = session_client("regular")
    assert student.get("/api/admin/overview").status_code == 403
    admin, _ = session_client("supervisor", role="admin")
    main.connected_clients[object()] = main.ClientInfo(
        student_user["id"], student_user["username"], student_user["role"], "192.168.0.42"
    )
    overview = admin.get("/api/admin/overview")
    assert overview.status_code == 200
    usernames = {user["username"] for user in overview.json()["users"]}
    assert {"regular", "supervisor"} <= usernames
    users = {user["username"]: user for user in overview.json()["users"]}
    assert users["regular"]["current_ip"] == "192.168.0.42"
    assert users["supervisor"]["current_ip"] is None

def test_admin_can_disable_and_reenable_user():
    student, student_user = session_client("disable-me")
    admin, _ = session_client("manager", role="admin")
    disabled = admin.post(f"/api/admin/users/{student_user['id']}", headers=ORIGIN,
        json={"role": "student", "active": False, "new_password": None})
    assert disabled.status_code == 200
    assert disabled.json()["active"] is False
    assert student.get("/api/auth/me").status_code == 401
    enabled = admin.post(f"/api/admin/users/{student_user['id']}", headers=ORIGIN,
        json={"role": "student", "active": True, "new_password": None})
    assert enabled.status_code == 200
    assert enabled.json()["active"] is True

def test_admin_password_reset_invalidates_sessions_and_new_password_works():
    user = database.create_user("reset-me", hash_secret("old-pass"))
    raw = "reset-session"
    database.create_session(token_hash(raw), user["id"], "2999-01-01T00:00:00Z")
    student = TestClient(main.app)
    student.cookies.set(main.SESSION_COOKIE, raw)
    admin, _ = session_client("password-admin", role="admin")
    reset = admin.post(f"/api/admin/users/{user['id']}", headers=ORIGIN,
        json={"role": "student", "active": True, "new_password": "new55"})
    assert reset.status_code == 200
    assert student.get("/api/auth/me").status_code == 401
    login = TestClient(main.app).post("/api/auth/login", headers=ORIGIN,
        json={"username": "reset-me", "password": "new55"})
    assert login.status_code == 200

def test_final_active_admin_cannot_be_disabled_or_demoted():
    admin, admin_user = session_client("only-admin", role="admin")
    disable = admin.post(f"/api/admin/users/{admin_user['id']}", headers=ORIGIN,
        json={"role": "admin", "active": False, "new_password": None})
    assert disable.status_code == 400
    demote = admin.post(f"/api/admin/users/{admin_user['id']}", headers=ORIGIN,
        json={"role": "student", "active": True, "new_password": None})
    assert demote.status_code == 400

def test_admin_can_promote_another_account():
    admin, _ = session_client("first-admin", role="admin")
    _, student = session_client("future-admin")
    promoted = admin.post(f"/api/admin/users/{student['id']}", headers=ORIGIN,
        json={"role": "admin", "active": True, "new_password": None})
    assert promoted.status_code == 200
    assert promoted.json()["role"] == "admin"

def test_admin_controls_registration_and_rotates_enrollment_code(monkeypatch):
    from app.auth import verify_secret
    admin, _ = session_client("settings-admin", role="admin")
    closed = admin.post("/api/admin/registration", headers=ORIGIN, json={"enabled": False})
    assert closed.status_code == 200
    assert main.CONFIG.registration_enabled is False
    changed = admin.post("/api/admin/enrollment-code", headers=ORIGIN,
        json={"enrollment_code": "room5"})
    assert changed.status_code == 204
    assert verify_secret(main.CONFIG.enrollment_code_hash, "room5")
    config_path = Path(os.environ["BAMBOOCHAT_CONFIG"])
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["registration_enabled"] is False
    assert saved["enrollment_code_hash"] != "room5"

def test_admin_ui_and_relaxed_fields_are_rendered():
    response = TestClient(main.app).get("/")
    assert 'id="admin-modal"' in response.text
    assert 'id="admin-btn"' in response.text
    assert 'id="help-btn"' in response.text
    assert 'id="help-modal"' in response.text
    assert 'id="mention-menu"' in response.text
    assert 'id="markdown-toolbar"' in response.text
    assert 'id="retention-note"' in response.text
    assert '서버에 영구 저장' in response.text
    assert '“변경 적용”을 눌러 저장하세요' in response.text
    assert 'minlength="2"' in response.text
    assert 'placeholder="비밀번호 (5자 이상)"' in response.text
    assert 'id="password-input" type="password" maxlength=' not in response.text


def test_cli_password_recovery_resets_hash_without_starting_server(tmp_path, monkeypatch):
    import run as run_module
    from app.auth import verify_secret

    config_path = tmp_path / "cli-room.json"
    data_dir = tmp_path / "cli-data"
    config = RoomConfig(server_name="CLI room", data_dir=str(data_dir),
        enrollment_code_hash=hash_secret("room55"))
    save_config(config, config_path)
    database.configure_storage(data_dir, config.database_limit_bytes)
    database.init_db()
    database.create_user("recover-admin", hash_secret("old55"), role="admin")

    answers = iter(["new55", "new55"])
    monkeypatch.setattr(run_module.getpass, "getpass", lambda _prompt: next(answers))
    monkeypatch.setattr("sys.argv", ["run.py", "--config", str(config_path),
                                     "--reset-user-password", "recover-admin"])
    run_module.main()

    recovered = database.get_user_by_username("recover-admin")
    assert verify_secret(recovered["password_hash"], "new55")
