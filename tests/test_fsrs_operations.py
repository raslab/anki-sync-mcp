from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from anki import scheduler_pb2
from anki._backend_generated import RustBackendGenerated
from anki.collection import Collection

from anki_mcp.collection import AnkiCollectionService


@pytest.fixture
def fsrs_collection(tmp_path: Path) -> Iterator[tuple[str, int]]:
    path = str(tmp_path / "collection.anki2")
    collection = Collection(path)
    try:
        deck_id = int(collection.decks.id("FSRS"))
        note = collection.new_note(collection.models.current())
        note["Front"] = "FSRS card"
        note["Back"] = "answer"
        collection.add_note(note, deck_id)
        card_id = int(collection.find_cards(f"nid:{int(note.id)}")[0])
        card = collection.get_card(card_id)
        card.type = 2
        card.queue = 2
        card.due = collection.sched.today + 100
        card.ivl = 100
        card.reps = 3
        card.factor = 2500
        collection.update_card(card)
        now_ms = int(time.time() * 1000)
        day_ms = 86_400_000
        collection.db.executemany(
            """
            insert into revlog(id, cid, usn, ease, ivl, lastIvl, factor, time, type)
            values (?, ?, -1, ?, ?, ?, 2500, 1000, 1)
            """,
            [
                (now_ms - 100 * day_ms, card_id, 3, 1, 0),
                (now_ms - 90 * day_ms, card_id, 3, 10, 1),
                (now_ms - 50 * day_ms, card_id, 3, 50, 10),
            ],
        )
        new_note = collection.new_note(collection.models.current())
        new_note["Front"] = "New FSRS card"
        new_note["Back"] = "answer"
        collection.add_note(new_note, deck_id)
    finally:
        collection.close()
    yield path, deck_id


@pytest.mark.anyio
async def test_scheduler_settings_can_enable_fsrs(
    fsrs_collection: tuple[str, int],
) -> None:
    path, deck_id = fsrs_collection

    async with AnkiCollectionService(path, max_page_size=100) as service:
        updated = await service.update_deck_scheduler_settings(None, None, fsrs_enabled=True)
        options = await service.get_deck_options(deck_id, ("global_settings",))

    assert updated == {
        "scope": "collection",
        "updated": True,
        "apply_all_parent_limits": False,
        "new_cards_ignore_review_limit": False,
        "fsrs_enabled": True,
    }
    assert options["sections"]["global_settings"]["fsrs"] is True


@pytest.mark.anyio
async def test_fsrs_simulator_uses_preset_defaults_and_is_compact(
    fsrs_collection: tuple[str, int],
) -> None:
    path, _ = fsrs_collection

    async with AnkiCollectionService(path, max_page_size=100) as service:
        compact = await service.simulate_fsrs(
            config_id=1,
            mode="review",
            deck_size=1_000,
            days_to_simulate=30,
            desired_retention=None,
            search=None,
            include_daily=False,
        )
        optimal = await service.simulate_fsrs(
            config_id=1,
            mode="optimal_retention",
            deck_size=1_000,
            days_to_simulate=365,
            desired_retention=None,
            search=None,
            include_daily=False,
        )

    assert compact["mode"] == "review"
    assert compact["config_id"] == 1
    assert compact["days"] == 30
    assert compact["summary"]["total_reviews"] >= 0
    assert compact["summary"]["total_new_cards"] >= 0
    assert "daily" not in compact
    assert optimal["mode"] == "optimal_retention"
    assert 0.7 <= optimal["optimal_retention"] <= 0.99


