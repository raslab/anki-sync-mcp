from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from typing import Any

import pytest
from anki.collection import Collection
from anki.errors import NetworkError, SyncError, SyncErrorKind
from anki.sync import SyncAuth, SyncOutput
from anki.sync_pb2 import MediaSyncStatusResponse
from starlette.testclient import TestClient

from anki_mcp.app import create_app
from anki_mcp.config import Settings


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Settings:
    collection_path = tmp_path / "collection.anki2"
    collection = Collection(str(collection_path))
    deck_id = collection.decks.id("Languages::Spanish")
    note = collection.new_note(collection.models.current())
    note["Front"] = "hola"
    note["Back"] = "hello"
    collection.add_note(note, deck_id)
    collection.close()
    monkeypatch.setenv("MCP_AUTH_TOKEN", "correct-token")
    monkeypatch.setenv("ANKI_COLLECTION_PATH", str(collection_path))
    monkeypatch.setenv("ANKI_SYNC_USERNAME", "sync-user")
    monkeypatch.setenv("ANKI_SYNC_PASSWORD", "sync-password")
    monkeypatch.setenv("ANKI_SYNC_HOST", "https://sync.example.test/")
    monkeypatch.setenv("ANKI_SYNC_ON_WRITE", "false")
    monkeypatch.setenv("MCP_SCOPES", "read,write,admin,destructive")
    monkeypatch.setenv("ANKI_ALLOW_DESTRUCTIVE", "true")
    monkeypatch.setenv("ANKI_ALLOW_FULL_SYNC", "true")
    monkeypatch.setenv("ANKI_ALLOW_SCHEMA_CHANGES", "true")
    return Settings(_env_file=None)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_health_endpoints_are_public_and_safe(client: TestClient) -> None:
    assert client.get("/health/live").json() == {"status": "ok"}
    ready = client.get("/health/ready")
    assert ready.status_code == 503
    assert ready.json() == {"status": "not_ready", "reason": "authentication_required"}
    assert "token" not in ready.text.lower()
    assert "collection.anki2" not in ready.text


def test_readiness_degrades_when_collection_is_unusable(client: TestClient) -> None:
    service = client.app.app.state.collection_service
    service.executor.submit(lambda adapter: adapter.close()).result()
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "reason": "collection_unavailable"}


def test_mcp_rejects_missing_or_invalid_bearer_token(client: TestClient) -> None:
    for method in ("GET", "POST", "DELETE"):
        missing = client.request(method, "/mcp", json={})
        invalid = client.request(
            method,
            "/mcp",
            headers={"Authorization": "Bearer wrong-token"},
            json={},
        )
        assert missing.status_code == 401
        assert invalid.status_code == 401
        assert missing.headers["www-authenticate"] == "Bearer"
        assert "wrong-token" not in invalid.text


def test_bearer_token_comparison_runs_for_missing_and_malformed_headers(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[bytes, bytes]] = []

    def compare(supplied: bytes, expected: bytes) -> bool:
        calls.append((supplied, expected))
        return False

    monkeypatch.setattr("anki_mcp.auth.hmac.compare_digest", compare)
    assert client.post("/mcp", json={}).status_code == 401
    assert (
        client.post("/mcp", headers={"Authorization": "Basic malformed"}, json={}).status_code
        == 401
    )
    assert calls == [(b"", b"correct-token"), (b"malformed", b"correct-token")]


def test_mcp_accepts_valid_bearer_token(client: TestClient) -> None:
    response = client.post(
        "/mcp",
        headers={
            "Authorization": "Bearer correct-token",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == 1
    assert payload["result"]["serverInfo"]["name"] == "anki-mcp"


def test_bearer_scheme_is_case_insensitive(client: TestClient) -> None:
    response = client.post(
        "/mcp",
        headers={
            "Authorization": "bearer correct-token",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        },
    )
    assert response.status_code == 200


def test_mcp_rejects_untrusted_host_and_origin(client: TestClient) -> None:
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1"},
        },
    }
    headers = {
        "Authorization": "Bearer correct-token",
        "Accept": "application/json, text/event-stream",
    }
    bad_host = client.post("/mcp", headers={**headers, "Host": "evil.example"}, json=request)
    bad_origin = client.post(
        "/mcp", headers={**headers, "Origin": "https://evil.example"}, json=request
    )
    assert bad_host.status_code == 421
    assert bad_origin.status_code == 403


