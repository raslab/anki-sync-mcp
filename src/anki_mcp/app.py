from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Awaitable
from contextlib import asynccontextmanager
from typing import Any, NoReturn, TypeVar
from uuid import uuid4

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import StrictInt
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp

from anki_mcp.auth import BearerAuthMiddleware
from anki_mcp.collection import AnkiCollectionService
from anki_mcp.config import Settings

T = TypeVar("T")
Offset = StrictInt
PageLimit = StrictInt
StableId = StrictInt


def create_app(settings: Settings) -> ASGIApp:
    """Create the complete ASGI application and read-only MCP registry."""

    service = AnkiCollectionService(
        settings.collection_path,
        settings.max_page_size,
        settings.max_search_scan,
        settings.max_rendered_field_bytes,
    )
    mcp = FastMCP(
        "anki-mcp",
        instructions="Read-only access to one local Anki collection.",
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

    async def execute(operation: Awaitable[T]) -> T:
        try:
            return await operation
        except LookupError as exc:
            raise_tool_error("NOT_FOUND", str(exc), exc)
        except ValueError as exc:
            raise_tool_error("INVALID_ARGUMENT", str(exc), exc)
        except Exception as exc:
            raise_tool_error("INTERNAL_ERROR", "internal collection operation failed", exc)

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

    @mcp.tool(name="anki_cards_search")
    async def cards_search(
        query: str = "", offset: Offset = 0, limit: PageLimit = settings.max_page_size
    ) -> dict[str, Any]:
        """Search cards with Anki search syntax and bounded offset pagination."""
        return await execute(service.search_cards(query=query, offset=offset, limit=limit))

    @mcp.tool(name="anki_cards_get")
    async def cards_get(card_id: StableId) -> dict[str, Any]:
        """Get card content, deck identity, and scheduling state by stable card ID."""
        return await execute(service.get_card(card_id))

    # FastMCP currently generates argument models with Pydantic's extra="ignore".
    # Tighten both runtime validation and the JSON schemas advertised to clients.
    for tool_name in (
        "anki_decks_list",
        "anki_decks_get",
        "anki_cards_search",
        "anki_cards_get",
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
        app,
        settings.auth_token.get_secret_value(),
        settings.mcp_path,
    )
