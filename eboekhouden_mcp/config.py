"""Configuration for the e-Boekhouden MCP server."""

import os
import subprocess  # nosec B404
import sys
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing_extensions import Self

OFFICIAL_API_HOST = "api.e-boekhouden.nl"
_KEYRING_CACHE: dict[tuple[str, str], str] = {}


def get_keyring_secret(service_name: str, account: str) -> str | None:
    """Read a credential from OS keyring without triggering macOS unsigned-binary password popups."""
    import keyring

    # Respect unittest.mock.patch("keyring.get_password") in unit tests
    if hasattr(keyring.get_password, "_mock_name") or hasattr(
        keyring.get_password, "assert_called"
    ):
        val = keyring.get_password(service_name, account)
        return val.strip() if isinstance(val, str) and val.strip() else None

    cache_key = (service_name, account)
    if cache_key in _KEYRING_CACHE:
        return _KEYRING_CACHE[cache_key]

    if sys.platform == "darwin" and os.path.exists("/usr/bin/security"):
        proc = subprocess.run(  # nosec B603
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                service_name,
                "-a",
                account,
                "-w",
            ],
            capture_output=True,
            check=False,
            text=True,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            secret = proc.stdout.strip()
            _KEYRING_CACHE[cache_key] = secret
            return secret
        return None

    val = keyring.get_password(service_name, account)
    if isinstance(val, str) and val.strip():
        secret = val.strip()
        _KEYRING_CACHE[cache_key] = secret
        return secret
    return None


def set_keyring_secret(service_name: str, account: str, secret: str) -> None:
    """Store a credential in OS keyring without triggering legacy macOS password prompts on read."""
    cleaned = secret.strip()
    if not cleaned:
        raise ValueError(f"{account} must not be empty")

    _KEYRING_CACHE[(service_name, account)] = cleaned

    if sys.platform == "darwin" and os.path.exists("/usr/bin/security"):
        # Delete any existing item first so its legacy partition ACL is replaced cleanly
        subprocess.run(  # nosec B603
            [
                "/usr/bin/security",
                "delete-generic-password",
                "-s",
                service_name,
                "-a",
                account,
            ],
            capture_output=True,
            check=False,
            text=True,
        )
        proc = subprocess.run(  # nosec B603
            [
                "/usr/bin/security",
                "add-generic-password",
                "-U",
                "-A",
                "-s",
                service_name,
                "-a",
                account,
                "-w",
                cleaned,
            ],
            capture_output=True,
            check=False,
            text=True,
        )
        if proc.returncode == 0:
            return

    import keyring

    keyring.set_password(service_name, account, cleaned)