def test_exact_tool_inventory(client: TestClient) -> None:
    headers = {
        "Authorization": "Bearer correct-token",
        "Accept": "application/json, text/event-stream",
    }
    initialize = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        },
    )
    headers["Mcp-Session-Id"] = initialize.headers["mcp-session-id"]
    listed = client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    assert listed.status_code == 200
    tools = listed.json()["result"]["tools"]
    names = [tool["name"] for tool in tools]
    assert names == [
        "anki_status",
        "anki_operations_list",
        "anki_operations_get",
        "anki_metrics",
        "anki_sync_login",
        "anki_sync",
        "anki_sync_full_download",
        "anki_sync_full_upload",
        "anki_backup_create",
        "anki_decks_list",
        "anki_decks_get",
        "anki_decks_create",
        "anki_decks_update",
        "anki_decks_update_config",
        "anki_decks_delete_preview",
        "anki_decks_delete",
        "anki_notes_search",
        "anki_notes_get",
        "anki_notes_create",
        "anki_notes_create_batch",
        "anki_notes_update_fields",
        "anki_notes_add_tags",
        "anki_notes_remove_tags",
        "anki_notes_delete_preview",
        "anki_notes_delete",
        "anki_tags_list",
        "anki_tags_rename",
        "anki_tags_delete_preview",
        "anki_tags_delete",
        "anki_cards_search",
        "anki_cards_get",
        "anki_cards_create",
        "anki_cards_update",
        "anki_cards_change_deck",
        "anki_cards_suspend",
        "anki_cards_unsuspend",
        "anki_cards_set_flag",
        "anki_cards_reposition",
        "anki_cards_delete_preview",
        "anki_cards_delete",
        "anki_note_types_list",
        "anki_note_types_get",
        "anki_note_types_change_preview",
        "anki_note_types_create",
        "anki_note_types_update",
        "anki_note_type_fields_update",
        "anki_templates_update",
        "anki_note_types_delete",
        "anki_media_list",
        "anki_media_get",
        "anki_media_check",
        "anki_media_store",
        "anki_media_rename",
        "anki_media_delete_preview",
        "anki_media_delete",
    ]
    assert all(tool["inputSchema"]["additionalProperties"] is False for tool in tools)
    paginated = [tool for tool in tools if "limit" in tool["inputSchema"]["properties"]]
    assert all(tool["inputSchema"]["properties"]["limit"]["minimum"] == 1 for tool in paginated)
    assert all(tool["inputSchema"]["properties"]["limit"]["maximum"] == 100 for tool in paginated)
    by_name = {tool["name"]: tool for tool in tools}
    assert by_name["anki_sync"]["inputSchema"]["properties"]["sync_media"]["type"] == "boolean"
    for name in (
        "anki_sync_full_download",
        "anki_sync_full_upload",
    ):
        assert by_name[name]["inputSchema"]["properties"]["confirm"]["type"] == "boolean"
    for name in (
        "anki_decks_delete",
        "anki_cards_delete",
        "anki_notes_delete",
        "anki_tags_delete",
        "anki_note_types_delete",
        "anki_media_delete",
    ):
        assert by_name[name]["inputSchema"]["properties"]["confirmation_token"]["type"] == "string"


