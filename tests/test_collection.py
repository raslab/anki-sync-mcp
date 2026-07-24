from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator

import pytest
from anki.collection import Collection

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
        populated_collection, max_page_size=100, max_rendered_field_bytes=64
    ) as service:
        search = await service.search_cards(query="hola", offset=0, limit=10)
        card = await service.get_card(search["items"][0]["id"])
    assert len(card["question"].encode("utf-8")) <= 64
    assert len(card["answer"].encode("utf-8")) <= 64
    assert card["question_truncated"] is True
    assert card["answer_truncated"] is True


@pytest.mark.anyio
async def test_unknown_card_and_deck_are_not_found(populated_collection: str) -> None:
    async with AnkiCollectionService(populated_collection, max_page_size=100) as service:
        with pytest.raises(LookupError, match="deck"):
            await service.get_deck(999_999_999)
        with pytest.raises(LookupError, match="card"):
            await service.get_card(999_999_999)


@pytest.mark.anyio
async def test_concurrent_operations_use_one_collection_thread(populated_collection: str) -> None:
    async with AnkiCollectionService(populated_collection, max_page_size=100) as service:
        assert service.executor.max_workers == 1
        worker_ids = await asyncio.gather(
            *(service.executor.run(lambda _: threading.get_ident()) for _ in range(10))
        )
    assert len(set(worker_ids)) == 1
    assert worker_ids[0] != threading.get_ident()
