from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated process configuration.

    Token material is resolved once at startup and retained as a SecretStr.
    """

    model_config = SettingsConfigDict(
        env_file=None, case_sensitive=True, extra="forbid", populate_by_name=True
    )

    host: str = Field("0.0.0.0", alias="MCP_HOST")
    port: int = Field(8000, ge=1, le=65535, alias="MCP_PORT")
    mcp_path: str = Field("/mcp", alias="MCP_PATH")
    auth_token_input: SecretStr | None = Field(None, alias="MCP_AUTH_TOKEN")
    auth_token_file: Path | None = Field(None, alias="MCP_AUTH_TOKEN_FILE")
    collection_path: Path = Field(Path("/data/collection.anki2"), alias="ANKI_COLLECTION_PATH")
    max_page_size: int = Field(100, ge=1, le=1000, alias="MCP_MAX_PAGE_SIZE")
    max_search_scan: int = Field(10_000, ge=1, le=1_000_000, alias="MCP_MAX_SEARCH_SCAN")
    max_rendered_field_bytes: int = Field(
        262_144, ge=1, le=4_194_304, alias="MCP_MAX_RENDERED_FIELD_BYTES"
    )
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    allowed_hosts_csv: str = Field(
        "testserver,localhost,localhost:*,127.0.0.1,127.0.0.1:*,anki-mcp,anki-mcp:*",
        alias="MCP_ALLOWED_HOSTS",
    )
    allowed_origins_csv: str = Field(
        "http://localhost:*,http://127.0.0.1:*", alias="MCP_ALLOWED_ORIGINS"
    )
    auth_token: SecretStr = Field(default=SecretStr(""), exclude=True)

    @field_validator("mcp_path")
    @classmethod
    def valid_mcp_path(cls, value: str) -> str:
        if not value.startswith("/") or value == "/":
            raise ValueError("MCP_PATH must be an absolute, non-root path")
        if any(character in value for character in "{}?#") or any(
            character.isspace() for character in value
        ):
            raise ValueError("MCP_PATH must be a static URL path without templates or whitespace")
        value = value.rstrip("/")
        if value == "/health" or value.startswith("/health/"):
            raise ValueError("MCP_PATH must not overlap the reserved /health routes")
        return value

    @property
    def allowed_hosts(self) -> list[str]:
        return [item.strip() for item in self.allowed_hosts_csv.split(",") if item.strip()]

    @property
    def allowed_origins(self) -> list[str]:
        return [item.strip() for item in self.allowed_origins_csv.split(",") if item.strip()]

    @model_validator(mode="before")
    @classmethod
    def resolve_token(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        values = dict(data)
        direct = values.get("MCP_AUTH_TOKEN", values.get("auth_token_input"))
        file_value = values.get("MCP_AUTH_TOKEN_FILE", values.get("auth_token_file"))
        # During environment loading aliases are present in data. Exactly one is mandatory.
        direct_present = direct is not None
        file_present = file_value is not None
        if direct_present == file_present:
            raise ValueError("exactly one of MCP_AUTH_TOKEN and MCP_AUTH_TOKEN_FILE is required")
        if direct_present:
            raw = direct.get_secret_value() if isinstance(direct, SecretStr) else str(direct)
        else:
            try:
                raw = Path(str(file_value)).read_text(encoding="utf-8").removesuffix("\n")
            except OSError as exc:
                raise ValueError("unable to read MCP_AUTH_TOKEN_FILE") from exc
        if not raw:
            raise ValueError("MCP authentication token must not be empty")
        values["auth_token"] = SecretStr(raw)
        return values
