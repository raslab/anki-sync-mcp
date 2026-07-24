from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar, cast

from anki.collection import Collection
from anki.errors import NotFoundError
from anki.sync import SyncAuth

from anki_mcp.config import validate_sync_endpoint

if TYPE_CHECKING:
    from anki.cards import CardId
    from anki.decks import DeckId

T = TypeVar("T")
SYNC_REQUIRED_NAMES = (
    "NO_CHANGES",
    "NORMAL_SYNC",
    "FULL_SYNC",
    "FULL_DOWNLOAD",
    "FULL_UPLOAD",
)


class SyncLoginRequiredError(RuntimeError):
    """Raised when synchronization is requested before remote login."""


class CollectionAdapter:
    """Synchronous adapter; an instance must only be used by its owning thread."""

    def __init__(
        self,
        path: str | Path,
        max_page_size: int,
        max_search_scan: int = 10_000,
        max_rendered_field_bytes: int = 262_144,
        max_card_fields: int = 100,
    ) -> None:
        self.collection = Collection(str(path))
        self.max_page_size = max_page_size
        self.max_search_scan = max_search_scan
        self.max_rendered_field_bytes = max_rendered_field_bytes
        self.max_card_fields = max_card_fields
        self._backup_folder = Path(path).parent / "backups"
        self._sync_auth: SyncAuth | None = None
        self._pending_full_sync: tuple[int, int | None] | None = None

    def close(self) -> None:
        self.collection.close()

    def check_ready(self) -> bool:
        if self.collection.db is None:
            return False
        return self.collection.db.scalar("select 1") == 1

    def _page(self, values: list[Any], offset: int, limit: int) -> dict[str, Any]:
        if offset < 0:
            raise ValueError("offset must be non-negative")
        if limit < 1 or limit > self.max_page_size:
            raise ValueError(f"limit must be between 1 and {self.max_page_size}")
        total = len(values)
        return {
            "items": values[offset : offset + limit],
            "offset": offset,
            "limit": limit,
            "total": total,
            "has_more": offset + limit < total,
        }

    def list_decks(self, offset: int, limit: int) -> dict[str, Any]:
        if self.collection.decks.count() > self.max_search_scan:
            raise ValueError(
                "collection exceeds MCP_MAX_SEARCH_SCAN; use a larger configured bound"
            )
        decks = []
        for item in sorted(self.collection.decks.all_names_and_ids(), key=lambda d: d.name):
            name, name_truncated = self._truncate_rendered(item.name)
            decks.append(
                {
                    "id": int(item.id),
                    "name": name,
                    "name_truncated": name_truncated,
                    "hierarchy": name.split("::"),
                }
            )
        return self._page(decks, offset, limit)

    def get_deck(self, deck_id: int) -> dict[str, Any]:
        deck = self.collection.decks.get(cast("DeckId", deck_id), default=False)
        if deck is None:
            raise LookupError(f"deck {deck_id} not found")
        name, name_truncated = self._truncate_rendered(str(deck["name"]))
        description, description_truncated = self._truncate_rendered(str(deck.get("desc", "")))
        result: dict[str, Any] = {
            "id": int(deck["id"]),
            "name": name,
            "name_truncated": name_truncated,
            "hierarchy": name.split("::"),
            "modified": int(deck.get("mod", 0)),
            "description": description,
            "description_truncated": description_truncated,
            "dynamic": bool(deck.get("dyn", 0)),
        }
        if not result["dynamic"]:
            result["config_id"] = int(deck.get("conf", 1))
            config = self.collection.decks.config_dict_for_deck_id(cast("DeckId", deck_id))
            config_name, config_name_truncated = self._truncate_rendered(
                str(config.get("name", ""))
            )
            result["config"] = {
                "name": config_name,
                "name_truncated": config_name_truncated,
                "new_cards_per_day": int(config.get("new", {}).get("perDay", 0)),
                "reviews_per_day": int(config.get("rev", {}).get("perDay", 0)),
                "max_answer_seconds": int(config.get("maxTaken", 0)),
                "desired_retention": float(config.get("desiredRetention", 0.9)),
            }
        return result

    def create_deck(self, name: str) -> dict[str, Any]:
        if not name.strip():
            raise ValueError("deck name must not be blank")
        existing = self.collection.decks.id_for_name(name)
        if existing is not None:
            return {"id": int(existing), "name": name, "created": False}
        deck_id = self.collection.decks.id(name)
        if deck_id is None:  # pragma: no cover - creation returns an ID
            raise RuntimeError("Anki did not return a deck ID")
        return {"id": int(deck_id), "name": name, "created": True}

    def update_deck(self, deck_id: int, name: str) -> dict[str, Any]:
        self.get_deck(deck_id)
        if not name.strip():
            raise ValueError("deck name must not be blank")
        self.collection.decks.rename(cast("DeckId", deck_id), name)
        return {"id": deck_id, "name": name, "updated": True}

    def delete_deck(self, deck_id: int) -> dict[str, Any]:
        self.get_deck(deck_id)
        if deck_id == 1:
            raise ValueError("the Default deck cannot be deleted")
        self.collection.decks.remove([cast("DeckId", deck_id)])
        return {"id": deck_id, "deleted": True}

    def search_cards(self, query: str, offset: int, limit: int) -> dict[str, Any]:
        # Validate bounds before potentially expensive collection search.
        self._page([], offset, limit)
        if int(self.collection.card_count()) > self.max_search_scan:
            raise ValueError(
                "collection exceeds MCP_MAX_SEARCH_SCAN; use a larger configured bound"
            )
        card_ids = [int(card_id) for card_id in self.collection.find_cards(query)]
        result = self._page(card_ids, offset, limit)
        result["items"] = [self._card_summary(card_id) for card_id in result["items"]]
        return result

    def _card_summary(self, card_id: int) -> dict[str, Any]:
        card = self.collection.get_card(cast("CardId", card_id))
        return {"id": int(card.id), "note_id": int(card.nid), "deck_id": int(card.did)}

    def get_card(self, card_id: int) -> dict[str, Any]:
        try:
            card = self.collection.get_card(cast("CardId", card_id))
        except NotFoundError as exc:
            raise LookupError(f"card {card_id} not found") from exc
        deck = self.collection.decks.get(card.did, default=False)
        if deck is None:
            raise LookupError(f"deck for card {card_id} not found")
        question, question_truncated = self._truncate_rendered(card.question())
        answer, answer_truncated = self._truncate_rendered(card.answer())
        deck_name, deck_name_truncated = self._truncate_rendered(str(deck["name"]))
        note = card.note()
        fields: list[dict[str, Any]] = []
        note_items = note.items()
        for name, value in note_items[: self.max_card_fields]:
            bounded_name, name_truncated = self._truncate_rendered(name)
            bounded_value, value_truncated = self._truncate_rendered(value)
            fields.append(
                {
                    "name": bounded_name,
                    "name_truncated": name_truncated,
                    "value": bounded_value,
                    "value_truncated": value_truncated,
                }
            )
        return {
            "id": int(card.id),
            "note_id": int(card.nid),
            "deck_id": int(card.did),
            "deck_name": deck_name,
            "deck_name_truncated": deck_name_truncated,
            "template_ordinal": int(card.ord),
            "flags": int(card.flags),
            "modified": int(card.mod),
            "question": question,
            "question_truncated": question_truncated,
            "answer": answer,
            "answer_truncated": answer_truncated,
            "fields": fields,
            "fields_omitted": max(0, len(note_items) - self.max_card_fields),
            "scheduling": {
                "type": int(card.type),
                "queue": int(card.queue),
                "due": int(card.due),
                "interval": int(card.ivl),
                "ease_factor": int(card.factor),
                "reps": int(card.reps),
                "lapses": int(card.lapses),
                "left": int(card.left),
            },
        }

    def _basic_model(self) -> dict[str, Any]:
        model = self.collection.models.by_name("Basic")
        if model is None:
            raise RuntimeError("Basic note type is unavailable")
        fields = model.get("flds", [])
        templates = model.get("tmpls", [])
        supported = (
            int(model.get("type", -1)) == 0
            and [(field.get("name"), field.get("ord")) for field in fields]
            == [("Front", 0), ("Back", 1)]
            and len(templates) == 1
            and int(templates[0].get("ord", -1)) == 0
        )
        if not supported:
            raise ValueError("card operations require the built-in Basic single-card note type")
        return model

    def create_card(self, deck_id: int, front: str, back: str) -> dict[str, Any]:
        self.get_deck(deck_id)
        if not front.strip():
            raise ValueError("front must not be blank")
        model = self._basic_model()
        note = self.collection.new_note(model)
        note["Front"] = front
        note["Back"] = back
        self.collection.add_note(note, cast("DeckId", deck_id))
        try:
            card_ids = self.collection.find_cards(f"nid:{int(note.id)}")
            if len(card_ids) != 1:
                raise ValueError("Basic note must generate exactly one card")
        except Exception:
            self.collection.remove_notes([note.id])
            raise
        return {
            "id": int(card_ids[0]),
            "note_id": int(note.id),
            "deck_id": deck_id,
            "created": True,
        }

    def update_card(
        self, card_id: int, front: str | None, back: str | None, deck_id: int | None
    ) -> dict[str, Any]:
        try:
            card = self.collection.get_card(cast("CardId", card_id))
        except NotFoundError as exc:
            raise LookupError(f"card {card_id} not found") from exc
        if front is None and back is None and deck_id is None:
            raise ValueError("at least one card field or deck_id must be supplied")
        if front is not None and not front.strip():
            raise ValueError("front must not be blank")
        if deck_id is not None:
            self.get_deck(deck_id)
        note = card.note()
        basic = self._basic_model()
        basic_id = int(basic["id"])
        sibling_cards = self.collection.find_cards(f"nid:{int(note.id)}")
        if int(note.mid) != basic_id or len(sibling_cards) != 1:
            raise ValueError("card updates require the built-in Basic single-card note type")
        previous_front = note["Front"]
        previous_back = note["Back"]
        note_changed = front is not None or back is not None
        if note_changed:
            if front is not None:
                note["Front"] = front
            if back is not None:
                note["Back"] = back
            self.collection.update_note(note)
            if len(self.collection.find_cards(f"nid:{int(note.id)}")) != 1:
                note["Front"] = previous_front
                note["Back"] = previous_back
                self.collection.update_note(note)
                raise ValueError("Basic note update must retain exactly one card")
        try:
            if deck_id is not None:
                self.collection.set_deck([cast("CardId", card_id)], deck_id)
        except Exception:
            if note_changed:
                note["Front"] = previous_front
                note["Back"] = previous_back
                self.collection.update_note(note)
            raise
        return {
            "id": card_id,
            "note_id": int(note.id),
            "deck_id": deck_id if deck_id is not None else int(card.did),
            "updated": True,
        }

    def delete_card(self, card_id: int) -> dict[str, Any]:
        self.get_card(card_id)
        self.collection.remove_cards_and_orphaned_notes([cast("CardId", card_id)])
        return {"id": card_id, "deleted": True}

    def sync_login(self, username: str, password: str, endpoint: str | None) -> dict[str, Any]:
        self._sync_auth = None
        self._pending_full_sync = None
        self._sync_auth = self.collection.sync_login(username, password, endpoint)
        return {"authenticated": True, "endpoint_kind": "custom" if endpoint else "ankiweb"}

    def sync(self, sync_media: bool) -> dict[str, Any]:
        if self._sync_auth is None:
            raise SyncLoginRequiredError("sync login is required before synchronization")
        try:
            output = self.collection.sync_collection(self._sync_auth, sync_media)
            endpoint_changed = bool(output.new_endpoint)
            if endpoint_changed:
                self._sync_auth.endpoint = validate_sync_endpoint(output.new_endpoint)
            if output.required in {2, 3, 4}:
                self._pending_full_sync = (
                    output.required,
                    output.server_media_usn if sync_media else None,
                )
            else:
                self._pending_full_sync = None
            required = SYNC_REQUIRED_NAMES[output.required]
            server_message, server_message_truncated = self._truncate_rendered(
                output.server_message
            )
            return {
                "required": required,
                "server_message": server_message,
                "server_message_truncated": server_message_truncated,
                "host_number": output.host_number,
                "media_sync_requested": sync_media,
                "endpoint_changed": endpoint_changed,
            }
        except Exception:
            self._sync_auth = None
            self._pending_full_sync = None
            raise

    def full_sync(self, upload: bool) -> dict[str, Any]:
        if self._sync_auth is None:
            raise SyncLoginRequiredError("sync login is required before full synchronization")
        if self._pending_full_sync is None:
            raise ValueError("a full sync was not requested by the remote server")
        required, server_usn = self._pending_full_sync
        if required == 3 and upload:
            raise ValueError("the remote server requires a full download")
        if required == 4 and not upload:
            raise ValueError("the remote server requires a full upload")
        try:
            self._backup_folder.mkdir(parents=True, exist_ok=True)
            backup_created = self.collection.create_backup(
                backup_folder=str(self._backup_folder),
                force=True,
                wait_for_completion=True,
            )
            self.collection.full_upload_or_download(
                auth=self._sync_auth,
                server_usn=server_usn,
                upload=upload,
            )
        except Exception:
            self._sync_auth = None
            self._pending_full_sync = None
            raise
        self._pending_full_sync = None
        return {
            "completed": True,
            "direction": "upload" if upload else "download",
            "backup_created": backup_created,
        }

    def _truncate_rendered(self, value: str) -> tuple[str, bool]:
        encoded = value.encode("utf-8")
        if len(encoded) <= self.max_rendered_field_bytes:
            return value, False
        truncated = encoded[: self.max_rendered_field_bytes].decode("utf-8", errors="ignore")
        return truncated, True


