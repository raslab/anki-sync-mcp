from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from anki.collection import Collection

from anki_mcp.collection import AnkiCollectionService


@pytest.fixture
def reviewed_collection(tmp_path: Path) -> Iterator[tuple[str, int, int, int]]:
    path = str(tmp_path / "collection.anki2")
    collection = Collection(path)
    try:
        parent_id = int(collection.decks.id("History"))
        child_id = int(collection.decks.id("History::Child"))
        model = collection.models.current()

        parent_note = collection.new_note(model)
        parent_note["Front"] = "parent review"
        parent_note["Back"] = "answer"
        collection.add_note(parent_note, parent_id)
        parent_card_id = int(collection.find_cards(f"nid:{int(parent_note.id)}")[0])

        child_note = collection.new_note(model)
        child_note["Front"] = "child review"
        child_note["Back"] = "answer"
        collection.add_note(child_note, child_id)
        child_card_id = int(collection.find_cards(f"nid:{int(child_note.id)}")[0])

        now_ms = int(time.time() * 1000)
        collection.db.executemany(
            """
            insert into revlog(id, cid, usn, ease, ivl, lastIvl, factor, time, type)
            values (?, ?, -1, ?, ?, ?, ?, ?, ?)
            """,
            [
                (now_ms - 2_000, parent_card_id, 1, 60, 0, 2500, 1_250, 0),
                (now_ms - 1_000, parent_card_id, 3, 1, 1, 2450, 2_500, 1),
                (now_ms, child_card_id, 4, 86_400, 600, 2400, 3_750, 2),
            ],
        )
        parent_card = collection.get_card(parent_card_id)
        parent_card.reps = 2
        collection.update_card(parent_card)
    finally:
        collection.close()
    yield path, parent_id, parent_card_id, child_card_id


@pytest.mark.anyio
async def test_card_get_includes_lifetime_review_and_fsrs_summary(
    reviewed_collection: tuple[str, int, int, int],
) -> None:
    path, _, card_id, _ = reviewed_collection

    async with AnkiCollectionService(path, max_page_size=100) as service:
        card = await service.get_card(card_id)

    assert card["review_summary"]["added_at"] > 0
    assert card["review_summary"]["first_review_at"] > 0
    assert card["review_summary"]["latest_review_at"] >= card["review_summary"]["first_review_at"]
    assert card["review_summary"]["reviews"] == 2
    assert card["review_summary"]["total_answer_seconds"] == pytest.approx(3.75)
    assert card["review_summary"]["average_answer_seconds"] == pytest.approx(1.875)
    assert card["review_summary"]["preset"] == "Default"
    assert "original_deck" in card["review_summary"]
    assert "memory_state" in card["fsrs"]
    assert "retrievability" in card["fsrs"]
    assert "desired_retention" in card["fsrs"]


@pytest.mark.anyio
async def test_reviews_list_supports_card_scope_and_stable_pagination(
    reviewed_collection: tuple[str, int, int, int],
) -> None:
    path, _, card_id, _ = reviewed_collection

    async with AnkiCollectionService(path, max_page_size=100) as service:
        newest = await service.list_reviews(
            card_id=card_id,
            deck_id=None,
            query=None,
            include_children=False,
            offset=0,
            limit=1,
            order="newest",
        )
        oldest = await service.list_reviews(
            card_id=card_id,
            deck_id=None,
            query=None,
            include_children=False,
            offset=0,
            limit=1,
            order="oldest",
        )

    assert newest["scope"] == {"kind": "card", "card_id": card_id}
    assert newest["attribution"] == "exact_card"
    assert newest["total"] == 2
    assert newest["has_more"] is True
    assert newest["items"][0]["rating"] == 3
    assert newest["items"][0]["rating_label"] == "Good"
    assert newest["items"][0]["review_kind"] == "review"
    assert newest["items"][0]["answer_seconds"] == pytest.approx(2.5)
    assert newest["items"][0]["interval_seconds"] == 86_400
    assert newest["items"][0]["previous_interval_seconds"] == 86_400
    assert newest["items"][0]["card_id"] == card_id
    assert oldest["items"][0]["rating"] == 1