def test_phase2_administration_tools_work_through_json_rpc(client: TestClient) -> None:
    headers = {
        "Authorization": "Bearer correct-token",
        "Accept": "application/json, text/event-stream",
    }
    initialized = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        },
    )
    headers["Mcp-Session-Id"] = initialized.headers["mcp-session-id"]
    request_id = 1

    def call(name: str, arguments: dict[str, object]) -> dict[str, Any]:
        nonlocal request_id
        request_id += 1
        response = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        result = response.json()["result"]
        assert result.get("isError") is not True, result
        return json.loads(result["content"][0]["text"])

    decks = call("anki_decks_list", {})
    deck_id = next(item["id"] for item in decks["items"] if item["name"] == "Languages::Spanish")
    cards = call("anki_cards_search", {"query": 'deck:"Languages::Spanish"'})
    card_id = cards["items"][0]["id"]
    note_types = call("anki_note_types_list", {})
    note_type_id = next(item["id"] for item in note_types["items"] if item["name"] == "Basic")

    deck_config = call(
        "anki_decks_update_config",
        {
            "deck_id": deck_id,
            "new_cards_per_day": 13,
            "reviews_per_day": 77,
            "max_answer_seconds": 42,
            "desired_retention": 0.9,
            "idempotency_key": "phase2-deck-config",
        },
    )
    flagged = call(
        "anki_cards_set_flag",
        {"card_ids": [card_id], "flag": 4, "idempotency_key": "phase2-flag"},
    )
    repositioned = call(
        "anki_cards_reposition",
        {
            "card_ids": [card_id],
            "starting_from": 20,
            "step_size": 1,
            "randomize": False,
            "shift_existing": True,
            "idempotency_key": "phase2-reposition",
        },
    )
    fields_preview = call(
        "anki_note_types_change_preview",
        {
            "operation": "fields_update",
            "note_type_id": note_type_id,
            "field_mappings": [
                {"name": "Back", "source_ordinal": 1},
                {"name": "Hint", "source_ordinal": None},
                {"name": "Front", "source_ordinal": 0},
            ],
        },
    )
    fields = call(
        "anki_note_type_fields_update",
        {
            "note_type_id": note_type_id,
            "mappings": [
                {"name": "Back", "source_ordinal": 1},
                {"name": "Hint", "source_ordinal": None},
                {"name": "Front", "source_ordinal": 0},
            ],
            "confirmation_token": fields_preview["confirmation_token"],
            "idempotency_key": "phase2-fields",
        },
    )
    templates_preview = call(
        "anki_note_types_change_preview",
        {
            "operation": "templates_update",
            "note_type_id": note_type_id,
            "template_mappings": [
                {
                    "name": "Card 1",
                    "source_ordinal": 0,
                    "question_format": "{{Front}}",
                    "answer_format": "{{FrontSide}}<hr id=answer>{{Back}}",
                }
            ],
        },
    )
    templates = call(
        "anki_templates_update",
        {
            "note_type_id": note_type_id,
            "mappings": [
                {
                    "name": "Card 1",
                    "source_ordinal": 0,
                    "question_format": "{{Front}}",
                    "answer_format": "{{FrontSide}}<hr id=answer>{{Back}}",
                }
            ],
            "confirmation_token": templates_preview["confirmation_token"],
            "idempotency_key": "phase2-templates",
        },
    )
    media = call("anki_media_check", {})
    operations = call("anki_operations_list", {})
    operation = call("anki_operations_get", {"idempotency_key": "phase2-deck-config"})
    metrics = call("anki_metrics", {})

    assert deck_config["result"]["updated"] is True
    assert flagged["result"]["flag"] == 4
    assert repositioned["result"]["repositioned"] == 1
    assert fields["result"]["field_count"] == 3
    assert templates["result"]["template_count"] == 1
    assert media["missing_total"] == 0
    assert operations["total"] == 5
    assert operation["operation"] == "anki_decks_update_config"
    assert metrics["mutations"]["total"] == 5


