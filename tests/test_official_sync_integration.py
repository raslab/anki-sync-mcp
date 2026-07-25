from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from anki.collection import Collection

from anki_mcp.collection import AnkiCollectionService, FullSyncRequiredError


def _free_loopback_port() -> int:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


@pytest.fixture
def official_sync_server(tmp_path: Path) -> Iterator[str]:
    """Run the sync server shipped by the pinned official Anki package."""
    port = _free_loopback_port()
    sync_base = tmp_path / "sync-server"
    sync_base.mkdir()
    environment = {
        **os.environ,
        "SYNC_BASE": str(sync_base),
        "SYNC_HOST": "127.0.0.1",
        "SYNC_PORT": str(port),
        "SYNC_USER1": "phase1-user:phase1-password",
        "RUST_LOG": "anki=warn",
    }
    process = subprocess.Popen(  # noqa: S603 - fixed interpreter/module, test-only environment
        [sys.executable, "-m", "anki.syncserver"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout is not None else ""
            pytest.fail(f"official sync server exited during startup: {output}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        process.terminate()
        pytest.fail("official sync server did not become ready")

    try:
        yield f"http://127.0.0.1:{port}/"
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_complete_phase1_lifecycle_against_official_sync_server(
    official_sync_server: str, tmp_path: Path
) -> None:
    """Prove full-sync gating, write sync, recovery, and idempotency end to end."""
    client_a_path = str(tmp_path / "client-a" / "collection.anki2")
    Path(client_a_path).parent.mkdir()
    collection = Collection(client_a_path)
    try:
        model = collection.models.by_name("Basic")
        assert model is not None
        note_type_id = int(model["id"])
    finally:
        collection.close()

    note_id = 0
    original_receipt: dict[str, object] | None = None

    async def write_from_client_a() -> None:
        nonlocal note_id, original_receipt
        async with AnkiCollectionService(
            client_a_path, max_page_size=100, sync_on_write=True
        ) as service:
            await service.sync_login("phase1-user", "phase1-password", official_sync_server)
            initial = await service.sync(sync_media=False)
            assert initial["required"] == "NO_CHANGES"
            with pytest.raises(FullSyncRequiredError, match="FULL_UPLOAD"):
                await service.coordinated_mutation(
                    operation="anki_notes_create",
                    idempotency_key="official-lifecycle-key",
                    request={"front": "official lifecycle"},
                    mutate=lambda adapter: adapter.create_note(
                        1,
                        note_type_id,
                        {"Front": "official lifecycle", "Back": "first value"},
                        ["phase1"],
                    ),
                )
            blocked = await service.status()
            assert blocked["pending_full_sync"] == "FULL_UPLOAD"
            assert blocked["readiness_reason"] == "full_sync_required"
            assert blocked["pending_mutations"] == 1

            full = await service.full_sync(upload=True)
            assert full["completed"] is True
            assert full["direction"] == "upload"
            original_receipt = await service.coordinated_mutation(
                operation="anki_notes_create",
                idempotency_key="official-lifecycle-key",
                request={"front": "official lifecycle"},
                mutate=lambda adapter: (_ for _ in ()).throw(
                    AssertionError("local mutation was replayed")
                ),
            )
            assert original_receipt["remote_synced"] is True
            note_id = int(original_receipt["result"]["note_id"])  # type: ignore[index]

    asyncio.run(write_from_client_a())

    async def verify_restart_and_second_client() -> None:
        async with AnkiCollectionService(
            client_a_path, max_page_size=100, sync_on_write=True
        ) as restarted:
            status = await restarted.status()
            assert status["authenticated"] is True
            assert status["pending_mutations"] == 0
            repeated = await restarted.coordinated_mutation(
                operation="anki_notes_create",
                idempotency_key="official-lifecycle-key",
                request={"front": "official lifecycle"},
                mutate=lambda adapter: (_ for _ in ()).throw(
                    AssertionError("local mutation was replayed after restart")
                ),
            )
            assert repeated == original_receipt

        client_b_path = str(tmp_path / "client-b" / "collection.anki2")
        Path(client_b_path).parent.mkdir()
        Collection(client_b_path).close()
        async with AnkiCollectionService(
            client_b_path, max_page_size=100, sync_on_write=True
        ) as second_client:
            await second_client.sync_login("phase1-user", "phase1-password", official_sync_server)
            required = await second_client.sync(sync_media=False)
            assert required["required"] == "FULL_DOWNLOAD"
            await second_client.full_sync(upload=False)
            downloaded = await second_client.get_note(note_id)
            assert downloaded["id"] == note_id

            updated = await second_client.coordinated_mutation(
                operation="anki_notes_update_fields",
                idempotency_key="official-update-key",
                request={"note_id": note_id, "fields": {"Back": "second value"}},
                mutate=lambda adapter: adapter.update_note_fields(
                    note_id, {"Back": "second value"}
                ),
            )
            assert updated["local_committed"] is True
            assert updated["remote_synced"] is True

        async with AnkiCollectionService(client_a_path, max_page_size=100) as first_client:
            refreshed = await first_client.coordinated_read(
                lambda adapter: adapter.get_note(note_id), sync_before=True
            )
        fields = {item["name"]: item["value"] for item in refreshed["fields"]}
        assert fields["Back"] == "second value"

    asyncio.run(verify_restart_and_second_client())
