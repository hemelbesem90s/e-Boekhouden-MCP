---
id: ADR-0002
title: "Autonomous Web Session IP Whitelisting & Egress Architecture"
status: accepted
created: 2026-11-11
updated: 2026-11-11
tags: [architecture, adr, security, web-session, imap, proxy]
Create PDF: False
---

# ADR 0002: Autonomous Web Session IP Whitelisting & Egress Architecture

- **Status**: Approved & Implemented
- **Date**: 2026-11-11
- **Deciders**: Jacob Grobler, Core Agentic Engineering
- **Consulted**: `grobler-eboekhouden-accounting`, `26077I_GRBL_eBoekhouden_Hybrid_MCP`

---

> [!CAUTION]
> **CRITICAL INVARIANT (Chesterton's Fence)**:
> 1. **Non-Destructive IMAP Polling (`BODY.PEEK`)**: All automated IMAP polling in `eboekhouden_mcp/web_session/email_whitelist.py` MUST select the mailbox with `readonly=True` and fetch messages exclusively via `(BODY.PEEK[])`. Never use `RFC822` or `(BODY[])`, which sets the `\Seen` flag on the server and silently marks the user's incoming emails as read.
> 2. **Bounded Socket Timeout**: `imaplib.IMAP4_SSL` MUST always be instantiated with an explicit `timeout=15` to prevent hung network sockets from blocking the MCP stdio event loop.
> 3. **Recursion Guard (`auto_whitelist=False`)**: When `whitelist_ip()` performs a post-whitelist login (`login()` / `async_login()`), it MUST pass `_allow_auto_whitelist=False` to prevent infinite recursion if e-Boekhouden returns a second IP challenge.
> 4. **No `Chrome Safe Storage` Keychain Probing**: Never query macOS Keychain for `Chrome Safe Storage` from an unsigned Python/CLI process; doing so triggers a blocking macOS system password modal that hangs headless execution.

## 1. Context & Problem Statement

The `eboekhouden` Hybrid MCP server provides 30 Web Session Bridge tools for advanced accounting features not exposed by the official e-Boekhouden REST API v1. These include:
- Unprocessed bank statement import grids (`get_unprocessed_bank_transactions`)
- Mutation attachment linking (`attach_file_to_mutation`)
- Digital Archive folder/file management (`list_archive_folders`, `upload_archive_file`)
- WIP time tracking (`list_time_entries`, `invoice_time_entries`)
- Quote/offerte lifecycle management (`list_quotes`, `create_quote`, `update_quote`)
- Direct email invoicing (`send_invoice`)

### The Trigger Failure
When running automated background agents or scheduled jobs on mobile laptops, the egress public IP address changes whenever switching between home Wi-Fi, office networks, client locations, and mobile hotspots.

e-Boekhouden enforces an automated security policy:
> *If an account attempts login from an IP address not used for that account in the past 30 days, e-Boekhouden blocks immediate web login and sends an email confirmation link to the user's registered address.*

In headless MCP server execution, this caused `WebSessionClient.login()` to fail with `IPVerificationRequiredError`. Background tools failed immediately, forcing human intervention to open email, locate the authorization link, and submit it manually via `whitelist_web_session_ip`.

---

## 2. Evaluation of Candidate Workarounds

Three primary architectures were evaluated to solve the new-IP email confirmation block:

| Candidate | Mechanism | Strengths | Failure Modes & Risks | Verdict |
| :--- | :--- | :--- | :--- | :--- |
| **1. Chrome Profile / CDP Cookie Extraction** | Decrypt `auth-token` and session cookies from macOS Chrome SQLite database (`~/Library/Application Support/Google/Chrome/Default/Cookies`). | Reuses existing browser session if user is logged into e-Boekhouden. | 1. Reading `Chrome Safe Storage` on macOS triggers an intrusive OS GUI prompt requiring system password.<br>2. Chrome `auth-token` is a session cookie wiped on browser exit.<br>3. e-Boekhouden's 30-day IP whitelist check is server-side; new IPs trigger verification emails even within Chrome. | **Rejected as standalone**; adopted only as cookie jar persistence & macOS UA alignment. |
| **2. Autonomous Email Confirmation Bridge** | Automatically intercept `IPVerificationRequiredError` during `login()`, poll IMAP non-destructively (`BODY.PEEK`), extract approval URL/code, and submit `whitelist_ip()`. | 1. 100% autonomous with zero human intervention.<br>2. Preserves unread status of user inbox.<br>3. Works from any network globally without external infrastructure.<br>4. Re-authorizes IP for 30 full days on e-Boekhouden servers. | Requires IMAP credentials (already configured in OS Keychain `grobler_gmail`). | **Accepted as Primary Engine**. |
| **3. Static Tailscale Exit Node / VPS Proxy** | Route all web session traffic through a fixed static egress IP (e.g. Hetzner VPS or home server via Tailscale/SOCKS5 proxy). | Guarantees a single fixed IP address that remains perpetually whitelisted. | 1. Currently no Tailscale exit nodes exist on user's tailnet.<br>2. Single point of failure if the exit node reboots or goes offline.<br>3. Additional network latency for all API requests. | **Accepted as Optional Extension** (`EBOEKHOUDEN_MCP_WEB_PROXY`). |

---

## 3. Decision & Recommended Hybrid Architecture

We implement a **3-Tier Hybrid Architecture**:

```
+---------------------------------------------------------------------------------+
|                        WebSessionClient Authentication                          |
+---------------------------------------------------------------------------------+
                                       |
                     [1. Check Valid auth-token in Memory]
                                       |
                   +-------------------+-------------------+
                   | (Valid)                               | (Expired / Missing)
             [Execute Request]                       [Attempt Login]
                                                           |
                                             [e-Boekhouden IP Gate Check]
                                                           |
                                      +--------------------+--------------------+
                                      | (Whitelisted IP)                        | (New / Unrecognized IP)
                              [Return JWT auth-token]               [IPVerificationRequiredError]
                                                                                |
                                                                [Auto-Whitelist Enabled & Configured?]
                                                                                |
                                                      +-------------------------+-------------------------+
                                                      | Yes                                               | No / Disabled
                                          [Poll IMAP via BODY.PEEK]                          [Raise IPVerificationRequiredError]
                                          [Scan recent e-Boekhouden email]
                                          [Extract URL/code (?code=...)]
                                                      |
                                          [Call whitelist_ip(code)]
                                          [POST /v1/api/loginwhitelist/whitelist]
                                                      |
                                          [Silent Auto-Login & Mint JWT]
                                          [Persist Token to OS Keychain]
                                                      |
                                          [Resume Original MCP Request]
```

### Architectural Pillars:
1. **Tier 1: Device Consistency & Cookie Persistence**:
   - Align User-Agent with host OS (macOS Chrome when on Darwin).
   - In-memory cookie jar propagates `SRVRID`, `DG`, `LG`, and `v1_sessionKey`.
2. **Tier 2: Configurable Egress Proxy (`EBOEKHOUDEN_MCP_WEB_PROXY`)**:
   - Standard HTTP/SOCKS5 proxy support passed to `httpx.Client` and `httpx.AsyncClient`.
   - Allows operators to route web traffic through Tailscale or fixed VPS nodes if desired.
3. **Tier 3: Autonomous Email Whitelist Bridge (`email_whitelist.py`)**:
   - Transparently catches `IPVerificationRequiredError` during synchronous or asynchronous `login()`.
   - Polls IMAP using `BODY.PEEK` (read-only, zero user inbox disruption) for e-Boekhouden authorization email.
   - Extracts approval link/code and executes `POST /v1/api/loginwhitelist/whitelist`.
   - Completes login and returns fresh JWT token to resume the caller's request seamlessly.
   - Fails closed to clear error messaging if email credentials are unavailable or polling times out.

---

## 4. Consequences & Verification

- **Positive**:
  - Background MCP tools (`list_time_entries`, `get_unprocessed_bank_transactions`, `list_quotes`, `list_archive_folders`) no longer break when laptop connects from new networks.
  - Zero disruption to user's mailbox: emails inspected via `BODY.PEEK` remain unread.
  - 30-day IP whitelist persistence reduces email polling to once a month per network location.
- **Test Coverage**:
  - Full suite expanded to 206 unit and integration tests passing with 100% pass rate.
  - Includes mocked IMAP polling, HTML link extraction, auto-login recursion prevention, proxy configuration, and timeout fallback.