def test_critical_resource_crud_tools_work_through_json_rpc(client: TestClient) -> None:
    headers = {
        "Authorization": "Bearer correct-token",
        "Accept": "application/json, text/event-stream",
    }
    initialized = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        },
    )
    headers["Mcp-Session-Id"] = initialized.headers["mcp-session-id"]
    request_id = 2

    def call(name: str, arguments: dict[str, object]) -> dict[str, object]:
        nonlocal request_id
        response = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        request_id += 1
        assert response.status_code == 200
        result = response.json()["result"]
        assert result.get("isError") is not True, result
        parsed = json.loads(result["content"][0]["text"])
        assert isinstance(parsed, dict)
        return parsed

    deck = call("anki_decks_create", {"name": "CRUD"})
    existing_deck = call("anki_decks_create", {"name": "CRUD"})
    deck_id = deck["result"]["id"]
    renamed = call("anki_decks_update", {"deck_id": deck_id, "name": "CRUD Updated"})
    card = call(
        "anki_cards_create",
        {"deck_id": deck_id, "front": "question", "back": "answer"},
    )
    card_id = card["result"]["id"]
    changed = call(
        "anki_cards_update",
        {"card_id": card_id, "front": "new question", "back": "new answer"},
    )
    card_token = call("anki_cards_delete_preview", {"card_id": card_id})["confirmation_token"]
    card_deleted = call("anki_cards_delete", {"card_id": card_id, "confirmation_token": card_token})
    deck_token = call("anki_decks_delete_preview", {"deck_id": deck_id})["confirmation_token"]
    deck_deleted = call("anki_decks_delete", {"deck_id": deck_id, "confirmation_token": deck_token})

    create_token = call(
        "anki_note_types_change_preview",
        {
            "operation": "create",
            "name": "Protocol Type",
            "fields": ["Question", "Answer"],
            "templates": [
                {
                    "name": "Card 1",
                    "question_format": "{{Question}}",
                    "answer_format": "{{Answer}}",
                }
            ],
        },
    )["confirmation_token"]
    note_type = call(
        "anki_note_types_create",
        {
            "name": "Protocol Type",
            "fields": ["Question", "Answer"],
            "templates": [
                {
                    "name": "Card 1",
                    "question_format": "{{Question}}",
                    "answer_format": "{{Answer}}",
                }
            ],
            "confirmation_token": create_token,
        },
    )
    note_type_id = note_type["result"]["id"]
    assert call("anki_note_types_get", {"note_type_id": note_type_id})["name"] == "Protocol Type"
    assert call("anki_note_types_list", {"limit": 100})["total"] >= 1
    update_token = call(
        "anki_note_types_change_preview",
        {
            "operation": "update",
            "note_type_id": note_type_id,
            "name": "Protocol Type Updated",
            "fields": ["Prompt", "Response"],
            "templates": [
                {
                    "name": "Prompt Card",
                    "question_format": "{{Prompt}}",
                    "answer_format": "{{Response}}",
                }
            ],
        },
    )["confirmation_token"]
    call(
        "anki_note_types_update",
        {
            "note_type_id": note_type_id,
            "name": "Protocol Type Updated",
            "fields": ["Prompt", "Response"],
            "templates": [
                {
                    "name": "Prompt Card",
                    "question_format": "{{Prompt}}",
                    "answer_format": "{{Response}}",
                }
            ],
            "confirmation_token": update_token,
        },
    )
    resources_deck = call("anki_decks_create", {"name": "Resource CRUD"})
    resources_deck_id = resources_deck["result"]["id"]
    note = call(
        "anki_notes_create",
        {
            "deck_id": resources_deck_id,
            "note_type_id": note_type_id,
            "fields": {"Prompt": "protocol note", "Response": "answer"},
            "tags": ["protocol-old"],
        },
    )
    note_id = note["result"]["note_id"]
    assert call("anki_tags_list", {"limit": 100})["total"] >= 1
    call("anki_tags_rename", {"old_name": "protocol-old", "new_name": "protocol-new"})
    tag_token = call("anki_tags_delete_preview", {"name": "protocol-new"})["confirmation_token"]
    call("anki_tags_delete", {"name": "protocol-new", "confirmation_token": tag_token})
    note_token = call("anki_notes_delete_preview", {"note_ids": [note_id]})["confirmation_token"]
    call("anki_notes_delete", {"note_ids": [note_id], "confirmation_token": note_token})
    type_token = call(
        "anki_note_types_change_preview",
        {"operation": "delete", "note_type_id": note_type_id},
    )["confirmation_token"]
    call(
        "anki_note_types_delete",
        {"note_type_id": note_type_id, "confirmation_token": type_token},
    )
    resource_deck_token = call("anki_decks_delete_preview", {"deck_id": resources_deck_id})[
        "confirmation_token"
    ]
    call(
        "anki_decks_delete",
        {"deck_id": resources_deck_id, "confirmation_token": resource_deck_token},
    )

    encoded = base64.b64encode(b"protocol media").decode()
    media = call(
        "anki_media_store",
        {"filename": "protocol.txt", "content_base64": encoded},
    )
    assert media["result"]["created"] is True
    assert call("anki_media_list", {"limit": 100})["total"] == 1
    assert call("anki_media_get", {"filename": "protocol.txt"})["content_base64"] == encoded
    call(
        "anki_media_rename",
        {"old_filename": "protocol.txt", "new_filename": "renamed.txt"},
    )
    media_token = call("anki_media_delete_preview", {"filename": "renamed.txt"})[
        "confirmation_token"
    ]
    call(
        "anki_media_delete",
        {"filename": "renamed.txt", "confirmation_token": media_token},
    )

    media_sync_calls: list[bool] = []
    collection_service = client.app.app.state.collection_service

    def enable_media_sync_probe(adapter: Any) -> None:
        adapter.sync_on_write = True
        adapter._sync_or_raise_full_sync = lambda sync_media: (
            media_sync_calls.append(sync_media) or {"required": "NO_CHANGES"}
        )

    collection_service.executor.submit(enable_media_sync_probe).result()
    synced_media = call(
        "anki_media_store",
        {
            "filename": "synced.txt",
            "content_base64": base64.b64encode(b"sync me").decode(),
        },
    )
    call("anki_media_get", {"filename": "synced.txt", "sync_before": True})
    assert synced_media["remote_synced"] is True
    assert synced_media["media_synced"] is True
    assert media_sync_calls == [True, True, True]

    assert renamed["result"] == {"id": deck_id, "updated": True}
    assert deck["result"]["created"] is True
    assert existing_deck["result"] == {"id": deck_id, "created": False}
    assert changed["result"]["updated"] is True
    assert card_deleted["result"]["deleted"] is True
    assert deck_deleted["result"]["deleted"] is True


