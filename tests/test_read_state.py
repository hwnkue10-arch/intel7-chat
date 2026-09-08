"""Tests for Iteration 4 — Read State & Notifications."""
import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from app import database, main
from app.auth import token_hash
from app.config import GIB

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


class TestReadStateMigration:
    def test_schema_version_reaches_v4(self):
        with database.get_connection() as conn:
            version = database._get_schema_version(conn)
        assert version >= 4
        assert len(database._MIGRATIONS) >= 4

    def test_user_conversation_state_table_structure(self):
        with database.get_connection() as conn:
            info = conn.execute("PRAGMA table_info(user_conversation_state)").fetchall()
            cols = {col[1]: col[2] for col in info}
            assert "user_id" in cols
            assert "conversation_type" in cols
            assert "conversation_id" in cols
            assert "last_read_message_id" in cols
            assert "muted" in cols
            assert "updated_at" in cols


class TestReadStateApi:
    def test_get_empty_read_states(self):
        client, user = session_client("alice")
        resp = client.get("/api/read-states", headers=ORIGIN)
        assert resp.status_code == 200
        data = resp.json()
        assert "states" in data
        assert "unread_counts" in data
        assert "channel:1" in data["unread_counts"]
        assert data["unread_counts"]["channel:1"] == 0

    def test_ack_channel_read_state(self):
        client_alice, user_alice = session_client("alice")
        client_bob, user_bob = session_client("bob")

        msg1 = database.save_message("alice", "메시지 1", user_id=user_alice["id"], channel_id=1)
        msg2 = database.save_message("alice", "메시지 2", user_id=user_alice["id"], channel_id=1)
        raw_id2 = int(msg2["message_id"].replace("public:", ""))

        # Check Bob's initial unread count
        resp = client_bob.get("/api/read-states", headers=ORIGIN)
        assert resp.json()["unread_counts"]["channel:1"] == 2

        # Bob acks message 2
        ack_resp = client_bob.post("/api/read-states/ack", headers=ORIGIN, json={
            "conversation_type": "channel",
            "conversation_id": "1",
            "last_read_message_id": raw_id2,
        })
        assert ack_resp.status_code == 200
        ack_data = ack_resp.json()
        assert ack_data["state"]["last_read_message_id"] == raw_id2
        assert ack_data["unread_counts"]["channel:1"] == 0

        # Verify persistent read state
        resp2 = client_bob.get("/api/read-states", headers=ORIGIN)
        assert resp2.json()["states"]["channel:1"]["last_read_message_id"] == raw_id2
        assert resp2.json()["unread_counts"]["channel:1"] == 0

    def test_ack_dm_read_state(self):
        client_alice, user_alice = session_client("alice")
        client_bob, user_bob = session_client("bob")

        dm1 = database.save_direct_message(user_alice, user_bob, "DM 메시지 1")
        dm2 = database.save_direct_message(user_alice, user_bob, "DM 메시지 2")
        raw_dm2 = int(dm2["message_id"].replace("dm:", ""))

        # Check Bob's unread DM count
        resp = client_bob.get("/api/read-states", headers=ORIGIN)
        dm_key = f"dm:{user_alice['id']}"
        assert resp.json()["unread_counts"].get(dm_key) == 2

        # Bob acks DM
        ack_resp = client_bob.post("/api/read-states/ack", headers=ORIGIN, json={
            "conversation_type": "dm",
            "conversation_id": str(user_alice["id"]),
            "last_read_message_id": raw_dm2,
        })
        assert ack_resp.status_code == 200
        assert ack_resp.json()["unread_counts"].get(dm_key, 0) == 0

    def test_ack_dm_read_state_by_username(self):
        client_alice, user_alice = session_client("alice")
        client_bob, user_bob = session_client("bob")

        dm1 = database.save_direct_message(user_alice, user_bob, "DM 메시지 1")
        raw_dm1 = int(dm1["message_id"].replace("dm:", ""))

        dm_key = f"dm:{user_alice['id']}"
        resp = client_bob.get("/api/read-states", headers=ORIGIN)
        assert resp.json()["unread_counts"].get(dm_key) == 1

        # Bob acks DM using partner username
        ack_resp = client_bob.post("/api/read-states/ack", headers=ORIGIN, json={
            "conversation_type": "dm",
            "conversation_id": "alice",
            "last_read_message_id": raw_dm1,
        })
        assert ack_resp.status_code == 200
        assert ack_resp.json()["unread_counts"].get(dm_key, 0) == 0

    def test_ack_dm_read_state_prefixed(self):
        client_alice, user_alice = session_client("alice")
        client_bob, user_bob = session_client("bob")

        dm1 = database.save_direct_message(user_alice, user_bob, "DM 메시지 1")
        raw_dm1 = int(dm1["message_id"].replace("dm:", ""))
        dm_key = f"dm:{user_alice['id']}"

        ack_resp = client_bob.post("/api/read-states/ack", headers=ORIGIN, json={
            "conversation_type": "dm",
            "conversation_id": f"dm:{user_alice['id']}",
            "last_read_message_id": raw_dm1,
        })
        assert ack_resp.status_code == 200
        assert ack_resp.json()["unread_counts"].get(dm_key, 0) == 0

    def test_mute_and_unmute_conversation(self):
        client_bob, user_bob = session_client("bob")

        # Mute channel 1
        resp = client_bob.post("/api/read-states/mute", headers=ORIGIN, json={
            "conversation_type": "channel",
            "conversation_id": "1",
            "muted": True,
        })
        assert resp.status_code == 200
        assert resp.json()["state"]["muted"] is True

        # Verify in GET /api/read-states
        states_resp = client_bob.get("/api/read-states", headers=ORIGIN)
        assert states_resp.json()["states"]["channel:1"]["muted"] is True

        # Unmute channel 1
        resp2 = client_bob.post("/api/read-states/mute", headers=ORIGIN, json={
            "conversation_type": "channel",
            "conversation_id": "1",
            "muted": False,
        })
        assert resp2.status_code == 200
        assert resp2.json()["state"]["muted"] is False

    def test_read_state_requires_login(self):
        anon_client = TestClient(main.app)
        resp = anon_client.get("/api/read-states", headers=ORIGIN)
        assert resp.status_code == 401

        resp = anon_client.post("/api/read-states/ack", headers=ORIGIN, json={
            "conversation_type": "channel",
            "conversation_id": "1",
            "last_read_message_id": 1,
        })
        assert resp.status_code == 401