@pytest.mark.anyio
async def test_reviews_list_supports_deck_children_and_query_scopes(
    reviewed_collection: tuple[str, int, int, int],
) -> None:
    path, deck_id, parent_card_id, child_card_id = reviewed_collection

    async with AnkiCollectionService(path, max_page_size=100) as service:
        exact_deck = await service.list_reviews(
            card_id=None,
            deck_id=deck_id,
            query=None,
            include_children=False,
            offset=0,
            limit=10,
            order="newest",
        )
        deck_tree = await service.list_reviews(
            card_id=None,
            deck_id=deck_id,
            query=None,
            include_children=True,
            offset=0,
            limit=10,
            order="newest",
        )
        query = await service.list_reviews(
            card_id=None,
            deck_id=None,
            query='"child review"',
            include_children=False,
            offset=0,
            limit=10,
            order="newest",
        )

    assert {item["card_id"] for item in exact_deck["items"]} == {parent_card_id}
    assert {item["card_id"] for item in deck_tree["items"]} == {
        parent_card_id,
        child_card_id,
    }
    assert deck_tree["attribution"] == "current_card_membership"
    assert query["scope"] == {"kind": "query", "query": '"child review"'}
    assert [item["card_id"] for item in query["items"]] == [child_card_id]


@pytest.mark.anyio
async def test_review_scope_and_scan_bounds_are_enforced(
    reviewed_collection: tuple[str, int, int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, deck_id, card_id, _ = reviewed_collection

    async with AnkiCollectionService(path, max_page_size=100, max_search_scan=2) as service:
        with pytest.raises(ValueError, match="exactly one"):
            await service.list_reviews(None, None, None, False, 0, 10, "newest")
        with pytest.raises(ValueError, match="exactly one"):
            await service.list_reviews(card_id, deck_id, None, False, 0, 10, "newest")
        with pytest.raises(ValueError, match="include_children"):
            await service.list_reviews(card_id, None, None, True, 0, 10, "newest")
        with pytest.raises(ValueError, match="MCP_MAX_SEARCH_SCAN"):
            await service.list_reviews(None, deck_id, None, True, 0, 10, "newest")

    monkeypatch.setattr(
        Collection,
        "card_stats_data",
        lambda self, card_id: pytest.fail("review listing must not load unbounded card stats"),
    )
    async with AnkiCollectionService(path, max_page_size=100) as service:
        listed = await service.list_reviews(card_id, None, None, False, 0, 10, "newest")
    assert listed["total"] == 2


@pytest.mark.anyio
async def test_review_stats_supports_card_deck_and_query_scopes(
    reviewed_collection: tuple[str, int, int, int],
) -> None:
    path, deck_id, card_id, _ = reviewed_collection

    async with AnkiCollectionService(path, max_page_size=100) as service:
        card_stats = await service.review_stats(card_id, None, None, False, 30)
        deck_stats = await service.review_stats(None, deck_id, None, True, 30)
        query_stats = await service.review_stats(None, None, '"child review"', False, 30)

    assert card_stats["scope"] == {"kind": "card", "card_id": card_id}
    assert card_stats["attribution"] == "exact_card"
    assert card_stats["days"] == 30
    assert card_stats["graphs"]["today"]["answer_count"] >= 0
    assert "reviews" in card_stats["graphs"]
    assert deck_stats["scope"] == {
        "kind": "deck",
        "deck_id": deck_id,
        "include_children": True,
    }
    assert deck_stats["attribution"] == "current_card_membership"
    assert query_stats["scope"] == {"kind": "query", "query": '"child review"'}


@pytest.mark.anyio
async def test_review_stats_rejects_invalid_scope_and_days(
    reviewed_collection: tuple[str, int, int, int],
) -> None:
    path, _, card_id, _ = reviewed_collection

    async with AnkiCollectionService(path, max_page_size=100) as service:
        with pytest.raises(ValueError, match="exactly one"):
            await service.review_stats(None, None, None, False, 30)
        with pytest.raises(ValueError, match="days"):
            await service.review_stats(card_id, None, None, False, 7)
        with pytest.raises(ValueError, match="include_children"):
            await service.review_stats(card_id, None, None, True, 30)
