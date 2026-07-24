from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from anki_mcp.config import Settings


def env(tmp_path: Path, **values: str) -> dict[str, str]:
    base = {"MCP_AUTH_TOKEN": "test-secret", "ANKI_COLLECTION_PATH": str(tmp_path / "c.anki2")}
    base.update(values)
    return base


def test_direct_token_and_defaults(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, **env(tmp_path))
    assert settings.auth_token.get_secret_value() == "test-secret"
    assert settings.host == "0.0.0.0"
    assert settings.port == 8000
    assert settings.mcp_path == "/mcp"
    assert settings.max_page_size == 100
    assert settings.max_search_scan == 10_000
    assert settings.max_rendered_field_bytes == 262_144


def test_token_file_strips_one_trailing_newline(tmp_path: Path) -> None:
    secret = tmp_path / "token"
    secret.write_text("file-secret\n", encoding="utf-8")
    values = env(tmp_path, MCP_AUTH_TOKEN_FILE=str(secret))
    del values["MCP_AUTH_TOKEN"]
    assert Settings(_env_file=None, **values).auth_token.get_secret_value() == "file-secret"


@pytest.mark.parametrize(
    "changes",
    [{}, {"MCP_AUTH_TOKEN": "x", "MCP_AUTH_TOKEN_FILE": "/tmp/token"}],
)
def test_exactly_one_token_source_is_required(tmp_path: Path, changes: dict[str, str]) -> None:
    values = {"ANKI_COLLECTION_PATH": str(tmp_path / "c.anki2"), **changes}
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_empty_direct_token_still_conflicts_with_token_file(tmp_path: Path) -> None:
    secret = tmp_path / "credential"
    secret.write_text("file-secret", encoding="utf-8")
    values = env(tmp_path)
    values["MCP_AUTH_TOKEN"] = ""
    values["MCP_AUTH_TOKEN_FILE"] = str(secret)
    with pytest.raises(ValidationError, match="exactly one"):
        Settings(_env_file=None, **values)


def test_missing_or_empty_token_file_fails(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **env(tmp_path, MCP_AUTH_TOKEN=""))


def test_limits_and_paths_are_validated(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **env(tmp_path, MCP_MAX_PAGE_SIZE="0"))
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **env(tmp_path, MCP_PATH="mcp"))
    with pytest.raises(ValidationError, match="health"):
        Settings(_env_file=None, **env(tmp_path, MCP_PATH="/health/mcp"))


@pytest.mark.parametrize(
    "mcp_path", ["/{path}", "/api/{rest:path}", "/mcp?debug=1", "/mcp#fragment", "/mcp path"]
)
def test_mcp_path_must_be_a_static_url_path(tmp_path: Path, mcp_path: str) -> None:
    with pytest.raises(ValidationError, match="static URL path"):
        Settings(_env_file=None, **env(tmp_path, MCP_PATH=mcp_path))


def test_unknown_configuration_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **env(tmp_path), SURPRISE="nope")
