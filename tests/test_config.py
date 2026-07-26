from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from anki_mcp.config import Settings, validate_sync_migration_endpoint


def env(tmp_path: Path, **values: str) -> dict[str, str]:
    base = {
        "MCP_AUTH_TOKEN": "test-secret",
        "ANKI_COLLECTION_PATH": str(tmp_path / "c.anki2"),
        "ANKI_SYNC_USERNAME": "sync-user",
        "ANKI_SYNC_PASSWORD": "sync-password",
        "ANKI_SYNC_HOST": "https://sync.example.test/",
    }
    base.update(values)
    return base


def test_readme_documents_every_environment_setting() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"\| `([A-Z][A-Z0-9_]*)` \|", readme))
    configured = {
        field.alias
        for field in Settings.model_fields.values()
        if isinstance(field.alias, str) and field.alias[:1].isupper()
    }

    assert documented == configured


def test_direct_token_and_defaults(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, **env(tmp_path))
    assert settings.auth_token.get_secret_value() == "test-secret"
    assert settings.host == "0.0.0.0"
    assert settings.port == 8000
    assert settings.mcp_path == "/mcp"
    assert settings.max_page_size == 100
    assert settings.max_search_scan == 10_000
    assert settings.max_rendered_field_bytes == 262_144
    assert settings.max_card_fields == 100
    assert settings.max_media_bytes == 1_048_576
    assert settings.sync_timeout_seconds == 300
    assert settings.max_response_bytes == 1_048_576
    assert settings.max_request_bytes == 1_048_576
    assert settings.sync_username == "sync-user"
    assert settings.sync_password.get_secret_value() == "sync-password"
    assert settings.sync_host == "https://sync.example.test/"
    assert settings.scopes == frozenset({"read", "write", "admin"})
    assert settings.sync_on_read is False
    assert settings.sync_on_write is True
    assert settings.allow_destructive is False
    assert settings.allow_full_sync is False
    assert settings.allow_schema_changes is False
    assert settings.bootstrap_mode == "disabled"
    assert settings.max_batch_size == 50


def test_sync_username_can_be_empty_until_login(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, **env(tmp_path, ANKI_SYNC_USERNAME=""))

    assert settings.sync_username == ""


def test_sync_password_can_be_omitted_for_persisted_auth(tmp_path: Path) -> None:
    values = env(tmp_path)
    del values["ANKI_SYNC_PASSWORD"]
    assert Settings(_env_file=None, **values).sync_password is None


def test_sync_password_can_be_loaded_from_secret_file(tmp_path: Path) -> None:
    secret = tmp_path / "sync-password"
    secret.write_text("file-sync-password\n", encoding="utf-8")
    values = env(tmp_path, ANKI_SYNC_PASSWORD_FILE=str(secret))
    del values["ANKI_SYNC_PASSWORD"]

    settings = Settings(_env_file=None, **values)

    assert settings.sync_password.get_secret_value() == "file-sync-password"


def test_exactly_one_sync_password_source_is_required(tmp_path: Path) -> None:
    secret = tmp_path / "sync-password"
    secret.write_text("file-sync-password", encoding="utf-8")
    with pytest.raises(ValidationError, match="exactly one"):
        Settings(
            _env_file=None,
            **env(tmp_path, ANKI_SYNC_PASSWORD_FILE=str(secret)),
        )


def test_empty_sync_host_selects_ankiweb(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, **env(tmp_path, ANKI_SYNC_HOST=""))
    assert settings.sync_endpoint is None


@pytest.mark.parametrize(
    "host",
    [
        "sync.example.test",
        "ftp://sync.example.test",
        "https://",
        "http://sync.example.test",
        "https://user:secret@sync.example.test",
        "https://sync.example.test?token=secret",
        "https://sync.example.test/#fragment",
        "https://sync.example.test:invalid/",
        "https://sync.example.test/\nheader",
        "https://:443",
        "https:// /",
        "https://sync example.test/",
    ],
)
def test_sync_host_rejects_insecure_or_credential_bearing_urls(tmp_path: Path, host: str) -> None:
    with pytest.raises(ValidationError, match="ANKI_SYNC_HOST"):
        Settings(_env_file=None, **env(tmp_path, ANKI_SYNC_HOST=host))


@pytest.mark.parametrize(
    "host", ["http://localhost:8080", "http://127.0.0.1:8080", "http://[::1]:8080"]
)
def test_sync_host_allows_http_only_for_loopback_development(tmp_path: Path, host: str) -> None:
    assert Settings(_env_file=None, **env(tmp_path, ANKI_SYNC_HOST=host)).sync_host == host


