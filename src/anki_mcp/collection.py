from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar, cast

from anki.collection import Collection
from anki.errors import NotFoundError

if TYPE_CHECKING:
    from anki.cards import CardId
    from anki.decks import DeckId

T = TypeVar("T")


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

    async def search_cards(self, query: str, offset: int, limit: int) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.search_cards(query, offset, limit))

    async def get_card(self, card_id: int) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.get_card(card_id))

    async def check_ready(self) -> bool:
        return await self.executor.run(lambda adapter: adapter.check_ready())
