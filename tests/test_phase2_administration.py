from __future__ import annotations

import base64
import json
import logging
import sqlite3
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from anki.collection import Collection, ImportAnkiPackageRequest
from anki.sync import SyncAuth, SyncOutput
from anki.sync_pb2 import MediaSyncProgress, MediaSyncStatusResponse

from anki_mcp.collection import AnkiCollectionService
from anki_mcp.state import PersistentState


@pytest.fixture
def phase2_collection(tmp_path: Path) -> Iterator[str]:
    path = str(tmp_path / "collection.anki2")
    collection = Collection(path)
    try:
        deck_id = collection.decks.id("Phase 2")
        for index in range(2):
            note = collection.new_note(collection.models.current())
            note["Front"] = f"phase two {index}"
            note["Back"] = "answer"
            collection.add_note(note, deck_id)
    finally:
        collection.close()
    yield path


@pytest.mark.anyio
async def test_deck_configuration_updates_supported_bounded_options(
    phase2_collection: str,
) -> None:
    collection = Collection(phase2_collection)
    try:
        deck_id = int(collection.decks.id_for_name("Phase 2") or 0)
    finally:
        collection.close()

    async with AnkiCollectionService(phase2_collection, max_page_size=100) as service:
        updated = await service.update_deck_config(
            deck_id,
            new_cards_per_day=17,
            reviews_per_day=123,
            max_answer_seconds=45,
            desired_retention=0.91,
        )
        deck = await service.get_deck(deck_id)
        with pytest.raises(ValueError, match="at least one"):
            await service.update_deck_config(deck_id)
        with pytest.raises(ValueError, match="desired_retention"):
            await service.update_deck_config(deck_id, desired_retention=1.5)

    assert updated == {"id": deck_id, "config_id": deck["config_id"], "updated": True}
    assert deck["config"] == {
        "name": "Default",
        "name_truncated": False,
        "new_cards_per_day": 17,
        "reviews_per_day": 123,
        "max_answer_seconds": 45,
        "desired_retention": 0.91,
    }


@pytest.mark.anyio
async def test_explicit_field_and_template_mappings_add_remove_rename_and_reorder(
    phase2_collection: str,
) -> None:
    async with AnkiCollectionService(phase2_collection, max_page_size=100) as service:
        created = await service.create_note_type(
            "Mapped",
            ["One", "Two", "Remove Me"],
            [
                {"name": "First", "question_format": "{{One}}", "answer_format": "{{Two}}"},
                {"name": "Remove Card", "question_format": "{{Two}}", "answer_format": "x"},
            ],
            "",
        )
        note_type_id = created["id"]
        fields = await service.update_note_type_fields(
            note_type_id,
            [
                {"name": "Second", "source_ordinal": 1},
                {"name": "Added", "source_ordinal": None},
                {"name": "First", "source_ordinal": 0},
            ],
        )
        templates = await service.update_templates(
            note_type_id,
            [
                {
                    "name": "Added Card",
                    "source_ordinal": None,
                    "question_format": "{{Added}}",
                    "answer_format": "{{First}}",
                },
                {
                    "name": "Renamed First",
                    "source_ordinal": 0,
                    "question_format": "{{First}}",
                    "answer_format": "{{Second}}",
                },
            ],
        )
        fetched = await service.get_note_type(note_type_id)

    assert fields == {"id": note_type_id, "updated": True, "field_count": 3}
    assert templates == {"id": note_type_id, "updated": True, "template_count": 2}
    assert [field["name"] for field in fetched["fields"]] == ["Second", "Added", "First"]
    assert [template["name"] for template in fetched["templates"]] == [
        "Added Card",
        "Renamed First",
    ]
    assert fetched["templates"][1]["answer_format"] == "{{Second}}"


@pytest.mark.anyio
async def test_card_flags_and_repositioning_are_bounded_bulk_operations(
    phase2_collection: str,
) -> None:
    collection = Collection(phase2_collection)
    try:
        card_ids = [int(card_id) for card_id in collection.find_cards('deck:"Phase 2"')]
    finally:
        collection.close()

    async with AnkiCollectionService(
        phase2_collection, max_page_size=100, max_batch_size=2
    ) as service:
        flagged = await service.set_card_flag(card_ids, 3)
        repositioned = await service.reposition_cards(
            card_ids,
            starting_from=10,
            step_size=2,
            randomize=False,
            shift_existing=True,
        )
        cards = [await service.get_card(card_id) for card_id in card_ids]
        with pytest.raises(ValueError, match="flag"):
            await service.set_card_flag(card_ids, 8)
        with pytest.raises(ValueError, match="starting_from"):
            await service.reposition_cards(
                card_ids,
                starting_from=0,
                step_size=1,
                randomize=False,
                shift_existing=True,
            )

    assert flagged == {"card_ids": card_ids, "flag": 3, "updated": len(card_ids)}
    assert repositioned == {"card_ids": card_ids, "repositioned": len(card_ids)}
    assert {card["flags"] for card in cards} == {3}
    assert sorted(card["scheduling"]["due"] for card in cards) == [10, 12]