@pytest.mark.anyio
async def test_fsrs_optimizer_applies_native_parameters(
    fsrs_collection: tuple[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _ = fsrs_collection
    optimized = [0.2 + index / 100 for index in range(21)]

    def compute_fsrs_params(self, **kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["search"] == 'preset:"Default" -is:suspended'
        assert kwargs["health_check"] is True
        return scheduler_pb2.ComputeFsrsParamsResponse(
            params=optimized,
            fsrs_items=432,
            health_check_passed=True,
        )

    monkeypatch.setattr(RustBackendGenerated, "compute_fsrs_params", compute_fsrs_params)

    async with AnkiCollectionService(path, max_page_size=100) as service:
        result = await service.optimize_fsrs(1, search=None, health_check=True)
        preset = await service.get_deck_preset(1, ("fsrs",))

    assert result == {
        "config_id": 1,
        "optimized": True,
        "search": 'preset:"Default" -is:suspended',
        "training_items": 432,
        "health_check_passed": True,
        "previous_parameters": [],
        "parameters": pytest.approx(optimized),
        "affected_decks": 2,
    }
    assert preset["sections"]["fsrs"]["fsrs_params_6"] == pytest.approx(optimized)


@pytest.mark.anyio
async def test_fsrs_optimizer_rejects_insufficient_review_history(
    fsrs_collection: tuple[str, int],
) -> None:
    path, _ = fsrs_collection

    async with AnkiCollectionService(path, max_page_size=100) as service:
        with pytest.raises(ValueError, match="not enough review history"):
            await service.optimize_fsrs(1, search=None, health_check=True)


@pytest.mark.anyio
async def test_fsrs_operations_translate_invalid_searches(
    fsrs_collection: tuple[str, int],
) -> None:
    path, _ = fsrs_collection

    async with AnkiCollectionService(path, max_page_size=100) as service:
        with pytest.raises(ValueError, match="invalid FSRS search"):
            await service.optimize_fsrs(1, search="(", health_check=True)
        with pytest.raises(ValueError, match="invalid FSRS search"):
            await service.simulate_fsrs(1, "workload", 100, 30, None, "(", False)


@pytest.mark.anyio
async def test_fsrs_operations_bound_the_selected_search_not_the_collection(
    fsrs_collection: tuple[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _ = fsrs_collection
    optimized = [0.2 + index / 100 for index in range(21)]

    def compute_fsrs_params(self, **kwargs):  # type: ignore[no-untyped-def]
        return scheduler_pb2.ComputeFsrsParamsResponse(
            params=optimized,
            fsrs_items=1,
            health_check_passed=True,
        )

    monkeypatch.setattr(RustBackendGenerated, "compute_fsrs_params", compute_fsrs_params)

    async with AnkiCollectionService(
        path, max_page_size=100, max_search_scan=1
    ) as service:
        optimized_result = await service.optimize_fsrs(
            1, search="deck:FSRS -is:new", health_check=True
        )
        simulated = await service.simulate_fsrs(
            1, "review", 100, 30, None, "deck:FSRS -is:new", False
        )

    assert optimized_result["training_items"] == 1
    assert simulated["summary"]["total_reviews"] >= 0


@pytest.mark.anyio
async def test_fsrs_reschedule_previews_and_applies_changed_retention(
    fsrs_collection: tuple[str, int],
) -> None:
    path, _ = fsrs_collection

    async with AnkiCollectionService(path, max_page_size=100) as service:
        await service.update_deck_scheduler_settings(None, None, fsrs_enabled=True)
        card_id = (await service.search_cards("deck:FSRS -is:new", 0, 1))["items"][0]["id"]
        before = await service.get_card(card_id)
        preview = await service.preview_fsrs_reschedule(1, 0.91, None)
        result = await service.reschedule_fsrs(1, 0.91, None)
        after = await service.get_card(card_id)
        preset = await service.get_deck_preset(1)

    assert preview["config_id"] == 1
    assert preview["cards"] == 1
    assert preview["decks"] == 2
    assert len(preview["state_fingerprint"]) == 64
    assert result == {
        "config_id": 1,
        "rescheduled": True,
        "cards": 1,
        "decks": 2,
        "desired_retention": 0.91,
        "parameters_changed": False,
    }
    assert after["scheduling"]["due"] != before["scheduling"]["due"]
    assert preset["desired_retention"] == pytest.approx(0.91)


@pytest.mark.anyio
async def test_fsrs_reschedule_fingerprint_tracks_card_eligibility(
    fsrs_collection: tuple[str, int],
) -> None:
    path, _ = fsrs_collection

    async with AnkiCollectionService(path, max_page_size=100) as service:
        await service.update_deck_scheduler_settings(None, None, fsrs_enabled=True)
        review_card = (await service.search_cards("deck:FSRS -is:new", 0, 1))["items"][0]
        before = await service.preview_fsrs_reschedule(1, 0.91, None)
        await service.suspend_cards([review_card["id"]])
        after = await service.preview_fsrs_reschedule(1, 0.91, None)

    assert before["cards"] == 1
    assert after["cards"] == 0
    assert after["state_fingerprint"] != before["state_fingerprint"]


@pytest.mark.anyio
async def test_fsrs_reschedule_requires_an_actual_fsrs_change(
    fsrs_collection: tuple[str, int],
) -> None:
    path, _ = fsrs_collection

    async with AnkiCollectionService(path, max_page_size=100) as service:
        with pytest.raises(ValueError, match="must change"):
            await service.preview_fsrs_reschedule(1, 0.9, None)


@pytest.mark.anyio
async def test_fsrs_reschedule_preview_requires_fsrs_to_be_enabled(
    fsrs_collection: tuple[str, int],
) -> None:
    path, _ = fsrs_collection

    async with AnkiCollectionService(path, max_page_size=100) as service:
        with pytest.raises(ValueError, match="must be enabled"):
            await service.preview_fsrs_reschedule(1, 0.91, None)