def test_sync_tools_use_server_configuration_without_exposing_credentials(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    login_calls: list[tuple[str, str, str | None]] = []
    full_sync_calls: list[tuple[int | None, bool]] = []

    def login(_: Collection, username: str, password: str, endpoint: str | None) -> SyncAuth:
        login_calls.append((username, password, endpoint))
        return SyncAuth(hkey="secret-host-key", endpoint=endpoint)

    def sync(_: Collection, auth: SyncAuth, sync_media: bool) -> SyncOutput:
        assert auth.hkey == "secret-host-key"
        assert sync_media is False
        return SyncOutput(
            required=3, server_message="full download required", host_number=2, server_media_usn=7
        )

    def full_sync(_: Collection, *, auth: SyncAuth, server_usn: int | None, upload: bool) -> None:
        assert auth.hkey == "secret-host-key"
        full_sync_calls.append((server_usn, upload))

    monkeypatch.setattr(Collection, "sync_login", login)
    monkeypatch.setattr(Collection, "sync_collection", sync)
    monkeypatch.setattr(Collection, "full_upload_or_download", full_sync)
    monkeypatch.setattr(Collection, "create_backup", lambda *args, **kwargs: True)
    headers = {
        "Authorization": "Bearer correct-token",
        "Accept": "application/json, text/event-stream",
    }
    initialized = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        },
    )
    headers["Mcp-Session-Id"] = initialized.headers["mcp-session-id"]

    def call(request_id: int, name: str, arguments: dict[str, object]) -> dict[str, object]:
        response = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        result = response.json()["result"]
        assert result.get("isError") is not True
        return json.loads(result["content"][0]["text"])

    logged_in = call(2, "anki_sync_login", {})
    synced = call(3, "anki_sync", {"sync_media": False})
    downloaded = call(4, "anki_sync_full_download", {"confirm": True})

    assert login_calls == [("sync-user", "sync-password", "https://sync.example.test/")]
    assert logged_in == {"authenticated": True, "endpoint_kind": "custom"}
    assert "sync-password" not in repr(logged_in)
    assert "secret-host-key" not in repr(logged_in)
    assert synced["required"] == "FULL_DOWNLOAD"
    assert downloaded == {"completed": True, "direction": "download", "backup_created": True}
    assert full_sync_calls == [(None, False)]


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (NetworkError("network secret", None, None, None), "NETWORK_ERROR"),
        (
            SyncError("authentication secret", None, None, None, SyncErrorKind.AUTH),
            "AUTHENTICATION_FAILED",
        ),
    ],
)
def test_sync_failures_have_safe_machine_readable_codes(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected_code: str,
) -> None:
    def fail_login(*args: object, **kwargs: object) -> SyncAuth:
        raise failure

    monkeypatch.setattr(Collection, "sync_login", fail_login)
    headers = {
        "Authorization": "Bearer correct-token",
        "Accept": "application/json, text/event-stream",
    }
    initialized = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        },
    )
    headers["Mcp-Session-Id"] = initialized.headers["mcp-session-id"]
    response = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "anki_sync_login", "arguments": {}},
        },
    )
    result = response.json()["result"]
    text = result["content"][0]["text"]
    payload = json.loads(text[text.index("{") :])
    assert result["isError"] is True
    assert payload["code"] == expected_code
    assert "secret" not in payload["message"]


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("anki_cards_set_flag", {"card_ids": [1], "flag": 8}),
        ("anki_cards_reposition", {"card_ids": [1], "starting_from": 0}),
        ("anki_cards_set_flag", {"card_ids": [1], "flag": 1, "unexpected": True}),
    ],
)
def test_argument_validation_has_stable_machine_readable_error(
    client: TestClient, name: str, arguments: dict[str, object]
) -> None:
    headers = {
        "Authorization": "Bearer correct-token",
        "Accept": "application/json, text/event-stream",
    }
    initialized = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        },
    )
    headers["Mcp-Session-Id"] = initialized.headers["mcp-session-id"]
    response = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )

    result = response.json()["result"]
    text = result["content"][0]["text"]
    payload = json.loads(text[text.index("{") :])
    assert result["isError"] is True
    assert payload["code"] == "INVALID_ARGUMENT"
    assert payload["message"] == "tool arguments failed validation"
    assert payload["correlation_id"]
    assert "pydantic.dev" not in text
    assert "unexpected" not in text


