from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class _Confirmation:
    operation: str
    request_hash: str
    expires_at: float


class ConfirmationRegistry:
    """Issue short-lived, one-time tokens bound to an exact guarded request."""

    def __init__(
        self,
        ttl_seconds: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._tokens: dict[str, _Confirmation] = {}

    @staticmethod
    def _request_hash(operation: str, request: dict[str, Any]) -> str:
        encoded = json.dumps(
            {"operation": operation, "request": request},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _purge_expired(self, now: float) -> None:
        self._tokens = {
            token: confirmation
            for token, confirmation in self._tokens.items()
            if confirmation.expires_at > now
        }

    def issue(self, operation: str, request: dict[str, Any]) -> str:
        now = self._clock()
        self._purge_expired(now)
        token = secrets.token_urlsafe(32)
        self._tokens[token] = _Confirmation(
            operation=operation,
            request_hash=self._request_hash(operation, request),
            expires_at=now + self.ttl_seconds,
        )
        return token

    def consume(self, token: str, operation: str, request: dict[str, Any]) -> None:
        now = self._clock()
        confirmation = self._tokens.pop(token, None)
        if confirmation is None:
            raise ValueError("confirmation token is invalid or already used")
        if confirmation.expires_at <= now:
            raise ValueError("confirmation token expired")
        if confirmation.operation != operation or confirmation.request_hash != self._request_hash(
            operation, request
        ):
            self._tokens[token] = confirmation
            raise ValueError("confirmation token does not match this request")