@pytest.mark.anyio
async def test_media_consistency_check_returns_bounded_missing_and_unused_files(
    phase2_collection: str,
) -> None:
    collection = Collection(phase2_collection)
    try:
        deck_id = int(collection.decks.id_for_name("Phase 2") or 0)
        note = collection.new_note(collection.models.current())
        note["Front"] = '<img src="missing.png">'
        note["Back"] = "media"
        collection.add_note(note, deck_id)
    finally:
        collection.close()

    async with AnkiCollectionService(phase2_collection, max_page_size=100) as service:
        await service.store_media("unused.txt", base64.b64encode(b"unused").decode())
        checked = await service.check_media(offset=0, limit=10)

    assert checked["missing"] == ["missing.png"]
    assert checked["unused"] == ["unused.txt"]
    assert checked["missing_total"] == 1
    assert checked["unused_total"] == 1
    assert checked["has_more"] is False
    assert "report" not in checked


def test_legacy_receipts_get_timestamps_and_recent_updates_sort_first(tmp_path: Path) -> None:
    collection_path = tmp_path / "collection.anki2"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    database = sqlite3.connect(state_dir / "idempotency.sqlite")
    database.execute(
        """
        create table mutation_receipts (
            idempotency_key text primary key,
            operation text not null,
            request_hash text not null,
            receipt_json text not null
        )
        """
    )
    receipt = json.dumps({"state": "committed", "remote_synced": True})
    database.execute(
        "insert into mutation_receipts values (?, ?, ?, ?)",
        ("legacy", "legacy_operation", "legacy-hash", receipt),
    )
    database.commit()
    database.close()

    state = PersistentState(collection_path)
    try:
        legacy = state.get_operation("legacy")
        assert legacy is not None
        assert legacy["created_at"]
        assert legacy["updated_at"]
        state.put_receipt("newer", "newer_operation", "newer-hash", {"state": "committed"})
        time.sleep(0.001)
        state.put_receipt(
            "legacy",
            "legacy_operation",
            "legacy-hash",
            {"state": "committed", "remote_synced": True, "result": {"updated": True}},
        )
        operations, _ = state.list_operations(0, 10)
    finally:
        state.close()

    assert operations[0]["idempotency_key"] == "legacy"


def test_full_sync_reconciliation_updates_operation_timestamps(tmp_path: Path) -> None:
    state = PersistentState(tmp_path / "collection.anki2")
    try:
        state.put_receipt(
            "upload",
            "upload_operation",
            "upload-hash",
            {
                "state": "committed",
                "local_committed": True,
                "remote_synced": False,
                "media_synced": True,
            },
        )
        upload_before = state.get_operation("upload")
        assert upload_before is not None
        time.sleep(0.001)
        state.mark_all_remote_synced()
        upload_after = state.get_operation("upload")
        assert upload_after is not None
        assert upload_after["updated_at"] > upload_before["updated_at"]

        state.put_receipt(
            "download",
            "download_operation",
            "download-hash",
            {
                "state": "committed",
                "local_committed": True,
                "remote_synced": False,
                "media_synced": True,
            },
        )
        download_before = state.get_operation("download")
        assert download_before is not None
        time.sleep(0.001)
        state.mark_pending_discarded_by_full_download()
        download_after = state.get_operation("download")
    finally:
        state.close()

    assert download_after is not None
    assert download_after["updated_at"] > download_before["updated_at"]