def test_media_sync_timeout_has_stable_error_and_preserves_authentication(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom = settings.model_copy(update={"sync_timeout_seconds": 0.001})
    monkeypatch.setattr(
        Collection,
        "sync_login",
        lambda self, username, password, endpoint: SyncAuth(hkey="phase2", endpoint=endpoint),
    )
    monkeypatch.setattr(
        Collection,
        "sync_collection",
        lambda self, auth, sync_media: SyncOutput(required=0),
    )
    monkeypatch.setattr(
        "anki._backend.RustBackend.media_sync_status",
        lambda self: MediaSyncStatusResponse(active=True),
    )
    headers = {
        "Authorization": "Bearer correct-token",
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(create_app(custom)) as custom_client:
        initialized = custom_client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1"},
                },
            },
        )
        headers["Mcp-Session-Id"] = initialized.headers["mcp-session-id"]

        def call(request_id: int, name: str, arguments: dict[str, object]) -> dict[str, Any]:
            return custom_client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
            ).json()["result"]

        assert call(2, "anki_sync_login", {}).get("isError") is not True
        failed = call(3, "anki_sync", {"sync_media": True})
        status = call(4, "anki_status", {})

    text = failed["content"][0]["text"]
    payload = json.loads(text[text.index("{") :])
    assert failed["isError"] is True
    assert payload["code"] == "MEDIA_SYNC_FAILED"
    assert json.loads(status["content"][0]["text"])["authenticated"] is True


