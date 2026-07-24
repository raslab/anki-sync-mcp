from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator

import pytest
from anki.collection import Collection
from anki.sync import SyncAuth, SyncOutput

from anki_mcp.collection import AnkiCollectionService


@pytest.fixture
def populated_collection(tmp_path) -> Iterator[str]:
    path = str(tmp_path / "collection.anki2")
    collection = Collection(path)
    try:
        deck_id = collection.decks.id("Languages::Spanish")
        note = collection.new_note(collection.models.current())
        note["Front"] = "hola"
        note["Back"] = "hello"
        collection.add_note(note, deck_id)
        collection.save()
    finally:
        collection.close()
    yield path


@pytest.mark.anyio
async def test_lists_decks_with_hierarchy(populated_collection: str) -> None:
    async with AnkiCollectionService(populated_collection, max_page_size=100) as service:
        result = await service.list_decks(offset=0, limit=20)
    names = {deck["name"] for deck in result["items"]}
    assert {"Default", "Languages", "Languages::Spanish"}.issubset(names)
    assert result["offset"] == 0
    assert result["limit"] == 20
    assert result["total"] >= 3


@pytest.mark.anyio
async def test_gets_deck_by_stable_id(populated_collection: str) -> None:
    collection = Collection(populated_collection)
    try:
        deck_id = collection.decks.id_for_name("Languages::Spanish")
        assert deck_id is not None
    finally:
        collection.close()
    async with AnkiCollectionService(populated_collection, max_page_size=100) as service:
        result = await service.get_deck(deck_id)
    assert result["id"] == deck_id
    assert result["name"] == "Languages::Spanish"
    assert result["config"]["new_cards_per_day"] == 20
    assert result["config"]["reviews_per_day"] == 200


@pytest.mark.anyio
async def test_searches_and_gets_cards(populated_collection: str) -> None:
    async with AnkiCollectionService(populated_collection, max_page_size=100) as service:
        search = await service.search_cards(query="hola", offset=0, limit=10)
        assert search["total"] == 1
        card_id = search["items"][0]["id"]
        card = await service.get_card(card_id)
    assert card["id"] == card_id
    assert card["note_id"] > 0
    assert card["deck_name"] == "Languages::Spanish"
    assert card["question"]
    assert card["answer"]


@pytest.mark.anyio
async def test_page_size_is_bounded(populated_collection: str) -> None:
    async with AnkiCollectionService(populated_collection, max_page_size=2) as service:
        with pytest.raises(ValueError, match="limit"):
            await service.search_cards(query="", offset=0, limit=3)


@pytest.mark.anyio
async def test_search_scan_is_bounded_before_query(populated_collection: str) -> None:
    async with AnkiCollectionService(
        populated_collection, max_page_size=100, max_search_scan=0
    ) as service:
        with pytest.raises(ValueError, match="MCP_MAX_SEARCH_SCAN"):
            await service.search_cards(query="", offset=0, limit=10)


@pytest.mark.anyio
async def test_rendered_card_fields_are_bounded(populated_collection: str) -> None:
    async with AnkiCollectionService(
        populated_collection,
        max_page_size=100,
        max_rendered_field_bytes=64,
        max_card_fields=1,
    ) as service:
        search = await service.search_cards(query="hola", offset=0, limit=10)
        card = await service.get_card(search["items"][0]["id"])
        created = await service.create_card(card["deck_id"], "x" * 1000, "y" * 1000)
    assert len(card["question"].encode("utf-8")) <= 64
    assert len(card["answer"].encode("utf-8")) <= 64
    assert card["question_truncated"] is True
    assert card["answer_truncated"] is True
    assert all(len(value.encode("utf-8")) <= 64 for value in created["fields"].values())
    assert len(created["fields"]) == 1
    assert created["fields_omitted"] == 1
    assert all(created["fields_truncated"].values())


@pytest.mark.anyio
async def test_unknown_card_and_deck_are_not_found(populated_collection: str) -> None:
    async with AnkiCollectionService(populated_collection, max_page_size=100) as service:
        with pytest.raises(LookupError, match="deck"):
            await service.get_deck(999_999_999)
        with pytest.raises(LookupError, match="card"):
            await service.get_card(999_999_999)


@pytest.mark.anyio
async def test_default_deck_cannot_be_deleted(populated_collection: str) -> None:
    async with AnkiCollectionService(populated_collection, max_page_size=100) as service:
        with pytest.raises(ValueError, match="Default"):
            await service.delete_deck(1)
        assert (await service.get_deck(1))["name"] == "Default"


