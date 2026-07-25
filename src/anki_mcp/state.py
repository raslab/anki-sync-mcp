from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any


class PersistentState:
    """Small durable state store owned by the collection worker thread."""

    def __init__(self, collection_path: str | Path) -> None:
        self.directory = Path(collection_path).parent / "state"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.sync_auth_path = self.directory / "sync-auth"
        self.status_path = self.directory / "operation-status.json"
        self.connection = sqlite3.connect(self.directory / "idempotency.sqlite")
        self.connection.execute(
            """
            create table if not exists mutation_receipts (
                idempotency_key text primary key,
                operation text not null,
                request_hash text not null,
                receipt_json text not null
            )
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def request_hash(operation: str, request: dict[str, Any]) -> str:
        canonical = json.dumps(
            {"operation": operation, "request": request},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def get_receipt(self, key: str) -> tuple[str, str, dict[str, Any]] | None:
        row = self.connection.execute(
            "select operation, request_hash, receipt_json from mutation_receipts "
            "where idempotency_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        operation, request_hash, receipt_json = row
        receipt = json.loads(receipt_json)
        if not isinstance(receipt, dict):  # pragma: no cover - only this class writes records
            raise RuntimeError("invalid mutation receipt")
        return str(operation), str(request_hash), receipt

    def put_receipt(
        self, key: str, operation: str, request_hash: str, receipt: dict[str, Any]
    ) -> None:
        encoded = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.connection.execute(
            """
            insert into mutation_receipts(idempotency_key, operation, request_hash, receipt_json)
            values (?, ?, ?, ?)
            on conflict(idempotency_key) do update set receipt_json = excluded.receipt_json
            """,
            (key, operation, request_hash, encoded),
        )
        self.connection.commit()

    def pending_receipt_count(self) -> int:
        rows = self.connection.execute("select receipt_json from mutation_receipts").fetchall()
        return sum(not bool(json.loads(row[0]).get("remote_synced")) for row in rows)

    def mark_all_remote_synced(self) -> None:
        """Reconcile locally committed receipts after an operator-selected full upload."""
        rows = self.connection.execute(
            "select idempotency_key, receipt_json from mutation_receipts"
        ).fetchall()
        for key, receipt_json in rows:
            receipt = json.loads(receipt_json)
            if receipt.get("local_committed") and not receipt.get("remote_synced"):
                receipt["remote_synced"] = True
                receipt["retryable"] = False
                encoded = json.dumps(
                    receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                self.connection.execute(
                    "update mutation_receipts set receipt_json = ? where idempotency_key = ?",
                    (encoded, key),
                )
        self.connection.commit()

    def load_sync_auth(self) -> dict[str, Any] | None:
        return self._load_json(self.sync_auth_path)

    def save_sync_auth(self, auth: dict[str, Any]) -> None:
        self._atomic_json(self.sync_auth_path, auth, secret=True)

    def clear_sync_auth(self) -> None:
        self.sync_auth_path.unlink(missing_ok=True)

    def load_status(self) -> dict[str, Any]:
        return self._load_json(self.status_path) or {}

    def save_status(self, status: dict[str, Any]) -> None:
        self._atomic_json(self.status_path, status, secret=False)

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"unable to load persistent state file {path.name}") from exc
        if not isinstance(value, dict):
            raise RuntimeError(f"invalid persistent state file {path.name}")
        return value

    @staticmethod
    def _atomic_json(path: Path, value: dict[str, Any], *, secret: bool) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        descriptor = os.open(temporary, flags, 0o600 if secret else 0o644)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            if secret:
                path.chmod(0o600)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