class CollectionExecutor:
    """A single worker thread that exclusively owns one Anki collection."""

    def __init__(
        self,
        path: str | Path,
        max_page_size: int,
        max_search_scan: int = 10_000,
        max_rendered_field_bytes: int = 262_144,
        max_card_fields: int = 100,
    ) -> None:
        self._path = path
        self._max_page_size = max_page_size
        self._max_search_scan = max_search_scan
        self._max_rendered_field_bytes = max_rendered_field_bytes
        self._max_card_fields = max_card_fields
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="anki-collection")
        self._adapter: CollectionAdapter | None = None
        self._worker_id: int | None = None

    @property
    def max_workers(self) -> int:
        """Expose the executor's actual configured concurrency for diagnostics and tests."""
        return int(self._pool._max_workers)  # pyright: ignore[reportPrivateUsage]

    def start(self) -> None:
        def open_collection() -> None:
            self._worker_id = threading.get_ident()
            self._adapter = CollectionAdapter(
                self._path,
                self._max_page_size,
                self._max_search_scan,
                self._max_rendered_field_bytes,
                self._max_card_fields,
            )

        self._pool.submit(open_collection).result()

    def submit(self, function: Callable[[CollectionAdapter], T]) -> Future[T]:
        def invoke() -> T:
            if self._adapter is None:
                raise RuntimeError("collection executor is not started")
            if threading.get_ident() != self._worker_id:
                raise RuntimeError("collection accessed outside owning thread")
            return function(self._adapter)

        return self._pool.submit(invoke)

    async def run(self, function: Callable[[CollectionAdapter], T]) -> T:
        return await asyncio.wrap_future(self.submit(function))

    async def close(self) -> None:
        if self._adapter is not None:
            await self.run(lambda adapter: adapter.close())
            self._adapter = None
        self._pool.shutdown(wait=True, cancel_futures=True)