@pytest.mark.anyio
async def test_deck_crud_cycle(populated_collection: str) -> None:
    async with AnkiCollectionService(populated_collection, max_page_size=100) as service:
        created = await service.create_deck("Projects::Anki MCP")
        updated = await service.update_deck(created["id"], "Projects::Anki Server")
        deleted = await service.delete_deck(created["id"])
        with pytest.raises(LookupError, match="deck"):
            await service.get_deck(created["id"])

    assert created["name"] == "Projects::Anki MCP"
    assert updated["name"] == "Projects::Anki Server"
    assert deleted == {"id": created["id"], "deleted": True}


@pytest.mark.anyio
async def test_card_crud_cycle(populated_collection: str) -> None:
    collection = Collection(populated_collection)
    try:
        deck_id = collection.decks.id_for_name("Languages::Spanish")
        assert deck_id is not None
    finally:
        collection.close()

    async with AnkiCollectionService(populated_collection, max_page_size=100) as service:
        created = await service.create_card(deck_id, "adiós", "goodbye")
        updated = await service.update_card(created["id"], "hasta luego", "see you", None)
        deleted = await service.delete_card(created["id"])
        with pytest.raises(LookupError, match="card"):
            await service.get_card(created["id"])

    assert created["fields"] == {"Front": "adiós", "Back": "goodbye"}
    assert updated["fields"] == {"Front": "hasta luego", "Back": "see you"}
    assert deleted == {"id": created["id"], "deleted": True}


@pytest.mark.anyio
async def test_card_update_can_move_to_another_deck(populated_collection: str) -> None:
    async with AnkiCollectionService(populated_collection, max_page_size=100) as service:
        source = await service.create_deck("Source")
        target = await service.create_deck("Target")
        card = await service.create_card(source["id"], "front", "back")
        moved = await service.update_card(card["id"], None, None, target["id"])
    assert moved["deck_id"] == target["id"]


@pytest.mark.anyio
async def test_card_field_update_rejects_non_basic_note_type(populated_collection: str) -> None:
    collection = Collection(populated_collection)
    try:
        model = collection.models.by_name("Cloze")
        assert model is not None
        deck_id = collection.decks.id_for_name("Languages::Spanish")
        assert deck_id is not None
        note = collection.new_note(model)
        note["Text"] = "{{c1::hola}}"
        note["Back Extra"] = "hello"
        collection.add_note(note, deck_id)
        card_id = int(collection.find_cards(f"nid:{int(note.id)}")[0])
    finally:
        collection.close()

    async with AnkiCollectionService(populated_collection, max_page_size=100) as service:
        target = await service.create_deck("Must Not Move")
        with pytest.raises(ValueError, match="Basic"):
            await service.update_card(card_id, "changed", None, target["id"])
        unchanged = await service.get_card(card_id)
    assert unchanged["deck_id"] == deck_id


@pytest.mark.anyio
async def test_card_field_update_rejects_basic_copy_with_front_and_back(
    populated_collection: str,
) -> None:
    collection = Collection(populated_collection)
    try:
        basic = collection.models.by_name("Basic")
        assert basic is not None
        copied = collection.models.copy(basic)
        deck_id = collection.decks.id_for_name("Languages::Spanish")
        assert deck_id is not None
        note = collection.new_note(copied)
        note["Front"] = "copy front"
        note["Back"] = "copy back"
        collection.add_note(note, deck_id)
        card_id = int(collection.find_cards(f"nid:{int(note.id)}")[0])
    finally:
        collection.close()

    async with AnkiCollectionService(populated_collection, max_page_size=100) as service:
        with pytest.raises(ValueError, match="built-in Basic"):
            await service.update_card(card_id, "changed", None, None)


