"""Configuration for the e-Boekhouden MCP server."""

import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing_extensions import Self

OFFICIAL_API_HOST = "api.e-boekhouden.nl"


class MCPConfig(BaseSettings):
    """Configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="EBOEKHOUDEN_MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
        extra="ignore",
    )

    # e-Boekhouden API settings
    secret_token: SecretStr
    api_url: str = "https://api.e-boekhouden.nl/"
    source: str = "MCP-Server"

    # MCP server settings
    server_name: str = "eboekhouden"
    server_version: str = "0.2.0"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "WARNING"
    enable_write_tools: bool = False
    enable_archive_tool: bool = False
    archive_root: Path | None = None
    allow_custom_api_url: bool = False
    require_biometric: bool = Field(
        default=True,
        validation_alias="EBOEKHOUDEN_MCP_REQUIRE_BIOMETRIC",
    )
    keyring_service_name: str = Field(
        default="eboekhouden_mcp",
        validation_alias="EBOEKHOUDEN_MCP_KEYRING_SERVICE",
    )

    # Web session bridge settings
    enable_web_session: bool = Field(
        default=False,
        validation_alias="EBOEKHOUDEN_MCP_ENABLE_WEB_SESSION",
    )
    web_session_token: SecretStr | None = Field(
        default=None,
        validation_alias="EBOEKHOUDEN_MCP_WEB_SESSION_TOKEN",
    )
    web_session_base_url: str = Field(
        default="https://secure20.e-boekhouden.nl",
        validation_alias="EBOEKHOUDEN_MCP_WEB_SESSION_BASE_URL",
    )

    @model_validator(mode="before")
    @classmethod
    def resolve_token_from_keyring(cls, data: Any) -> Any:
        d = dict(data) if isinstance(data, dict) else {}
        token = d.get("secret_token") or d.get("EBOEKHOUDEN_MCP_SECRET_TOKEN")
        if isinstance(token, SecretStr):
            raw_token = token.get_secret_value()
        else:
            raw_token = str(token) if token is not None else ""

        if not raw_token.strip():
            service_name = d.get(
                "keyring_service_name",
                os.environ.get("EBOEKHOUDEN_MCP_KEYRING_SERVICE", "eboekhouden_mcp"),
            )
            try:
                import keyring

                keyring_token = keyring.get_password(service_name, "secret_token")
                if keyring_token and keyring_token.strip():
                    d["secret_token"] = keyring_token.strip()
            except Exception:
                pass
        return d

    @field_validator("secret_token")
    @classmethod
    def validate_secret_token(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value().strip()
        if not token:
            raise ValueError("secret_token must not be empty")
        return SecretStr(token)

    @model_validator(mode="after")
    def validate_security_settings(self) -> Self:
        """Reject credential exfiltration and unsafe archive configurations."""
        parsed = urlparse(self.api_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("api_url must be an absolute HTTPS URL")
        if parsed.username or parsed.password:
            raise ValueError("api_url must not contain credentials")
        if not self.allow_custom_api_url and parsed.hostname != OFFICIAL_API_HOST:
            raise ValueError(
                f"api_url must use {OFFICIAL_API_HOST}; set "
                "EBOEKHOUDEN_MCP_ALLOW_CUSTOM_API_URL=true only for a trusted endpoint"
            )

        if self.enable_archive_tool:
            if not self.enable_write_tools:
                raise ValueError(
                    "the archive tool also requires enable_write_tools=true"
                )
            if self.archive_root is None:
                raise ValueError(
                    "archive_root is required when the archive tool is enabled"
                )
            archive_root = self.archive_root.expanduser().resolve()
            if not archive_root.is_dir():
                raise ValueError("archive_root must be an existing directory")
            self.archive_root = archive_root

        return self
