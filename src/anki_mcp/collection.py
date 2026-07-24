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
    ) -> None:
        self.collection = Collection(str(path))
        self.max_page_size = max_page_size
        self.max_search_scan = max_search_scan
        self.max_rendered_field_bytes = max_rendered_field_bytes
        self._sync_auth: SyncAuth | None = None

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
            decks.append(
                {
                    "id": int(item.id),
                    "name": item.name,
                    "hierarchy": item.name.split("::"),
                }
            )
        return self._page(decks, offset, limit)

    def get_deck(self, deck_id: int) -> dict[str, Any]:
        deck = self.collection.decks.get(cast("DeckId", deck_id), default=False)
        if deck is None:
            raise LookupError(f"deck {deck_id} not found")
        name = str(deck["name"])
        result: dict[str, Any] = {
            "id": int(deck["id"]),
            "name": name,
            "hierarchy": name.split("::"),
            "modified": int(deck.get("mod", 0)),
            "description": str(deck.get("desc", "")),
            "dynamic": bool(deck.get("dyn", 0)),
        }
        if not result["dynamic"]:
            result["config_id"] = int(deck.get("conf", 1))
            config = self.collection.decks.config_dict_for_deck_id(cast("DeckId", deck_id))
            result["config"] = {
                "name": str(config.get("name", "")),
                "new_cards_per_day": int(config.get("new", {}).get("perDay", 0)),
                "reviews_per_day": int(config.get("rev", {}).get("perDay", 0)),
                "max_answer_seconds": int(config.get("maxTaken", 0)),
                "desired_retention": float(config.get("desiredRetention", 0.9)),
            }
        return result

    def create_deck(self, name: str) -> dict[str, Any]:
        if not name.strip():
            raise ValueError("deck name must not be blank")
        deck_id = self.collection.decks.id(name)
        if deck_id is None:  # pragma: no cover - creation returns an ID
            raise RuntimeError("Anki did not return a deck ID")
        return self.get_deck(int(deck_id))

    def update_deck(self, deck_id: int, name: str) -> dict[str, Any]:
        self.get_deck(deck_id)
        if not name.strip():
            raise ValueError("deck name must not be blank")
        self.collection.decks.rename(cast("DeckId", deck_id), name)
        return self.get_deck(deck_id)

    def delete_deck(self, deck_id: int) -> dict[str, Any]:
        self.get_deck(deck_id)
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
        note = card.note()
        fields: dict[str, str] = {}
        fields_truncated: dict[str, bool] = {}
        for name, value in note.items():
            fields[name], fields_truncated[name] = self._truncate_rendered(value)
        return {
            "id": int(card.id),
            "note_id": int(card.nid),
            "deck_id": int(card.did),
            "deck_name": str(deck["name"]),
            "template_ordinal": int(card.ord),
            "flags": int(card.flags),
            "modified": int(card.mod),
            "question": question,
            "question_truncated": question_truncated,
            "answer": answer,
            "answer_truncated": answer_truncated,
            "fields": fields,
            "fields_truncated": fields_truncated,
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

    def create_card(self, deck_id: int, front: str, back: str) -> dict[str, Any]:
        self.get_deck(deck_id)
        if not front.strip():
            raise ValueError("front must not be blank")
        model = self.collection.models.by_name("Basic")
        if model is None:
            raise RuntimeError("Basic note type is unavailable")
        note = self.collection.new_note(model)
        note["Front"] = front
        note["Back"] = back
        self.collection.add_note(note, cast("DeckId", deck_id))
        card_ids = self.collection.find_cards(f"nid:{int(note.id)}")
        if not card_ids:  # pragma: no cover - Basic always generates one card
            raise RuntimeError("note did not generate a card")
        return self.get_card(int(card_ids[0]))

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
        note = card.note() if front is not None or back is not None else None
        if note is not None and not {"Front", "Back"}.issubset(note.keys()):
            raise ValueError("card field updates require the Basic note type")
        if note is not None:
            if front is not None:
                note["Front"] = front
            if back is not None:
                note["Back"] = back
            self.collection.update_note(note)
        if deck_id is not None:
            self.collection.set_deck([cast("CardId", card_id)], deck_id)
        return self.get_card(card_id)

    def delete_card(self, card_id: int) -> dict[str, Any]:
        self.get_card(card_id)
        self.collection.remove_cards_and_orphaned_notes([cast("CardId", card_id)])
        return {"id": card_id, "deleted": True}

    def sync_login(self, username: str, password: str, endpoint: str | None) -> dict[str, Any]:
        self._sync_auth = self.collection.sync_login(username, password, endpoint)
        return {"authenticated": True, "endpoint": endpoint or self._sync_auth.endpoint}

    def sync(self, sync_media: bool) -> dict[str, Any]:
        if self._sync_auth is None:
            raise SyncLoginRequiredError("sync login is required before synchronization")
        output = self.collection.sync_collection(self._sync_auth, sync_media)
        required = SYNC_REQUIRED_NAMES[output.required]
        return {
            "required": required,
            "server_message": output.server_message,
            "host_number": output.host_number,
            "media_sync_requested": sync_media,
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
    ) -> None:
        self._path = path
        self._max_page_size = max_page_size
        self._max_search_scan = max_search_scan
        self._max_rendered_field_bytes = max_rendered_field_bytes
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
    ) -> None:
        self.executor = CollectionExecutor(
            path, max_page_size, max_search_scan, max_rendered_field_bytes
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

    async def check_ready(self) -> bool:
        return await self.executor.run(lambda adapter: adapter.check_ready())
