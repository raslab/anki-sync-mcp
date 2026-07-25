from __future__ import annotations

import asyncio
import base64
import binascii
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar, cast

import anki.lang
from anki.collection import AddNoteRequest, Collection
from anki.errors import NetworkError, NotFoundError
from anki.sync import SyncAuth
from anki.utils import field_checksum

from anki_mcp.config import validate_sync_migration_endpoint
from anki_mcp.state import PersistentState

if TYPE_CHECKING:
    from anki.cards import CardId
    from anki.decks import DeckId
    from anki.models import NotetypeId
    from anki.notes import NoteId

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


class FullSyncRequiredError(RuntimeError):
    """Raised when a normal operation encounters a one-way sync requirement."""


class DuplicateNoteError(ValueError):
    """Raised when a note request would create a duplicate."""


class IdempotencyConflictError(ValueError):
    """Raised when an idempotency key is reused with different content."""


class CollectionAdapter:
    """Synchronous adapter; an instance must only be used by its owning thread."""

    def __init__(
        self,
        path: str | Path,
        max_page_size: int,
        max_search_scan: int = 10_000,
        max_rendered_field_bytes: int = 262_144,
        max_card_fields: int = 100,
        max_batch_size: int = 50,
        max_media_bytes: int = 1_048_576,
        sync_on_read: bool = False,
        sync_on_write: bool = False,
    ) -> None:
        if anki.lang.current_i18n is None:
            anki.lang.set_lang("en_US")
        self.collection = Collection(str(path))
        self.max_page_size = max_page_size
        self.max_search_scan = max_search_scan
        self.max_rendered_field_bytes = max_rendered_field_bytes
        self.max_card_fields = max_card_fields
        self.max_batch_size = max_batch_size
        self.max_media_bytes = max_media_bytes
        self.sync_on_read = sync_on_read
        self.sync_on_write = sync_on_write
        self._backup_folder = Path(path).parent / "backups"
        self._state = PersistentState(path)
        persisted_auth = self._state.load_sync_auth()
        self._sync_auth = (
            SyncAuth(
                hkey=str(persisted_auth["hkey"]),
                endpoint=str(persisted_auth.get("endpoint") or ""),
            )
            if persisted_auth
            else None
        )
        self._configured_sync_endpoint = (
            str(persisted_auth.get("configured_endpoint") or "") or None if persisted_auth else None
        )
        status = self._state.load_status()
        pending = status.get("pending_full_sync")
        self._pending_full_sync = (
            (int(pending["required"]), pending.get("server_media_usn"))
            if isinstance(pending, dict)
            else None
        )
        self._last_sync_at = status.get("last_sync_at")

    def close(self) -> None:
        try:
            self.collection.close()
        finally:
            self._state.close()

    def check_ready(self) -> bool:
        if self.collection.db is None:
            return False
        return self.collection.db.scalar("select 1") == 1

    def _save_operational_status(self) -> None:
        pending = None
        if self._pending_full_sync is not None:
            pending = {
                "required": self._pending_full_sync[0],
                "server_media_usn": self._pending_full_sync[1],
            }
        self._state.save_status({"last_sync_at": self._last_sync_at, "pending_full_sync": pending})

    def _invalidate_sync_auth(self) -> None:
        self._sync_auth = None
        self._configured_sync_endpoint = None
        self._pending_full_sync = None
        self._state.clear_sync_auth()
        self._save_operational_status()

    def status(self) -> dict[str, Any]:
        collection_ready = self.check_ready()
        pending_name = (
            SYNC_REQUIRED_NAMES[self._pending_full_sync[0]]
            if self._pending_full_sync is not None
            else None
        )
        authenticated = self._sync_auth is not None
        ready = collection_ready and authenticated and pending_name is None
        if not collection_ready:
            reason = "collection_unavailable"
        elif pending_name is not None:
            reason = "full_sync_required"
        elif not authenticated:
            reason = "authentication_required"
        else:
            reason = None
        return {
            "anki_version": version("anki"),
            "collection_ready": collection_ready,
            "authenticated": authenticated,
            "ready": ready,
            "readiness_reason": reason,
            "pending_full_sync": pending_name,
            "last_sync_at": self._last_sync_at,
            "pending_mutations": self._state.pending_receipt_count(),
        }

    def create_backup(self) -> dict[str, Any]:
        self._backup_folder.mkdir(parents=True, exist_ok=True)
        created = self.collection.create_backup(
            backup_folder=str(self._backup_folder), force=True, wait_for_completion=True
        )
        return {"requested": True, "created": bool(created)}

    def bootstrap(
        self,
        mode: str,
        username: str,
        password: str | None,
        endpoint: str | None,
    ) -> dict[str, Any]:
        """Perform an explicitly configured, download-only initialization of an empty client."""
        if mode == "disabled":
            return {"bootstrapped": False, "reason": "disabled"}
        if mode != "download_if_empty":
            raise ValueError("unsupported bootstrap mode")
        if self.collection.note_count() or self.collection.card_count():
            raise ValueError("download_if_empty bootstrap requires an empty local collection")
        if self._sync_auth is None:
            if not username.strip() or password is None:
                raise SyncLoginRequiredError(
                    "bootstrap requires sync credentials or persisted authentication"
                )
            self.sync_login(username, password, endpoint)
        sync_result = self.sync(sync_media=False)
        required = sync_result["required"]
        if required in {"FULL_SYNC", "FULL_DOWNLOAD"}:
            result = self.full_sync(upload=False)
            return {"bootstrapped": True, **result}
        if required == "FULL_UPLOAD":
            raise FullSyncRequiredError(
                "FULL_UPLOAD cannot be resolved by download_if_empty bootstrap"
            )
        return {"bootstrapped": True, "direction": None, "sync": sync_result}

    def coordinated_read(self, read: Callable[[CollectionAdapter], T], sync_before: bool) -> T:
        if sync_before or self.sync_on_read:
            self._sync_or_raise_full_sync(sync_media=False)
        return read(self)

    def coordinated_mutation(
        self,
        operation: str,
        idempotency_key: str,
        request: dict[str, Any],
        mutate: Callable[[CollectionAdapter], dict[str, Any]],
    ) -> dict[str, Any]:
        if not idempotency_key or len(idempotency_key.encode("utf-8")) > 128:
            raise ValueError("idempotency_key must contain 1 to 128 UTF-8 bytes")
        request_hash = self._state.request_hash(operation, request)
        existing = self._state.get_receipt(idempotency_key)
        if existing is not None:
            recorded_operation, recorded_hash, receipt = existing
            if recorded_operation != operation or recorded_hash != request_hash:
                raise IdempotencyConflictError(
                    "idempotency key was already used with different content"
                )
            if (
                receipt.get("local_committed")
                and not receipt.get("remote_synced")
                and receipt.get("retryable")
            ):
                try:
                    self._sync_or_raise_full_sync(sync_media=False)
                except FullSyncRequiredError:
                    receipt["retryable"] = False
                    self._state.put_receipt(idempotency_key, operation, request_hash, receipt)
                    raise
                receipt["remote_synced"] = True
                receipt["retryable"] = False
                self._state.put_receipt(idempotency_key, operation, request_hash, receipt)
            return receipt

        if self.sync_on_write:
            self._sync_or_raise_full_sync(sync_media=False)
        intent: dict[str, Any] = {
            "idempotency_key": idempotency_key,
            "state": "outcome_unknown",
            "local_committed": None,
            "remote_synced": False,
            "media_synced": None,
            "retryable": False,
            "result": None,
        }
        self._state.put_receipt(idempotency_key, operation, request_hash, intent)
        try:
            result = mutate(self)
        except (ValueError, LookupError):
            self._state.delete_receipt(idempotency_key)
            raise
        receipt: dict[str, Any] = {
            "idempotency_key": idempotency_key,
            "state": "committed",
            "local_committed": True,
            "remote_synced": not self.sync_on_write,
            "media_synced": None,
            "retryable": False,
            "result": result,
        }
        self._state.put_receipt(idempotency_key, operation, request_hash, receipt)
        if self.sync_on_write:
            try:
                self._sync_or_raise_full_sync(sync_media=False)
            except FullSyncRequiredError:
                receipt["remote_synced"] = False
                self._state.put_receipt(idempotency_key, operation, request_hash, receipt)
                raise
            except Exception:
                receipt["remote_synced"] = False
                receipt["retryable"] = True
                self._state.put_receipt(idempotency_key, operation, request_hash, receipt)
                return receipt
            receipt["remote_synced"] = True
            self._state.put_receipt(idempotency_key, operation, request_hash, receipt)
        return receipt

    def _sync_or_raise_full_sync(self, sync_media: bool) -> dict[str, Any]:
        result = self.sync(sync_media)
        if result["required"] in {"FULL_SYNC", "FULL_DOWNLOAD", "FULL_UPLOAD"}:
            raise FullSyncRequiredError(
                f"{result['required']} requires operator-controlled full synchronization"
            )
        return result

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
            return {"id": int(existing), "created": False}
        deck_id = self.collection.decks.id(name)
        if deck_id is None:  # pragma: no cover - creation returns an ID
            raise RuntimeError("Anki did not return a deck ID")
        return {"id": int(deck_id), "created": True}

    def update_deck(self, deck_id: int, name: str) -> dict[str, Any]:
        self.get_deck(deck_id)
        if not name.strip():
            raise ValueError("deck name must not be blank")
        self.collection.decks.rename(cast("DeckId", deck_id), name)
        return {"id": deck_id, "updated": True}

    def _raise_after_delete_failure(
        self,
        operation_error: Exception,
        resource_name: str,
        verify_present: Callable[[], object],
    ) -> None:
        try:
            verify_present()
        except LookupError:
            try:
                self.collection.undo()
                verify_present()
            except Exception:
                raise RuntimeError(
                    f"{resource_name} deletion failed and rollback was incomplete"
                ) from operation_error
        raise operation_error

    def delete_deck(self, deck_id: int) -> dict[str, Any]:
        self.get_deck(deck_id)
        if deck_id == 1:
            raise ValueError("the Default deck cannot be deleted")
        try:
            self.collection.decks.remove([cast("DeckId", deck_id)])
        except Exception as operation_error:
            self._raise_after_delete_failure(
                operation_error, "deck", lambda: self.get_deck(deck_id)
            )
        return {"id": deck_id, "deleted": True}

    def search_notes(self, query: str, offset: int, limit: int) -> dict[str, Any]:
        self._page([], offset, limit)
        if int(self.collection.note_count()) > self.max_search_scan:
            raise ValueError(
                "collection exceeds MCP_MAX_SEARCH_SCAN; use a larger configured bound"
            )
        note_ids = [int(note_id) for note_id in self.collection.find_notes(query)]
        result = self._page(note_ids, offset, limit)
        result["items"] = [self._note_summary(note_id) for note_id in result["items"]]
        return result

    def _note_summary(self, note_id: int) -> dict[str, Any]:
        try:
            note = self.collection.get_note(cast("NoteId", note_id))
        except NotFoundError as exc:
            raise LookupError(f"note {note_id} not found") from exc
        first_field, first_field_truncated = self._truncate_rendered(note.fields[0])
        return {
            "id": int(note.id),
            "note_type_id": int(note.mid),
            "first_field": first_field,
            "first_field_truncated": first_field_truncated,
            "tags": list(note.tags),
        }

    def get_note(self, note_id: int) -> dict[str, Any]:
        try:
            note = self.collection.get_note(cast("NoteId", note_id))
        except NotFoundError as exc:
            raise LookupError(f"note {note_id} not found") from exc
        model = self.collection.models.get(note.mid)
        if model is None:  # pragma: no cover - Anki preserves referenced note types
            raise LookupError(f"note type for note {note_id} not found")
        model_name, model_name_truncated = self._truncate_rendered(str(model["name"]))
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
            "id": int(note.id),
            "note_type_id": int(note.mid),
            "note_type_name": model_name,
            "note_type_name_truncated": model_name_truncated,
            "fields": fields,
            "fields_omitted": max(0, len(note_items) - self.max_card_fields),
            "tags": list(note.tags),
            "card_ids": [
                int(card_id) for card_id in self.collection.find_cards(f"nid:{int(note.id)}")
            ],
            "modified": int(note.mod),
        }

    def _prepare_note(
        self,
        deck_id: int,
        note_type_id: int,
        fields: dict[str, str],
        tags: list[str],
    ) -> Any:
        self.get_deck(deck_id)
        model = self.collection.models.get(cast("NotetypeId", note_type_id))
        if model is None:
            raise LookupError(f"note type {note_type_id} not found")
        expected_fields = self.collection.models.field_names(model)
        if set(fields) != set(expected_fields):
            raise ValueError(f"fields must exactly match note type fields: {expected_fields}")
        note = self.collection.new_note(model)
        for field_name in expected_fields:
            note[field_name] = fields[field_name]
        note.tags = self._normalize_tags(tags)
        check = int(note.duplicate_or_empty())
        if check == 1:
            raise ValueError("the note's first field must not be empty")
        if check == 2:
            raise DuplicateNoteError("a note with the same first field already exists")
        return note

    def _normalize_tags(self, tags: list[str]) -> list[str]:
        if any(not tag.strip() for tag in tags):
            raise ValueError("tags must not contain blank values")
        return list(dict.fromkeys(tags))

    def create_note(
        self,
        deck_id: int,
        note_type_id: int,
        fields: dict[str, str],
        tags: list[str],
    ) -> dict[str, Any]:
        note = self._prepare_note(deck_id, note_type_id, fields, tags)
        self.collection.add_note(note, cast("DeckId", deck_id))
        card_ids = [int(card_id) for card_id in self.collection.find_cards(f"nid:{int(note.id)}")]
        if not card_ids:
            self.collection.remove_notes([note.id])
            raise ValueError("note did not generate any cards")
        return {"note_id": int(note.id), "card_ids": card_ids, "created": True}

    def create_notes_batch(self, requests: list[dict[str, Any]]) -> dict[str, Any]:
        if not requests or len(requests) > self.max_batch_size:
            raise ValueError(f"batch must contain between 1 and {self.max_batch_size} notes")
        prepared: list[tuple[Any, int]] = []
        fingerprints: set[tuple[int, int]] = set()
        for request in requests:
            note = self._prepare_note(
                int(request["deck_id"]),
                int(request["note_type_id"]),
                dict(request["fields"]),
                list(request.get("tags", [])),
            )
            fingerprint = (int(note.mid), field_checksum(note.fields[0]))
            if fingerprint in fingerprints:
                raise DuplicateNoteError("batch contains duplicate first fields")
            fingerprints.add(fingerprint)
            prepared.append((note, int(request["deck_id"])))
        self.collection.add_notes(
            [
                AddNoteRequest(note=note, deck_id=cast("DeckId", deck_id))
                for note, deck_id in prepared
            ]
        )
        results = []
        for note, _ in prepared:
            card_ids = [
                int(card_id) for card_id in self.collection.find_cards(f"nid:{int(note.id)}")
            ]
            if not card_ids:  # pragma: no cover - validated normal note types generate cards
                raise RuntimeError("atomic note batch produced a note without cards")
            results.append({"note_id": int(note.id), "card_ids": card_ids, "created": True})
        return {"notes": results, "created": len(results)}

    def update_note_fields(self, note_id: int, fields: dict[str, str]) -> dict[str, Any]:
        if not fields:
            raise ValueError("at least one field update is required")
        try:
            note = self.collection.get_note(cast("NoteId", note_id))
        except NotFoundError as exc:
            raise LookupError(f"note {note_id} not found") from exc
        unknown = set(fields) - set(note.keys())
        if unknown:
            raise ValueError(f"unknown field: {', '.join(sorted(unknown))}")
        for name, value in fields.items():
            note[name] = value
        check = int(note.duplicate_or_empty())
        if check == 1:
            raise ValueError("the note's first field must not be empty")
        if check == 2:
            raise DuplicateNoteError("a note with the same first field already exists")
        self.collection.update_note(note)
        return {"note_id": note_id, "updated": True}

    def _update_note_tags(
        self, note_ids: list[int], tags: list[str], *, remove: bool
    ) -> dict[str, Any]:
        if not note_ids or len(note_ids) > self.max_batch_size:
            raise ValueError(
                f"note_ids must contain between 1 and {self.max_batch_size} stable IDs"
            )
        normalized = self._normalize_tags(tags)
        if not normalized:
            raise ValueError("tags must not be empty")
        notes = []
        for note_id in note_ids:
            try:
                note = self.collection.get_note(cast("NoteId", note_id))
            except NotFoundError as exc:
                raise LookupError(f"note {note_id} not found") from exc
            if remove:
                remove_names = {tag.casefold() for tag in normalized}
                note.tags = [tag for tag in note.tags if tag.casefold() not in remove_names]
            else:
                note.tags = list(dict.fromkeys([*note.tags, *normalized]))
            notes.append(note)
        self.collection.update_notes(notes)
        return {"updated_note_ids": note_ids}

    def add_note_tags(self, note_ids: list[int], tags: list[str]) -> dict[str, Any]:
        return self._update_note_tags(note_ids, tags, remove=False)

    def remove_note_tags(self, note_ids: list[int], tags: list[str]) -> dict[str, Any]:
        return self._update_note_tags(note_ids, tags, remove=True)

    def delete_notes(self, note_ids: list[int]) -> dict[str, Any]:
        if not note_ids or len(note_ids) > self.max_batch_size:
            raise ValueError(
                f"note_ids must contain between 1 and {self.max_batch_size} stable IDs"
            )
        if len(set(note_ids)) != len(note_ids):
            raise ValueError("note_ids must not contain duplicates")
        for note_id in note_ids:
            self.get_note(note_id)
        self.collection.remove_notes([cast("NoteId", note_id) for note_id in note_ids])
        return {"note_ids": note_ids, "deleted": len(note_ids)}

    def list_tags(self, offset: int, limit: int) -> dict[str, Any]:
        tags = self.collection.tags.all()
        if len(tags) > self.max_search_scan:
            raise ValueError(
                "collection exceeds MCP_MAX_SEARCH_SCAN; use a larger configured bound"
            )
        items = []
        for tag in sorted(tags, key=str.casefold):
            name, name_truncated = self._truncate_rendered(tag)
            items.append({"name": name, "name_truncated": name_truncated})
        return self._page(items, offset, limit)

    def rename_tag(self, old_name: str, new_name: str) -> dict[str, Any]:
        if not old_name.strip() or not new_name.strip():
            raise ValueError("tag names must not be blank")
        if old_name not in self.collection.tags.all():
            raise LookupError(f"tag {old_name} not found")
        if int(self.collection.note_count()) > self.max_search_scan:
            raise ValueError(
                "collection exceeds MCP_MAX_SEARCH_SCAN; use a larger configured bound"
            )
        result = self.collection.tags.rename(old_name, new_name)
        return {
            "old_name": old_name,
            "new_name": new_name,
            "updated_notes": int(result.count),
        }

    def delete_tag(self, name: str) -> dict[str, Any]:
        if not name.strip():
            raise ValueError("tag name must not be blank")
        if name not in self.collection.tags.all():
            raise LookupError(f"tag {name} not found")
        if int(self.collection.note_count()) > self.max_search_scan:
            raise ValueError(
                "collection exceeds MCP_MAX_SEARCH_SCAN; use a larger configured bound"
            )
        result = self.collection.tags.remove(name)
        return {"name": name, "updated_notes": int(result.count), "deleted": True}

    def list_note_types(self, offset: int, limit: int) -> dict[str, Any]:
        models = self.collection.models.all_names_and_ids()
        if len(models) > self.max_search_scan:
            raise ValueError(
                "collection exceeds MCP_MAX_SEARCH_SCAN; use a larger configured bound"
            )
        items = []
        for item in sorted(models, key=lambda model: model.name.casefold()):
            name, name_truncated = self._truncate_rendered(item.name)
            model = self.collection.models.get(cast("NotetypeId", item.id))
            items.append(
                {
                    "id": int(item.id),
                    "name": name,
                    "name_truncated": name_truncated,
                    "note_count": self.collection.models.use_count(model) if model else 0,
                }
            )
        return self._page(items, offset, limit)

    def get_note_type(self, note_type_id: int) -> dict[str, Any]:
        model = self.collection.models.get(cast("NotetypeId", note_type_id))
        if model is None:
            raise LookupError(f"note type {note_type_id} not found")
        name, name_truncated = self._truncate_rendered(str(model["name"]))
        css, css_truncated = self._truncate_rendered(str(model.get("css", "")))
        fields = []
        for field in model.get("flds", [])[: self.max_card_fields]:
            field_name, field_name_truncated = self._truncate_rendered(str(field["name"]))
            fields.append(
                {
                    "name": field_name,
                    "name_truncated": field_name_truncated,
                    "ordinal": int(field["ord"]),
                }
            )
        templates = []
        for template in model.get("tmpls", [])[: self.max_card_fields]:
            template_name, template_name_truncated = self._truncate_rendered(str(template["name"]))
            question, question_truncated = self._truncate_rendered(str(template.get("qfmt", "")))
            answer, answer_truncated = self._truncate_rendered(str(template.get("afmt", "")))
            templates.append(
                {
                    "name": template_name,
                    "name_truncated": template_name_truncated,
                    "ordinal": int(template["ord"]),
                    "question_format": question,
                    "question_format_truncated": question_truncated,
                    "answer_format": answer,
                    "answer_format_truncated": answer_truncated,
                }
            )
        return {
            "id": int(model["id"]),
            "name": name,
            "name_truncated": name_truncated,
            "kind": "cloze" if int(model.get("type", 0)) == 1 else "standard",
            "css": css,
            "css_truncated": css_truncated,
            "fields": fields,
            "fields_omitted": max(0, len(model.get("flds", [])) - self.max_card_fields),
            "templates": templates,
            "templates_omitted": max(0, len(model.get("tmpls", [])) - self.max_card_fields),
            "note_count": self.collection.models.use_count(model),
        }

    def _validate_note_type_parts(self, fields: list[str], templates: list[dict[str, str]]) -> None:
        if not fields or len(fields) > self.max_card_fields:
            raise ValueError(f"fields must contain between 1 and {self.max_card_fields} names")
        if any(not name.strip() for name in fields):
            raise ValueError("field names must not be blank")
        if len({name.casefold() for name in fields}) != len(fields):
            raise ValueError("field names must be unique")
        if not templates or len(templates) > self.max_card_fields:
            raise ValueError(f"templates must contain between 1 and {self.max_card_fields} items")
        names = [template["name"] for template in templates]
        if any(not name.strip() for name in names):
            raise ValueError("template names must not be blank")
        if len({name.casefold() for name in names}) != len(names):
            raise ValueError("template names must be unique")

    def create_note_type(
        self,
        name: str,
        fields: list[str],
        templates: list[dict[str, str]],
        css: str,
    ) -> dict[str, Any]:
        if not name.strip():
            raise ValueError("note type name must not be blank")
        if self.collection.models.by_name(name) is not None:
            raise ValueError(f"note type {name} already exists")
        self._validate_note_type_parts(fields, templates)
        model = self.collection.models.new(name)
        for field_name in fields:
            self.collection.models.add_field(model, self.collection.models.new_field(field_name))
        for template_input in templates:
            template = self.collection.models.new_template(template_input["name"])
            template["qfmt"] = template_input["question_format"]
            template["afmt"] = template_input["answer_format"]
            self.collection.models.add_template(model, template)
        model["css"] = css
        self.collection.models.add(model)
        return {"id": int(model["id"]), "created": True}

    def update_note_type(
        self,
        note_type_id: int,
        name: str,
        fields: list[str],
        templates: list[dict[str, str]],
        css: str,
    ) -> dict[str, Any]:
        model = self.collection.models.get(cast("NotetypeId", note_type_id))
        if model is None:
            raise LookupError(f"note type {note_type_id} not found")
        if not name.strip():
            raise ValueError("note type name must not be blank")
        existing = self.collection.models.by_name(name)
        if existing is not None and int(existing["id"]) != note_type_id:
            raise ValueError(f"note type {name} already exists")
        self._validate_note_type_parts(fields, templates)
        current_fields = model.get("flds", [])
        current_templates = model.get("tmpls", [])
        if len(fields) != len(current_fields) or len(templates) != len(current_templates):
            raise ValueError("note type update must preserve field and template counts")
        model["name"] = name
        model["css"] = css
        for field, field_name in zip(current_fields, fields, strict=True):
            field["name"] = field_name
        for template, template_input in zip(current_templates, templates, strict=True):
            template["name"] = template_input["name"]
            template["qfmt"] = template_input["question_format"]
            template["afmt"] = template_input["answer_format"]
        self.collection.models.update_dict(model)
        return {"id": note_type_id, "updated": True}

    def delete_note_type(self, note_type_id: int) -> dict[str, Any]:
        model = self.collection.models.get(cast("NotetypeId", note_type_id))
        if model is None:
            raise LookupError(f"note type {note_type_id} not found")
        note_count = int(self.collection.models.use_count(model))
        self.collection.models.remove(cast("NotetypeId", note_type_id))
        return {"id": note_type_id, "deleted": True, "deleted_notes": note_count}

    def _media_path(self, filename: str, *, require_exists: bool) -> Path:
        if (
            "\x00" in filename
            or not filename.strip()
            or Path(filename).name != filename
            or any(separator in filename for separator in ("/", "\\"))
        ):
            raise ValueError("media filename must be a plain filename without path separators")
        path = Path(self.collection.media.dir()) / filename
        if require_exists and (not path.is_file() or path.is_symlink()):
            raise LookupError(f"media {filename} not found")
        return path

    def list_media(self, offset: int, limit: int) -> dict[str, Any]:
        items = []
        for path in Path(self.collection.media.dir()).iterdir():
            if path.is_file() and not path.is_symlink():
                items.append({"filename": path.name, "size_bytes": path.stat().st_size})
                if len(items) > self.max_search_scan:
                    raise ValueError(
                        "collection exceeds MCP_MAX_SEARCH_SCAN; use a larger configured bound"
                    )
        sorted_items = sorted(items, key=lambda item: item["filename"].casefold())
        return self._page(sorted_items, offset, limit)

    def get_media(self, filename: str) -> dict[str, Any]:
        path = self._media_path(filename, require_exists=True)
        size = path.stat().st_size
        if size > self.max_media_bytes:
            raise ValueError(f"media file exceeds ANKI_MAX_MEDIA_BYTES ({self.max_media_bytes})")
        return {
            "filename": filename,
            "size_bytes": size,
            "content_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
        }

    def store_media(self, filename: str, content_base64: str) -> dict[str, Any]:
        self._media_path(filename, require_exists=False)
        try:
            content = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("content_base64 must be valid base64") from exc
        if len(content) > self.max_media_bytes:
            raise ValueError(f"media content exceeds ANKI_MAX_MEDIA_BYTES ({self.max_media_bytes})")
        existed = self.collection.media.have(filename)
        stored_name = self.collection.media.write_data(filename, content)
        return {"filename": stored_name, "size_bytes": len(content), "created": not existed}

    def rename_media(self, old_filename: str, new_filename: str) -> dict[str, Any]:
        old_path = self._media_path(old_filename, require_exists=True)
        self._media_path(new_filename, require_exists=False)
        if self.collection.media.have(new_filename):
            raise ValueError(f"media {new_filename} already exists")
        content = old_path.read_bytes()
        if len(content) > self.max_media_bytes:
            raise ValueError(f"media file exceeds ANKI_MAX_MEDIA_BYTES ({self.max_media_bytes})")
        stored_name = self.collection.media.write_data(new_filename, content)
        try:
            self.collection.media.trash_files([old_filename])
        except Exception:
            self.collection.media.trash_files([stored_name])
            raise
        return {"old_filename": old_filename, "filename": stored_name, "updated": True}

    def delete_media(self, filename: str) -> dict[str, Any]:
        self._media_path(filename, require_exists=True)
        self.collection.media.trash_files([filename])
        return {"filename": filename, "deleted": True}

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
            raise ValueError("at least one card update must be provided")
        if front is not None and not front.strip():
            raise ValueError("front must not be blank")
        original_deck_id = int(card.did)
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
            try:
                self.collection.update_note(note)
                if len(self.collection.find_cards(f"nid:{int(note.id)}")) != 1:
                    raise ValueError("Basic note update must retain exactly one card")
            except Exception as operation_error:
                note["Front"] = previous_front
                note["Back"] = previous_back
                try:
                    self.collection.update_note(note)
                except Exception:
                    raise RuntimeError(
                        "card update failed and field rollback was incomplete"
                    ) from operation_error
                raise operation_error
        try:
            if deck_id is not None:
                self.collection.set_deck([cast("CardId", card_id)], deck_id)
        except Exception as operation_error:
            rollback_failed = False
            try:
                self.collection.set_deck([cast("CardId", card_id)], original_deck_id)
            except Exception:
                rollback_failed = True
            if note_changed:
                note["Front"] = previous_front
                note["Back"] = previous_back
                try:
                    self.collection.update_note(note)
                except Exception:
                    rollback_failed = True
            if rollback_failed:
                raise RuntimeError(
                    "card update failed and rollback was incomplete"
                ) from operation_error
            raise operation_error
        return {
            "id": card_id,
            "note_id": int(note.id),
            "deck_id": deck_id if deck_id is not None else int(card.did),
            "updated": True,
        }

    def delete_card(self, card_id: int) -> dict[str, Any]:
        self.get_card(card_id)
        try:
            self.collection.remove_cards_and_orphaned_notes([cast("CardId", card_id)])
        except Exception as operation_error:
            self._raise_after_delete_failure(
                operation_error, "card", lambda: self.get_card(card_id)
            )
        return {"id": card_id, "deleted": True}

    def _validate_card_ids(self, card_ids: list[int]) -> None:
        if not card_ids or len(card_ids) > self.max_batch_size:
            raise ValueError(
                f"card_ids must contain between 1 and {self.max_batch_size} stable IDs"
            )
        for card_id in card_ids:
            self.get_card(card_id)

    def change_card_deck(self, card_ids: list[int], deck_id: int) -> dict[str, Any]:
        self._validate_card_ids(card_ids)
        self.get_deck(deck_id)
        self.collection.set_deck([cast("CardId", card_id) for card_id in card_ids], deck_id)
        return {"card_ids": card_ids, "deck_id": deck_id, "updated": True}

    def suspend_cards(self, card_ids: list[int]) -> dict[str, Any]:
        self._validate_card_ids(card_ids)
        self.collection.sched.suspend_cards([cast("CardId", card_id) for card_id in card_ids])
        return {"card_ids": card_ids, "suspended": True}

    def unsuspend_cards(self, card_ids: list[int]) -> dict[str, Any]:
        self._validate_card_ids(card_ids)
        self.collection.sched.unsuspend_cards([cast("CardId", card_id) for card_id in card_ids])
        return {"card_ids": card_ids, "suspended": False}

    def sync_login(self, username: str, password: str, endpoint: str | None) -> dict[str, Any]:
        self._sync_auth = None
        self._configured_sync_endpoint = None
        self._pending_full_sync = None
        self._state.clear_sync_auth()
        self._save_operational_status()
        self._sync_auth = self.collection.sync_login(username, password, endpoint)
        self._configured_sync_endpoint = endpoint or None
        self._state.save_sync_auth(
            {
                "hkey": self._sync_auth.hkey,
                "endpoint": self._sync_auth.endpoint or "",
                "configured_endpoint": self._configured_sync_endpoint or "",
            }
        )
        return {"authenticated": True, "endpoint_kind": "custom" if endpoint else "ankiweb"}

    def sync(self, sync_media: bool) -> dict[str, Any]:
        if self._sync_auth is None:
            raise SyncLoginRequiredError("sync login is required before synchronization")
        try:
            output = self.collection.sync_collection(self._sync_auth, sync_media)
            endpoint_changed = bool(output.new_endpoint)
            if endpoint_changed:
                self._sync_auth.endpoint = validate_sync_migration_endpoint(
                    output.new_endpoint, self._configured_sync_endpoint
                )
            if output.required in {2, 3, 4}:
                self._pending_full_sync = (
                    output.required,
                    output.server_media_usn if sync_media else None,
                )
            else:
                self._pending_full_sync = None
            self._last_sync_at = datetime.now(UTC).isoformat()
            self._state.save_sync_auth(
                {
                    "hkey": self._sync_auth.hkey,
                    "endpoint": self._sync_auth.endpoint or "",
                    "configured_endpoint": self._configured_sync_endpoint or "",
                }
            )
            self._save_operational_status()
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
        except Exception as exc:
            if not isinstance(exc, NetworkError):
                self._invalidate_sync_auth()
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
        except Exception as exc:
            if not isinstance(exc, NetworkError):
                self._invalidate_sync_auth()
            raise
        self._pending_full_sync = None
        self._last_sync_at = datetime.now(UTC).isoformat()
        if upload:
            self._state.mark_all_remote_synced()
        else:
            self._state.mark_pending_discarded_by_full_download()
        self._save_operational_status()
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
        max_batch_size: int = 50,
        max_media_bytes: int = 1_048_576,
        sync_on_read: bool = False,
        sync_on_write: bool = False,
    ) -> None:
        self._path = path
        self._max_page_size = max_page_size
        self._max_search_scan = max_search_scan
        self._max_rendered_field_bytes = max_rendered_field_bytes
        self._max_card_fields = max_card_fields
        self._max_batch_size = max_batch_size
        self._max_media_bytes = max_media_bytes
        self._sync_on_read = sync_on_read
        self._sync_on_write = sync_on_write
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
                self._max_batch_size,
                self._max_media_bytes,
                self._sync_on_read,
                self._sync_on_write,
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
        max_batch_size: int = 50,
        max_media_bytes: int = 1_048_576,
        sync_on_read: bool = False,
        sync_on_write: bool = False,
    ) -> None:
        self.executor = CollectionExecutor(
            path,
            max_page_size,
            max_search_scan,
            max_rendered_field_bytes,
            max_card_fields,
            max_batch_size,
            max_media_bytes,
            sync_on_read,
            sync_on_write,
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

    async def search_notes(self, query: str, offset: int, limit: int) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.search_notes(query, offset, limit))

    async def get_note(self, note_id: int) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.get_note(note_id))

    async def create_note(
        self,
        deck_id: int,
        note_type_id: int,
        fields: dict[str, str],
        tags: list[str],
    ) -> dict[str, Any]:
        return await self.executor.run(
            lambda adapter: adapter.create_note(deck_id, note_type_id, fields, tags)
        )

    async def create_notes_batch(self, requests: list[dict[str, Any]]) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.create_notes_batch(requests))

    async def update_note_fields(self, note_id: int, fields: dict[str, str]) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.update_note_fields(note_id, fields))

    async def add_note_tags(self, note_ids: list[int], tags: list[str]) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.add_note_tags(note_ids, tags))

    async def remove_note_tags(self, note_ids: list[int], tags: list[str]) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.remove_note_tags(note_ids, tags))

    async def delete_notes(self, note_ids: list[int]) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.delete_notes(note_ids))

    async def list_tags(self, offset: int, limit: int) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.list_tags(offset, limit))

    async def rename_tag(self, old_name: str, new_name: str) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.rename_tag(old_name, new_name))

    async def delete_tag(self, name: str) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.delete_tag(name))

    async def list_note_types(self, offset: int, limit: int) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.list_note_types(offset, limit))

    async def get_note_type(self, note_type_id: int) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.get_note_type(note_type_id))

    async def create_note_type(
        self,
        name: str,
        fields: list[str],
        templates: list[dict[str, str]],
        css: str,
    ) -> dict[str, Any]:
        return await self.executor.run(
            lambda adapter: adapter.create_note_type(name, fields, templates, css)
        )

    async def update_note_type(
        self,
        note_type_id: int,
        name: str,
        fields: list[str],
        templates: list[dict[str, str]],
        css: str,
    ) -> dict[str, Any]:
        return await self.executor.run(
            lambda adapter: adapter.update_note_type(note_type_id, name, fields, templates, css)
        )

    async def delete_note_type(self, note_type_id: int) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.delete_note_type(note_type_id))

    async def list_media(self, offset: int, limit: int) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.list_media(offset, limit))

    async def get_media(self, filename: str) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.get_media(filename))

    async def store_media(self, filename: str, content_base64: str) -> dict[str, Any]:
        return await self.executor.run(
            lambda adapter: adapter.store_media(filename, content_base64)
        )

    async def rename_media(self, old_filename: str, new_filename: str) -> dict[str, Any]:
        return await self.executor.run(
            lambda adapter: adapter.rename_media(old_filename, new_filename)
        )

    async def delete_media(self, filename: str) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.delete_media(filename))

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

    async def change_card_deck(self, card_ids: list[int], deck_id: int) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.change_card_deck(card_ids, deck_id))

    async def suspend_cards(self, card_ids: list[int]) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.suspend_cards(card_ids))

    async def unsuspend_cards(self, card_ids: list[int]) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.unsuspend_cards(card_ids))

    async def coordinated_read(
        self, read: Callable[[CollectionAdapter], T], sync_before: bool = False
    ) -> T:
        return await self.executor.run(lambda adapter: adapter.coordinated_read(read, sync_before))

    async def coordinated_mutation(
        self,
        operation: str,
        idempotency_key: str,
        request: dict[str, Any],
        mutate: Callable[[CollectionAdapter], dict[str, Any]],
    ) -> dict[str, Any]:
        return await self.executor.run(
            lambda adapter: adapter.coordinated_mutation(
                operation, idempotency_key, request, mutate
            )
        )

    async def status(self) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.status())

    async def create_backup(self) -> dict[str, Any]:
        return await self.executor.run(lambda adapter: adapter.create_backup())

    async def bootstrap(
        self,
        mode: str,
        username: str,
        password: str | None,
        endpoint: str | None,
    ) -> dict[str, Any]:
        return await self.executor.run(
            lambda adapter: adapter.bootstrap(mode, username, password, endpoint)
        )

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