@pytest.mark.anyio
async def test_sync_login_uses_configured_credentials_and_sync_reuses_host_key(
    populated_collection: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    login_calls: list[tuple[str, str, str | None]] = []
    sync_auth: list[SyncAuth] = []

    def login(_: Collection, username: str, password: str, endpoint: str | None) -> SyncAuth:
        login_calls.append((username, password, endpoint))
        return SyncAuth(hkey="host-key-secret", endpoint=endpoint or "https://ankiweb.net/")

    def sync(_: Collection, auth: SyncAuth, sync_media: bool) -> SyncOutput:
        sync_auth.append(auth)
        return SyncOutput(required=1, server_message="complete", host_number=3)

    monkeypatch.setattr(Collection, "sync_login", login)
    monkeypatch.setattr(Collection, "sync_collection", sync)

    async with AnkiCollectionService(populated_collection, max_page_size=100) as service:
        logged_in = await service.sync_login(
            "sync-user", "sync-password", "https://sync.example.test/"
        )
        synced = await service.sync(sync_media=True)

    assert login_calls == [("sync-user", "sync-password", "https://sync.example.test/")]
    assert logged_in == {"authenticated": True, "endpoint": "https://sync.example.test/"}
    assert "host-key-secret" not in repr(logged_in)
    assert sync_auth[0].hkey == "host-key-secret"
    assert synced == {
        "required": "NORMAL_SYNC",
        "server_message": "complete",
        "server_message_truncated": False,
        "host_number": 3,
        "media_sync_requested": True,
    }


@pytest.mark.anyio
async def test_sync_requires_login(populated_collection: str) -> None:
    async with AnkiCollectionService(populated_collection, max_page_size=100) as service:
        with pytest.raises(RuntimeError, match="login"):
            await service.sync(sync_media=False)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("required", "expected"),
    [(2, "FULL_SYNC"), (3, "FULL_DOWNLOAD"), (4, "FULL_UPLOAD")],
)
async def test_full_sync_requirements_are_reported_without_choosing_direction(
    populated_collection: str,
    monkeypatch: pytest.MonkeyPatch,
    required: int,
    expected: str,
) -> None:
    calls = 0

    def login(_: Collection, username: str, password: str, endpoint: str | None) -> SyncAuth:
        return SyncAuth(hkey="host-key", endpoint=endpoint)

    def sync(_: Collection, auth: SyncAuth, sync_media: bool) -> SyncOutput:
        nonlocal calls
        calls += 1
        return SyncOutput(required=required)

    monkeypatch.setattr(Collection, "sync_login", login)
    monkeypatch.setattr(Collection, "sync_collection", sync)
    async with AnkiCollectionService(populated_collection, max_page_size=100) as service:
        await service.sync_login("user", "password", "https://sync.example.test")
        result = await service.sync(sync_media=False)
    assert result["required"] == expected
    assert calls == 1


@pytest.mark.anyio
async def test_failed_relogin_clears_previous_sync_auth(
    populated_collection: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0

    def login(_: Collection, username: str, password: str, endpoint: str | None) -> SyncAuth:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise RuntimeError("remote login rejected")
        return SyncAuth(hkey="first-host-key", endpoint=endpoint)

    monkeypatch.setattr(Collection, "sync_login", login)
    async with AnkiCollectionService(populated_collection, max_page_size=100) as service:
        await service.sync_login("user", "password", "https://sync.example.test")
        with pytest.raises(RuntimeError, match="rejected"):
            await service.sync_login("user", "wrong", "https://sync.example.test")
        with pytest.raises(RuntimeError, match="login"):
            await service.sync(sync_media=False)


@pytest.mark.anyio
async def test_failed_sync_invalidates_auth_and_bounds_remote_message(
    populated_collection: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sync_attempts = 0

    def login(_: Collection, username: str, password: str, endpoint: str | None) -> SyncAuth:
        return SyncAuth(hkey="host-key", endpoint=endpoint)

    def sync(_: Collection, auth: SyncAuth, sync_media: bool) -> SyncOutput:
        nonlocal sync_attempts
        sync_attempts += 1
        if sync_attempts == 1:
            return SyncOutput(required=0, server_message="x" * 1000)
        raise RuntimeError("remote sync failed")

    monkeypatch.setattr(Collection, "sync_login", login)
    monkeypatch.setattr(Collection, "sync_collection", sync)
    async with AnkiCollectionService(
        populated_collection, max_page_size=100, max_rendered_field_bytes=64
    ) as service:
        await service.sync_login("user", "password", "https://sync.example.test")
        result = await service.sync(sync_media=False)
        assert len(result["server_message"].encode()) <= 64
        assert result["server_message_truncated"] is True
        with pytest.raises(RuntimeError, match="failed"):
            await service.sync(sync_media=False)
        with pytest.raises(RuntimeError, match="login"):
            await service.sync(sync_media=False)


@pytest.mark.anyio
async def test_concurrent_operations_use_one_collection_thread(populated_collection: str) -> None:
    async with AnkiCollectionService(populated_collection, max_page_size=100) as service:
        assert service.executor.max_workers == 1
        worker_ids = await asyncio.gather(
            *(service.executor.run(lambda _: threading.get_ident()) for _ in range(10))
        )
    assert len(set(worker_ids)) == 1
    assert worker_ids[0] != threading.get_ident()