def test_sync_login_rejects_an_unconfigured_username_before_contacting_remote(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom = settings.model_copy(update={"sync_username": ""})

    def unexpected_login(*args: object, **kwargs: object) -> SyncAuth:
        pytest.fail("sync backend must not be called without a configured username")

    monkeypatch.setattr(Collection, "sync_login", unexpected_login)
    headers = {
        "Authorization": "Bearer correct-token",
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(create_app(custom)) as custom_client:
        initialized = custom_client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1"},
                },
            },
        )
        headers["Mcp-Session-Id"] = initialized.headers["mcp-session-id"]
        response = custom_client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "anki_sync_login", "arguments": {}},
            },
        )

    result = response.json()["result"]
    text = result["content"][0]["text"]
    payload = json.loads(text[text.index("{") :])
    assert result["isError"] is True
    assert payload["code"] == "INVALID_ARGUMENT"
    assert payload["message"] == "ANKI_SYNC_USERNAME is not configured"
    assert payload["correlation_id"]


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("anki_sync", {"sync_media": 1}),
        ("anki_sync_full_download", {"confirm": 1}),
        ("anki_sync_full_upload", {"confirm": "true"}),
    ],
)
def test_boolean_tool_inputs_are_strict(
    client: TestClient, tool: str, arguments: dict[str, object]
) -> None:
    headers = {
        "Authorization": "Bearer correct-token",
        "Accept": "application/json, text/event-stream",
    }
    initialized = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        },
    )
    headers["Mcp-Session-Id"] = initialized.headers["mcp-session-id"]
    response = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
    )
    result = response.json()["result"]
    assert result["isError"] is True
    assert json.loads(result["content"][0]["text"])["code"] == "INVALID_ARGUMENT"


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("anki_decks_get", {"deck_id": 0}),
        ("anki_cards_get", {"card_id": -1}),
        ("anki_decks_create", {"name": "x" * 513}),
        ("anki_cards_search", {"query": "x" * 4097}),
        ("anki_cards_create", {"deck_id": 1, "front": "x" * 262_145, "back": "ok"}),
    ],
)
def test_tool_ids_and_strings_are_runtime_bounded(
    client: TestClient, tool: str, arguments: dict[str, object]
) -> None:
    headers = {
        "Authorization": "Bearer correct-token",
        "Accept": "application/json, text/event-stream",
    }
    initialized = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        },
    )
    headers["Mcp-Session-Id"] = initialized.headers["mcp-session-id"]
    response = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
    )
    result = response.json()["result"]
    assert result["isError"] is True
    assert json.loads(result["content"][0]["text"])["code"] == "INVALID_ARGUMENT"


def test_mcp_request_body_budget_is_enforced(settings: Settings) -> None:
    custom = settings.model_copy(update={"max_request_bytes": 1024})
    with TestClient(create_app(custom)) as custom_client:
        response = custom_client.post(
            "/mcp",
            headers={
                "Authorization": "Bearer correct-token",
                "Content-Type": "application/json",
            },
            content=b"x" * 1025,
        )
    assert response.status_code == 413
    assert response.json() == {"error": "request_too_large"}


def test_aggregate_tool_response_budget_is_enforced(settings: Settings) -> None:
    custom = settings.model_copy(update={"max_response_bytes": 128})
    headers = {
        "Authorization": "Bearer correct-token",
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(create_app(custom)) as custom_client:
        initialized = custom_client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1"},
                },
            },
        )
        headers["Mcp-Session-Id"] = initialized.headers["mcp-session-id"]
        response = custom_client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "anki_decks_list", "arguments": {"limit": 100}},
            },
        )
    result = response.json()["result"]
    assert result["isError"] is True
    text = result["content"][0]["text"]
    payload = json.loads(text[text.index("{") :])
    assert payload["code"] == "RESPONSE_TOO_LARGE"


def test_mutation_returns_concise_receipt_before_response_budget_check(settings: Settings) -> None:
    custom = settings.model_copy(update={"max_response_bytes": 512})
    headers = {
        "Authorization": "Bearer correct-token",
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(create_app(custom)) as custom_client:
        initialized = custom_client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1"},
                },
            },
        )
        headers["Mcp-Session-Id"] = initialized.headers["mcp-session-id"]
        response = custom_client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "anki_decks_create", "arguments": {"name": "Receipt"}},
            },
        )
    result = response.json()["result"]
    assert result.get("isError") is not True
    receipt = json.loads(result["content"][0]["text"])
    assert receipt["local_committed"] is True
    assert receipt["result"]["created"] is True
    collection = Collection(str(settings.collection_path))
    try:
        assert collection.decks.id_for_name("Receipt") == receipt["result"]["id"]
    finally:
        collection.close()