class TestReadStateWebSocketSync:
    def test_history_ready_includes_read_states_and_syncs_ack(self):
        client_alice, user_alice = session_client("alice")
        client_bob, user_bob = session_client("bob")

        msg = database.save_message("alice", "테스트", user_id=user_alice["id"], channel_id=1)
        raw_id = int(msg["message_id"].replace("public:", ""))

        with client_bob.websocket_connect("/ws", headers=ORIGIN) as ws:
            history_ready_evt = None
            while True:
                evt = ws.receive_json()
                if evt.get("type") == "history_ready":
                    history_ready_evt = evt
                    break

            assert history_ready_evt is not None
            assert "read_states" in history_ready_evt
            assert "unread_counts" in history_ready_evt
            assert history_ready_evt["unread_counts"]["channel:1"] == 1

            # Bob sends ack HTTP request in another tab
            ack_resp = client_bob.post("/api/read-states/ack", headers=ORIGIN, json={
                "conversation_type": "channel",
                "conversation_id": "1",
                "last_read_message_id": raw_id,
            })
            assert ack_resp.status_code == 200

            # WebSocket client receives read_state_updated
            event = ws.receive_json()
            assert event["type"] == "read_state_updated"
            assert event["state"]["last_read_message_id"] == raw_id
            assert event["unread_counts"]["channel:1"] == 0

    def test_cross_session_dm_read_state_sync(self):
        client_alice, user_alice = session_client("alice")
        client_bob, user_bob = session_client("bob")

        dm = database.save_direct_message(user_alice, user_bob, "안녕 밥")
        raw_dm_id = int(dm["message_id"].replace("dm:", ""))
        dm_key = f"dm:{user_alice['id']}"

        with client_bob.websocket_connect("/ws", headers=ORIGIN) as ws:
            while True:
                evt = ws.receive_json()
                if evt.get("type") == "history_ready":
                    assert evt["unread_counts"].get(dm_key) == 1
                    break

            # Bob ACKs DM via HTTP from another session tab using partner username
            ack_resp = client_bob.post("/api/read-states/ack", headers=ORIGIN, json={
                "conversation_type": "dm",
                "conversation_id": "alice",
                "last_read_message_id": raw_dm_id,
            })
            assert ack_resp.status_code == 200

            # Active WebSocket session receives real-time read_state_updated event
            event = ws.receive_json()
            assert event["type"] == "read_state_updated"
            assert event["state"]["conversation_type"] == "dm"
            assert str(event["state"]["conversation_id"]) == str(user_alice["id"])
            assert event["unread_counts"].get(dm_key, 0) == 0

    def test_mute_state_persistence_in_history_ready(self):
        client_alice, user_alice = session_client("alice")
        client_bob, user_bob = session_client("bob")

        # Bob mutes DM with Alice
        mute_resp = client_bob.post("/api/read-states/mute", headers=ORIGIN, json={
            "conversation_type": "dm",
            "conversation_id": "alice",
            "muted": True,
        })
        assert mute_resp.status_code == 200

        # Bob connects via WebSocket and checks history_ready
        dm_key = f"dm:{user_alice['id']}"
        with client_bob.websocket_connect("/ws", headers=ORIGIN) as ws:
            while True:
                evt = ws.receive_json()
                if evt.get("type") == "history_ready":
                    assert dm_key in evt["read_states"]
                    assert evt["read_states"][dm_key]["muted"] is True
                    break

    def test_unread_counts_has_no_duplicate_aliases(self):
        client_alice, user_alice = session_client("alice")
        client_bob, user_bob = session_client("bob")

        database.save_direct_message(user_alice, user_bob, "메시지 1")
        database.save_direct_message(user_alice, user_bob, "메시지 2")

        resp = client_bob.get("/api/read-states", headers=ORIGIN)
        assert resp.status_code == 200
        unread_counts = resp.json()["unread_counts"]
        # Should contain dm:<alice_id>, NOT dm:alice or raw user ID
        assert f"dm:{user_alice['id']}" in unread_counts
        assert "dm:alice" not in unread_counts
        assert unread_counts[f"dm:{user_alice['id']}"] == 2

    def test_own_messages_not_counted_in_unread(self):
        client_alice, user_alice = session_client("alice")
        client_bob, user_bob = session_client("bob")

        # Alice sends messages to general channel
        database.save_message("alice", "Alice msg 1", user_id=user_alice["id"], channel_id=1)
        database.save_message("alice", "Alice msg 2", user_id=user_alice["id"], channel_id=1)

        # Alice sends DM to Bob
        database.save_direct_message(user_alice, user_bob, "Alice to Bob DM")

        # Alice's unread counts should be 0 for both channel:1 and dm:bob
        resp_alice = client_alice.get("/api/read-states", headers=ORIGIN)
        assert resp_alice.status_code == 200
        alice_unreads = resp_alice.json()["unread_counts"]
        assert alice_unreads.get("channel:1", 0) == 0
        assert alice_unreads.get(f"dm:{user_bob['id']}", 0) == 0

        # Bob's unread counts should be 2 for channel:1 and 1 for dm:alice
        resp_bob = client_bob.get("/api/read-states", headers=ORIGIN)
        assert resp_bob.status_code == 200
        bob_unreads = resp_bob.json()["unread_counts"]
        assert bob_unreads.get("channel:1") == 2
        assert bob_unreads.get(f"dm:{user_alice['id']}") == 1

