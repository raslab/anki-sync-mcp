from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, NoReturn, TypeVar
from uuid import uuid4

from anki.errors import NetworkError, SyncError, SyncErrorKind
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field, StrictBool, StrictInt, StrictStr
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp

from anki_mcp.auth import BearerAuthMiddleware, RequestBodyLimitMiddleware
from anki_mcp.collection import AnkiCollectionService, SyncLoginRequiredError
from anki_mcp.config import Settings

T = TypeVar("T")
Offset = StrictInt
PageLimit = StrictInt
StableId = Annotated[StrictInt, Field(gt=0)]
Confirmation = StrictBool
SyncMedia = StrictBool
DeckName = Annotated[StrictStr, Field(min_length=1, max_length=512)]
SearchQuery = Annotated[StrictStr, Field(max_length=4096)]
CardText = Annotated[StrictStr, Field(max_length=262_144)]


class ResponseTooLargeError(RuntimeError):
    """Raised when a tool result exceeds the configured serialized response budget."""


def create_app(settings: Settings) -> ASGIApp:
    """Create the complete ASGI application and MCP registry."""

    service = AnkiCollectionService(
        settings.collection_path,
        settings.max_page_size,
        settings.max_search_scan,
        settings.max_rendered_field_bytes,
        settings.max_card_fields,
    )
    mcp = FastMCP(
        "anki-mcp",
        instructions="Authenticated sync and deck/card CRUD for one Anki collection.",
        streamable_http_path=settings.mcp_path,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.allowed_hosts,
            allowed_origins=settings.allowed_origins,
        ),
    )

    def raise_tool_error(code: str, message: str, cause: Exception) -> NoReturn:
        payload = {
            "code": code,
            "message": message,
            "correlation_id": str(uuid4()),
        }
        raise ToolError(json.dumps(payload, separators=(",", ":"))) from cause

    async def execute(
        operation: Awaitable[T], receipt: Callable[[T], dict[str, Any]] | None = None
    ) -> T | dict[str, Any]:
        try:
            raw_result = await operation
            result = receipt(raw_result) if receipt is not None else raw_result
            encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(encoded) > settings.max_response_bytes:
                raise ResponseTooLargeError("tool response exceeds MCP_MAX_RESPONSE_BYTES")
            return result
        except LookupError as exc:
            raise_tool_error("NOT_FOUND", str(exc), exc)
        except ValueError as exc:
            raise_tool_error("INVALID_ARGUMENT", str(exc), exc)
        except SyncLoginRequiredError as exc:
            raise_tool_error("AUTHENTICATION_FAILED", str(exc), exc)
        except ResponseTooLargeError as exc:
            raise_tool_error("RESPONSE_TOO_LARGE", str(exc), exc)
        except NetworkError as exc:
            raise_tool_error("NETWORK_ERROR", "remote sync network request failed", exc)
        except SyncError as exc:
            code = "AUTHENTICATION_FAILED" if exc.kind == SyncErrorKind.AUTH else "SYNC_ERROR"
            raise_tool_error(code, "remote sync operation failed", exc)
        except Exception as exc:
            raise_tool_error("INTERNAL_ERROR", "internal collection operation failed", exc)

    @mcp.tool(name="anki_sync_login")
    async def sync_login() -> dict[str, Any]:
        """Authenticate to the configured AnkiWeb or self-hosted sync endpoint."""
        return await execute(
            service.sync_login(
                settings.sync_username,
                settings.sync_password.get_secret_value(),
                settings.sync_endpoint,
            )
        )

    @mcp.tool(name="anki_sync")
    async def sync(sync_media: SyncMedia = True) -> dict[str, Any]:
        """Synchronize the collection with the authenticated remote server."""
        return await execute(service.sync(sync_media))

    @mcp.tool(name="anki_sync_full_download")
    async def sync_full_download(confirm: Confirmation = False) -> dict[str, Any]:
        """Replace local data after the server requires a full download and confirmation is true."""
        if not confirm:
            cause = ValueError("confirm must be true for full download")
            raise_tool_error("INVALID_ARGUMENT", str(cause), cause)
        return await execute(service.full_sync(upload=False))

    @mcp.tool(name="anki_sync_full_upload")
    async def sync_full_upload(confirm: Confirmation = False) -> dict[str, Any]:
        """Replace remote data after the server requires a full upload and confirmation is true."""
        if not confirm:
            cause = ValueError("confirm must be true for full upload")
            raise_tool_error("INVALID_ARGUMENT", str(cause), cause)
        return await execute(service.full_sync(upload=True))

    @mcp.tool(name="anki_decks_list")
    async def decks_list(
        offset: Offset = 0, limit: PageLimit = settings.max_page_size
    ) -> dict[str, Any]:
        """List decks with stable IDs and hierarchy, using bounded offset pagination."""
        return await execute(service.list_decks(offset=offset, limit=limit))

    @mcp.tool(name="anki_decks_get")
    async def decks_get(deck_id: StableId) -> dict[str, Any]:
        """Get metadata for one deck by stable Anki deck ID."""
        return await execute(service.get_deck(deck_id))

    @mcp.tool(name="anki_decks_create")
    async def decks_create(name: DeckName) -> dict[str, Any]:
        """Create a deck by name, or return the existing deck with that name."""
        return await execute(
            service.create_deck(name),
            lambda result: {
                "id": result["id"],
                "name": result["name"],
                "created": result["created"],
            },
        )

    @mcp.tool(name="anki_decks_update")
    async def decks_update(deck_id: StableId, name: DeckName) -> dict[str, Any]:
        """Rename a deck by stable Anki deck ID."""
        return await execute(
            service.update_deck(deck_id, name),
            lambda result: {"id": result["id"], "name": result["name"], "updated": True},
        )

    @mcp.tool(name="anki_decks_delete")
    async def decks_delete(deck_id: StableId, confirm: Confirmation = False) -> dict[str, Any]:
        """Delete a deck and its cards when confirm is explicitly true."""
        if not confirm:
            cause = ValueError("confirm must be true for deck deletion")
            raise_tool_error("INVALID_ARGUMENT", str(cause), cause)
        return await execute(service.delete_deck(deck_id))

    @mcp.tool(name="anki_cards_search")
    async def cards_search(
        query: SearchQuery = "", offset: Offset = 0, limit: PageLimit = settings.max_page_size
    ) -> dict[str, Any]:
        """Search cards with Anki search syntax and bounded offset pagination."""
        return await execute(service.search_cards(query=query, offset=offset, limit=limit))

    @mcp.tool(name="anki_cards_get")
    async def cards_get(card_id: StableId) -> dict[str, Any]:
        """Get card content, deck identity, and scheduling state by stable card ID."""
        return await execute(service.get_card(card_id))

    @mcp.tool(name="anki_cards_create")
    async def cards_create(deck_id: StableId, front: CardText, back: CardText) -> dict[str, Any]:
        """Create one Basic note/card in a deck."""
        return await execute(
            service.create_card(deck_id, front, back),
            lambda result: {
                "id": result["id"],
                "note_id": result["note_id"],
                "deck_id": result["deck_id"],
                "created": True,
            },
        )

    @mcp.tool(name="anki_cards_update")
    async def cards_update(
        card_id: StableId,
        front: CardText | None = None,
        back: CardText | None = None,
        deck_id: StableId | None = None,
    ) -> dict[str, Any]:
        """Update a Basic card's Front/Back fields and/or move it to another deck."""
        return await execute(
            service.update_card(card_id, front, back, deck_id),
            lambda result: {
                "id": result["id"],
                "deck_id": result["deck_id"],
                "updated": True,
            },
        )

    @mcp.tool(name="anki_cards_delete")
    async def cards_delete(card_id: StableId, confirm: Confirmation = False) -> dict[str, Any]:
        """Delete one card and any orphaned note when confirm is explicitly true."""
        if not confirm:
            cause = ValueError("confirm must be true for card deletion")
            raise_tool_error("INVALID_ARGUMENT", str(cause), cause)
        return await execute(service.delete_card(card_id))

    # FastMCP currently generates argument models with Pydantic's extra="ignore".
    # Tighten both runtime validation and the JSON schemas advertised to clients.
    for tool_name in (
        "anki_sync_login",
        "anki_sync",
        "anki_sync_full_download",
        "anki_sync_full_upload",
        "anki_decks_list",
        "anki_decks_get",
        "anki_decks_create",
        "anki_decks_update",
        "anki_decks_delete",
        "anki_cards_search",
        "anki_cards_get",
        "anki_cards_create",
        "anki_cards_update",
        "anki_cards_delete",
    ):
        registered = mcp._tool_manager.get_tool(tool_name)  # pyright: ignore[reportPrivateUsage]
        if registered is None:  # pragma: no cover
            raise RuntimeError(f"failed to register {tool_name}")
        registered.fn_metadata.arg_model.model_config["extra"] = "forbid"
        registered.fn_metadata.arg_model.model_config["hide_input_in_errors"] = True
        registered.fn_metadata.arg_model.model_rebuild(force=True)
        registered.parameters = registered.fn_metadata.arg_model.model_json_schema()
        properties = registered.parameters.get("properties", {})
        offset_schema = properties.get("offset")
        if offset_schema is not None:
            offset_schema["minimum"] = 0
        limit_schema = properties.get("limit")
        if limit_schema is not None:
            limit_schema["minimum"] = 1
            limit_schema["maximum"] = settings.max_page_size
        for id_name in ("deck_id", "card_id"):
            id_schema = properties.get(id_name)
            if id_schema is not None:
                id_schema["minimum"] = 1

    mcp_app = mcp.streamable_http_app()

    async def live(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def ready(_: Request) -> Response:
        try:
            is_ready = await service.check_ready()
        except Exception:
            is_ready = False
        if not is_ready:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return JSONResponse({"status": "ready"})

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncGenerator[None]:
        async with service, mcp_app.router.lifespan_context(mcp_app):
            yield

    routes = [
        Route("/health/live", live, methods=["GET"]),
        Route("/health/ready", ready, methods=["GET"]),
        *mcp_app.routes,
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.settings = settings
    app.state.collection_service = service
    return BearerAuthMiddleware(
        RequestBodyLimitMiddleware(app, settings.max_request_bytes, settings.mcp_path),
        settings.auth_token.get_secret_value(),
        settings.mcp_path,
    )