def test_sync_migrations_remain_within_the_login_trust_boundary() -> None:
    assert (
        validate_sync_migration_endpoint(
            "https://self.example.test/new-path", "https://self.example.test/base-path"
        )
        == "https://self.example.test/new-path"
    )
    assert (
        validate_sync_migration_endpoint("https://sync17.ankiweb.net/sync/", None)
        == "https://sync17.ankiweb.net/sync/"
    )
    with pytest.raises(ValueError, match="trusted origin"):
        validate_sync_migration_endpoint(
            "https://other.example.test/", "https://self.example.test/"
        )
    with pytest.raises(ValueError, match="untrusted origin"):
        validate_sync_migration_endpoint("https://127.0.0.1/", None)


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
        Settings(_env_file=None, **env(tmp_path, MCP_MAX_CARD_FIELDS="0"))
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **env(tmp_path, ANKI_MAX_MEDIA_BYTES="0"))
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **env(tmp_path, ANKI_SYNC_TIMEOUT_SECONDS="0"))
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **env(tmp_path, MCP_MAX_RESPONSE_BYTES="1023"))
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **env(tmp_path, MCP_MAX_REQUEST_BYTES="1023"))
    with pytest.raises(ValidationError, match="MCP_MAX_RESPONSE_BYTES"):
        Settings(
            _env_file=None,
            **env(
                tmp_path,
                MCP_MAX_RENDERED_FIELD_BYTES="4096",
                MCP_MAX_RESPONSE_BYTES="4096",
            ),
        )
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **env(tmp_path, MCP_PATH="mcp"))
    with pytest.raises(ValidationError, match="health"):
        Settings(_env_file=None, **env(tmp_path, MCP_PATH="/health/mcp"))


@pytest.mark.parametrize(
    ("secret_key", "file_key", "marker"),
    [
        ("MCP_AUTH_TOKEN", "MCP_AUTH_TOKEN_FILE", "BEARER-PLAINTEXT-MARKER"),
        ("ANKI_SYNC_PASSWORD", "ANKI_SYNC_PASSWORD_FILE", "SYNC-PLAINTEXT-MARKER"),
    ],
)
def test_secret_markers_are_hidden_from_validation_errors(
    tmp_path: Path, secret_key: str, file_key: str, marker: str
) -> None:
    secret_file = tmp_path / "conflicting-secret"
    secret_file.write_text("file-secret", encoding="utf-8")
    values = env(tmp_path)
    values[secret_key] = marker
    values[file_key] = str(secret_file)

    with pytest.raises(ValidationError) as captured:
        Settings(_env_file=None, **values)

    assert marker not in str(captured.value)


@pytest.mark.parametrize(
    "mcp_path", ["/{path}", "/api/{rest:path}", "/mcp?debug=1", "/mcp#fragment", "/mcp path"]
)
def test_mcp_path_must_be_a_static_url_path(tmp_path: Path, mcp_path: str) -> None:
    with pytest.raises(ValidationError, match="static URL path"):
        Settings(_env_file=None, **env(tmp_path, MCP_PATH=mcp_path))


def test_unknown_configuration_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **env(tmp_path), SURPRISE="nope")


def test_scopes_and_phase_one_safety_settings_are_validated(tmp_path: Path) -> None:
    configured = Settings(
        _env_file=None,
        **env(
            tmp_path,
            MCP_SCOPES="read,write,admin,destructive",
            ANKI_ALLOW_DESTRUCTIVE="true",
            ANKI_ALLOW_FULL_SYNC="true",
            ANKI_SYNC_ON_READ="true",
            ANKI_SYNC_ON_WRITE="false",
            ANKI_BOOTSTRAP_MODE="download_if_empty",
            ANKI_MAX_BATCH_SIZE="5",
        ),
    )
    assert configured.scopes == frozenset({"read", "write", "admin", "destructive"})
    assert configured.allow_destructive is True
    assert configured.allow_full_sync is True
    assert configured.sync_on_read is True
    assert configured.sync_on_write is False
    assert configured.bootstrap_mode == "download_if_empty"
    assert configured.max_batch_size == 5

    with pytest.raises(ValidationError, match="scope"):
        Settings(_env_file=None, **env(tmp_path, MCP_SCOPES="read,root"))
    with pytest.raises(ValidationError, match="ANKI_BOOTSTRAP_MODE"):
        Settings(_env_file=None, **env(tmp_path, ANKI_BOOTSTRAP_MODE="upload"))
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **env(tmp_path, ANKI_MAX_BATCH_SIZE="0"))
