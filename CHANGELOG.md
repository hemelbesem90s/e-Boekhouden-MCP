# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- Autonomous IMAP email polling & IP whitelisting bridge (`eboekhouden_mcp/web_session/email_whitelist.py`) using read-only `BODY.PEEK` to resolve new-IP challenges automatically during headless web session login.
- Session cookie jar persistence (`SRVRID`, `DG`, `LG`, `ISLOGINX`, `v1_sessionKey`), host OS User-Agent alignment, and optional HTTP/SOCKS5 egress proxy routing (`EBOEKHOUDEN_MCP_WEB_PROXY`).
- CLI flags `--whitelist-ip`, `--auto-whitelist-ip`, and `--set-imap-credentials` in `eboekhouden-mcp`.
- Architecture Decision Record `docs/adr/ADR_0002_Web_Session_IP_Whitelist_Workaround.md`.
- Strict MCP input validation for pagination, invoice lines, and batch requests.
- Rate-limit metadata and one-time expired-session recovery.
- Coverage, type checking, security scanning, CodeQL, and release artifact checks.

### Fixed

- Preserve explicitly supplied VAT amounts on mutation rows.
- Avoid mutable defaults on invoice and mutation response collections.

## 0.2.0 - 2026-07-21

### Added

- Standalone MCP distribution with its bundled e-Boekhouden API client.
- Read-only default tool surface and operator-controlled write tools.
- Explicit write confirmation, tool annotations, API host guarding, and archive
  path allowlisting.
- Setup instructions for common local MCP clients.