def test_configured_page_max_is_the_tool_default(settings: Settings) -> None:
    custom = settings.model_copy(update={"max_page_size": 20})
    headers = {
        "Authorization": "Bearer correct-token",
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(create_app(custom)) as custom_client:
        initialized = custom_client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1"},
                },
            },
        )
        headers["Mcp-Session-Id"] = initialized.headers["mcp-session-id"]
        listed = custom_client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        deck_list = next(
            tool for tool in listed.json()["result"]["tools"] if tool["name"] == "anki_decks_list"
        )
        limit_schema = deck_list["inputSchema"]["properties"]["limit"]
        assert limit_schema["default"] == 20
        assert limit_schema["maximum"] == 20
        called = custom_client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "anki_decks_list", "arguments": {}},
            },
        )
        assert called.json()["result"].get("isError") is not True


def test_read_tools_have_protocol_happy_paths(client: TestClient) -> None:
    headers = {
        "Authorization": "Bearer correct-token",
        "Accept": "application/json, text/event-stream",
    }
    initialize = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        },
    )
    headers["Mcp-Session-Id"] = initialize.headers["mcp-session-id"]
    request_id = 2

    def call(name: str, arguments: dict[str, object]) -> dict[str, object]:
        nonlocal request_id
        response = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        request_id += 1
        assert response.status_code == 200
        result = response.json()["result"]
        assert result.get("isError") is not True
        parsed = json.loads(result["content"][0]["text"])
        assert isinstance(parsed, dict)
        return parsed

    decks = call("anki_decks_list", {"offset": 0, "limit": 20})
    items = decks["items"]
    assert isinstance(items, list)
    spanish = next(item for item in items if item["name"] == "Languages::Spanish")
    deck = call("anki_decks_get", {"deck_id": spanish["id"]})
    assert deck["name"] == "Languages::Spanish"

    cards = call("anki_cards_search", {"query": "hola", "offset": 0, "limit": 10})
    assert cards["total"] == 1
    card_items = cards["items"]
    assert isinstance(card_items, list)
    card = call("anki_cards_get", {"card_id": card_items[0]["id"]})
    assert card["deck_name"] == "Languages::Spanish"


def test_tool_errors_are_machine_readable(client: TestClient) -> None:
    headers = {
        "Authorization": "Bearer correct-token",
        "Accept": "application/json, text/event-stream",
    }
    initialized = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        },
    )
    headers["Mcp-Session-Id"] = initialized.headers["mcp-session-id"]

    def error_for(name: str, arguments: dict[str, object], request_id: int) -> dict[str, str]:
        response = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        result = response.json()["result"]
        assert result["isError"] is True
        text = result["content"][0]["text"]
        parsed = json.loads(text[text.index("{") :])
        assert isinstance(parsed, dict)
        return parsed

    missing = error_for("anki_decks_get", {"deck_id": 999999999}, 2)
    invalid = error_for("anki_cards_search", {"query": "", "limit": 0}, 3)
    sync_auth = error_for("anki_sync", {}, 4)
    destructive = error_for("anki_decks_delete", {"deck_id": 1}, 5)
    assert missing["code"] == "NOT_FOUND"
    assert invalid["code"] == "INVALID_ARGUMENT"
    assert sync_auth["code"] == "AUTHENTICATION_FAILED"
    assert destructive["code"] == "INVALID_ARGUMENT"
    assert missing["correlation_id"]
    assert invalid["correlation_id"]

    marker = "sensitive-caller-value"
    unknown_argument = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "anki_cards_get",
                "arguments": {"card_id": 1, "unexpected": marker},
            },
        },
    )
    validation_result = unknown_argument.json()["result"]
    assert validation_result["isError"] is True
    assert marker not in validation_result["content"][0]["text"]

    strict_type = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "anki_cards_get", "arguments": {"card_id": True}},
        },
    )
    assert strict_type.json()["result"]["isError"] is True

    service = client.app.app.state.collection_service
    service.executor.submit(lambda adapter: adapter.close()).result()
    internal = error_for("anki_decks_list", {}, 6)
    assert internal["code"] == "INTERNAL_ERROR"
    assert internal["message"] == "internal collection operation failed"
    assert "collection.anki2" not in json.dumps(internal)