def migrate_macos_keychain_acl(
    service_name: str = "eboekhouden_mcp",
) -> list[str]:
    """Re-store existing macOS Keychain credentials with -A ACL to stop repeated password popups."""
    import keyring

    migrated: list[str] = []
    for account in (
        "secret_token",
        "web_session_token",
        "web_username",
        "web_password",
        "web_totp_secret",
    ):
        val = keyring.get_password(service_name, account)
        if val and val.strip():
            set_keyring_secret(service_name, account, val.strip())
            migrated.append(account)
    return migrated


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
    web_username: str | None = Field(
        default=None,
        validation_alias="EBOEKHOUDEN_MCP_WEB_USERNAME",
    )
    web_password: SecretStr | None = Field(
        default=None,
        validation_alias="EBOEKHOUDEN_MCP_WEB_PASSWORD",
    )
    web_totp_secret: SecretStr | None = Field(
        default=None,
        validation_alias="EBOEKHOUDEN_MCP_WEB_TOTP_SECRET",
    )
    web_session_base_url: str = Field(
        default="https://secure20.e-boekhouden.nl",
        validation_alias="EBOEKHOUDEN_MCP_WEB_SESSION_BASE_URL",
    )
    web_proxy: str | None = Field(
        default=None,
        validation_alias="EBOEKHOUDEN_MCP_WEB_PROXY",
    )
    auto_whitelist_ip: bool = Field(
        default=True,
        validation_alias="EBOEKHOUDEN_MCP_AUTO_WHITELIST_IP",
    )
    imap_server: str | None = Field(
        default=None,
        validation_alias="EBOEKHOUDEN_MCP_IMAP_SERVER",
    )
    imap_username: str | None = Field(
        default=None,
        validation_alias="EBOEKHOUDEN_MCP_IMAP_USERNAME",
    )
    imap_password: SecretStr | None = Field(
        default=None,
        validation_alias="EBOEKHOUDEN_MCP_IMAP_PASSWORD",
    )

    @model_validator(mode="before")
    @classmethod
    def resolve_token_from_keyring(cls, data: Any) -> Any:
        d = dict(data) if isinstance(data, dict) else {}
        token = d.get("secret_token") or d.get("EBOEKHOUDEN_MCP_SECRET_TOKEN")
        if isinstance(token, SecretStr):
            raw_token = token.get_secret_value()
        else:
            raw_token = str(token) if token is not None else ""  # noqa: F841

        service_name = d.get(
            "keyring_service_name",
            os.environ.get("EBOEKHOUDEN_MCP_KEYRING_SERVICE", "eboekhouden_mcp"),
        )
        web_enabled = str(
            d.get("enable_web_session")
            or d.get("EBOEKHOUDEN_MCP_ENABLE_WEB_SESSION")
            or os.environ.get("EBOEKHOUDEN_MCP_ENABLE_WEB_SESSION")
            or ""
        ).lower() in ("true", "1", "yes")
        web_token = (
            d.get("web_session_token")
            or d.get("EBOEKHOUDEN_MCP_WEB_SESSION_TOKEN")
            or os.environ.get("EBOEKHOUDEN_MCP_WEB_SESSION_TOKEN")
        )
        web_user = (
            d.get("web_username")
            or d.get("EBOEKHOUDEN_MCP_WEB_USERNAME")
            or os.environ.get("EBOEKHOUDEN_MCP_WEB_USERNAME")
        )
        web_pass = (
            d.get("web_password")
            or d.get("EBOEKHOUDEN_MCP_WEB_PASSWORD")
            or os.environ.get("EBOEKHOUDEN_MCP_WEB_PASSWORD")
        )
        web_totp = (
            d.get("web_totp_secret")
            or d.get("EBOEKHOUDEN_MCP_WEB_TOTP_SECRET")
            or os.environ.get("EBOEKHOUDEN_MCP_WEB_TOTP_SECRET")
        )

        if token is None or web_enabled:
            try:
                if token is None:
                    keyring_token = get_keyring_secret(service_name, "secret_token")
                    if keyring_token:
                        d["secret_token"] = keyring_token
                if web_enabled:
                    if not web_token:
                        keyring_web_token = get_keyring_secret(
                            service_name, "web_session_token"
                        )
                        if keyring_web_token:
                            d["web_session_token"] = keyring_web_token
                    if not web_user:
                        keyring_web_user = get_keyring_secret(
                            service_name, "web_username"
                        )
                        if keyring_web_user:
                            d["web_username"] = keyring_web_user
                    if not web_pass:
                        keyring_web_pass = get_keyring_secret(
                            service_name, "web_password"
                        )
                        if keyring_web_pass:
                            d["web_password"] = keyring_web_pass
                    if not web_totp:
                        keyring_web_totp = get_keyring_secret(
                            service_name, "web_totp_secret"
                        )
                        if keyring_web_totp:
                            d["web_totp_secret"] = keyring_web_totp
            except Exception:  # nosec B110
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


def load_settings(**kwargs: Any) -> MCPConfig:
    """Instantiate and return the validated MCPConfig settings."""
    return MCPConfig(**kwargs)
