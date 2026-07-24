from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from anki.collection import Collection
from anki.errors import NetworkError, SyncError, SyncErrorKind
from anki.sync import SyncAuth, SyncOutput
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
    return Settings(_env_file=None)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_health_endpoints_are_public_and_safe(client: TestClient) -> None:
    assert client.get("/health/live").json() == {"status": "ok"}
    ready = client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}
    assert "token" not in ready.text.lower()
    assert "collection.anki2" not in ready.text


def test_readiness_degrades_when_collection_is_unusable(client: TestClient) -> None:
    service = client.app.app.state.collection_service
    service.executor.submit(lambda adapter: adapter.close()).result()
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


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
        "anki_sync_login",
        "anki_sync",
        "anki_sync_full_download",
        "anki_sync_full_upload",
        "anki_decks_list",
        "anki_decks_get",
        "anki_decks_create",
        "anki_decks_update",
        "anki_decks_delete",
        "anki_cards_search",
        "anki_cards_get",
        "anki_cards_create",
        "anki_cards_update",
        "anki_cards_delete",
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
        "anki_decks_delete",
        "anki_cards_delete",
    ):
        assert by_name[name]["inputSchema"]["properties"]["confirm"]["type"] == "boolean"


def test_deck_and_card_crud_tools_work_through_json_rpc(client: TestClient) -> None:
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
    renamed = call("anki_decks_update", {"deck_id": deck["id"], "name": "CRUD Updated"})
    card = call(
        "anki_cards_create",
        {"deck_id": deck["id"], "front": "question", "back": "answer"},
    )
    changed = call(
        "anki_cards_update",
        {"card_id": card["id"], "front": "new question", "back": "new answer"},
    )
    card_deleted = call("anki_cards_delete", {"card_id": card["id"], "confirm": True})
    deck_deleted = call("anki_decks_delete", {"deck_id": deck["id"], "confirm": True})

    assert renamed["name"] == "CRUD Updated"
    assert changed["updated"] is True
    assert card_deleted["deleted"] is True
    assert deck_deleted["deleted"] is True


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
    ("tool", "arguments"),
    [
        ("anki_decks_delete", {"deck_id": 1, "confirm": 1}),
        ("anki_decks_delete", {"deck_id": 1, "confirm": "true"}),
        ("anki_cards_delete", {"card_id": 1, "confirm": 1}),
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
    assert "validation error" in result["content"][0]["text"].lower()


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
    assert "validation error" in result["content"][0]["text"].lower()


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
                "params": {"name": "anki_decks_create", "arguments": {"name": "Receipt"}},
            },
        )
    result = response.json()["result"]
    assert result.get("isError") is not True
    receipt = json.loads(result["content"][0]["text"])
    assert receipt["created"] is True
    collection = Collection(str(settings.collection_path))
    try:
        assert collection.decks.id_for_name("Receipt") == receipt["id"]
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