class AnkiCollectionService:
    """Async facade over the dedicated collection executor."""

    def __init__(
        self,
        path: str | Path,
        max_page_size: int,
        max_search_scan: int = 10_000,
        max_rendered_field_bytes: int = 262_144,
        max_card_fields: int = 100,
    ) -> None:
        self.executor = CollectionExecutor(
            path, max_page_size, max_search_scan, max_rendered_field_bytes, max_card_fields
        )

    async def __aenter__(self) -> AnkiCollectionService:
        self.executor.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.executor.close()

    async def list_decks(self, offset: int, limit: int) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.list_decks(offset, limit))

    async def get_deck(self, deck_id: int) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.get_deck(deck_id))

    async def create_deck(self, name: str) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.create_deck(name))

    async def update_deck(self, deck_id: int, name: str) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.update_deck(deck_id, name))

    async def delete_deck(self, deck_id: int) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.delete_deck(deck_id))

    async def search_cards(self, query: str, offset: int, limit: int) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.search_cards(query, offset, limit))

    async def get_card(self, card_id: int) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.get_card(card_id))

    async def create_card(self, deck_id: int, front: str, back: str) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.create_card(deck_id, front, back))

    async def update_card(
        self, card_id: int, front: str | None, back: str | None, deck_id: int | None
    ) -> dict[str, Any]:
        return await self.executor.run(
            lambda adapter: adapter.update_card(card_id, front, back, deck_id)
        )

    async def delete_card(self, card_id: int) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.delete_card(card_id))

    async def sync_login(
        self, username: str, password: str, endpoint: str | None
    ) -> dict[str, Any]:
        return await self.executor.run(
            lambda adapter: adapter.sync_login(username, password, endpoint)
        )

    async def sync(self, sync_media: bool) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.sync(sync_media))

    async def full_sync(self, upload: bool) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.full_sync(upload))

    async def check_ready(self) -> bool:
        return await self.executor.run(lambda adapter: adapter.check_ready())