@pytest.mark.anyio
async def test_operation_status_and_metrics_survive_service_restart(
    phase2_collection: str,
) -> None:
    async with AnkiCollectionService(phase2_collection, max_page_size=100) as service:
        created = await service.coordinated_mutation(
            "anki_decks_create",
            "phase2-operation",
            {"name": "Durable Operation"},
            lambda adapter: adapter.create_deck("Durable Operation"),
        )
        operation = await service.get_operation("phase2-operation")
        listed = await service.list_operations(offset=0, limit=10)
        metrics = await service.metrics()

    async with AnkiCollectionService(phase2_collection, max_page_size=100) as restarted:
        recovered = await restarted.get_operation("phase2-operation")
        recovered_metrics = await restarted.metrics()

    assert operation["idempotency_key"] == "phase2-operation"
    assert operation["operation"] == "anki_decks_create"
    assert operation["receipt"] == created
    assert listed["items"][0]["idempotency_key"] == "phase2-operation"
    assert metrics["mutations"]["total"] == 1
    assert metrics["mutations"]["by_operation"] == {"anki_decks_create": 1}
    assert recovered == operation
    assert recovered_metrics == metrics


@pytest.mark.anyio
async def test_media_sync_waits_for_completion_and_persists_progress(
    phase2_collection: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    statuses = iter(
        [
            MediaSyncStatusResponse(
                active=True, progress=MediaSyncProgress(checked="2", added="1", removed="0")
            ),
            MediaSyncStatusResponse(
                active=False, progress=MediaSyncProgress(checked="3", added="1", removed="1")
            ),
        ]
    )
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
    monkeypatch.setattr("anki._backend.RustBackend.media_sync_status", lambda self: next(statuses))

    async with AnkiCollectionService(phase2_collection, max_page_size=100) as service:
        await service.sync_login("user", "password", None)
        synced = await service.sync(sync_media=True)
        status = await service.status()
        metrics = await service.metrics()

    async with AnkiCollectionService(phase2_collection, max_page_size=100) as restarted:
        recovered = await restarted.status()

    assert synced["media_sync"] == {
        "completed": True,
        "checked": "3",
        "added": "1",
        "removed": "1",
    }
    assert status["last_media_sync_at"]
    assert recovered["last_media_sync_at"] == status["last_media_sync_at"]
    assert metrics["sync"]["last_successful_media_sync_at"] == status["last_media_sync_at"]
    assert metrics["sync"]["media_progress"] == {"checked": "3", "added": "1", "removed": "1"}


@pytest.mark.anyio
async def test_media_sync_timeout_preserves_persisted_sync_authentication(
    phase2_collection: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    aborts: list[bool] = []
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
        lambda self: MediaSyncStatusResponse(
            active=True, progress=MediaSyncProgress(checked="5", added="2", removed="1")
        ),
    )
    monkeypatch.setattr(Collection, "abort_media_sync", lambda self: aborts.append(True))

    async with AnkiCollectionService(
        phase2_collection, max_page_size=100, sync_timeout_seconds=0.001
    ) as service:
        await service.sync_login("user", "password", None)
        with pytest.raises(TimeoutError, match="media synchronization"):
            await service.sync(sync_media=True)
        status = await service.status()

    assert status["authenticated"] is True
    assert status["media_sync_progress"] == {"checked": "5", "added": "2", "removed": "1"}
    assert aborts == [True]


@pytest.mark.anyio
async def test_mutation_emits_content_free_structured_audit_event(
    phase2_collection: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="anki_mcp.audit")
    async with AnkiCollectionService(phase2_collection, max_page_size=100) as service:
        await service.coordinated_mutation(
            "anki_decks_create",
            "audit-operation",
            {"name": "sensitive deck content"},
            lambda adapter: adapter.create_deck("Audit Deck"),
        )

    event = json.loads(caplog.messages[-1])
    assert event["event"] == "anki_mutation"
    assert event["tool"] == "anki_decks_create"
    assert "idempotency_key" not in event
    assert len(event["operation_id"]) == 16
    assert event["local_committed"] is True
    assert event["remote_synced"] is True
    assert event["duration_ms"] >= 0
    assert "sensitive deck content" not in caplog.text
    assert "audit-operation" not in caplog.text


@pytest.mark.anyio
async def test_backup_can_restore_into_a_disposable_collection(
    phase2_collection: str, tmp_path: Path
) -> None:
    async with AnkiCollectionService(phase2_collection, max_page_size=100) as service:
        backup = await service.create_backup()

    backup_path = Path(backup["path"])
    assert backup_path.is_file()
    with zipfile.ZipFile(backup_path) as archive:
        assert "collection.anki21b" in archive.namelist()

    restored_path = tmp_path / "restored.anki2"
    restored = Collection(str(restored_path))
    try:
        restored.import_anki_package(ImportAnkiPackageRequest(package_path=str(backup_path)))
        assert restored.find_notes('"phase two 0"')
        assert restored.find_notes('"phase two 1"')
    finally:
        restored.close()
