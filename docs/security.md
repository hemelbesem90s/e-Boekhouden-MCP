# Security model

e-Boekhouden MCP is a local adapter between one MCP client and the official
e-Boekhouden API. It is not an authentication broker, credential vault, hosted
service, or multi-user authorization layer.

## Data flow

1. The MCP client starts `eboekhouden-mcp` as the current operating-system user over stdio.
2. The server reads its token from the host OS credential manager (Keyring) or a local `.env`.
3. The bundled client exchanges that token for a short-lived API session over HTTPS.
4. Mutating actions require explicit confirmation and pass through a native OS biometric hardware prompt.
5. Tool results return to the same MCP client over stdio.

The selected AI client can see tool results, including accounting data. Its own
retention, model-training, workspace, and privacy controls remain outside this
project's boundary.

## Controls

| Risk | Control |
|---|---|
| Accidental write | Write tools are absent unless the operator enables them; every write also requires `confirm: true`. |
| Unattended write execution | Native OS biometric hardware gate (Windows Hello / Touch ID) presents an interactive modal before executing writes. |
| Plaintext secret exposure | Native OS Keyring support loads tokens from Windows Credential Manager or macOS Keychain. |
| Destructive action | MCP annotations identify destructive tools; server instructions require exact user confirmation. |
| Credential exfiltration | Only the official API host is accepted by default; custom hosts require an explicit override and HTTPS. |
| Credential leakage | Pydantic secret types redact config representations; errors omit request bodies and values. |
| Malformed tool input | Strict JSON schemas reject unknown fields, bad limits, invalid nested rows, and oversized batches before API access. |
| Session races | Session creation, refresh, and API requests are serialized; one expired-session retry is allowed. |
| New-IP login block | Autonomous IMAP `BODY.PEEK` email whitelisting bridge authorizes new egress IPs non-destructively without marking user emails as read. |
| Arbitrary file read | Archive delivery is disabled separately and resolves every file beneath one configured root. |
| Oversized attachment | Archive files are limited to 3 MiB and an allowlist of PDF/image extensions. |
| Protocol corruption | Application logs go to stderr; stdout is reserved for MCP JSON-RPC. |

## Native Biometric Hardware Gate

When write tools are enabled and `EBOEKHOUDEN_MCP_REQUIRE_BIOMETRIC=true` (default), all write actions trigger an interactive OS hardware confirmation challenge:
- **Windows**: Windows Hello via `[Windows.Security.Credentials.UI.UserConsentVerifier]`. Requires Face recognition, Fingerprint scan, or PIN.
- **macOS**: `LocalAuthentication` / Touch ID.
- **Linux / Unsupported**: Fails closed (verification denied).

The modal dialog dynamically formats the transaction type and financial amounts:
- *Invoices*: `"e-Boekhouden MCP: Approve POST Invoice for EUR 1,250.00 to Relation 42?"`
- *Mutations*: `"e-Boekhouden MCP: Approve POST Mutation of EUR 540.00 on Ledger 8000?"`
- *Deletions*: `"e-Boekhouden MCP: Approve DELETE delete_relation for ID 123?"`

## Keyring Credential Store

To avoid storing credentials in plaintext `.env` files:
```bash
python -c "import keyring; keyring.set_password('eboekhouden_mcp', 'secret_token', '<your-token>')"
```
The server checks `keyring.get_password('eboekhouden_mcp', 'secret_token')` when `EBOEKHOUDEN_MCP_SECRET_TOKEN` is unset.

## Autonomous Web Session IP Whitelisting & Proxy Controls

e-Boekhouden enforces a 30-day server-side IP allowlist for web logins (`https://secure20.e-boekhouden.nl`). When a login occurs from an unrecognized IP address, e-Boekhouden blocks immediate login and sends a confirmation link by email.

When `EBOEKHOUDEN_MCP_AUTO_WHITELIST_IP=true` (default), `WebSessionClient` handles this challenge automatically (see [ADR 0002](adr/ADR_0002_Web_Session_IP_Whitelist_Workaround.md)):
1. **Read-Only IMAP Inspection (`BODY.PEEK`)**: The server connects to the configured IMAP inbox (`readonly=True`, 15-second socket timeout) and inspects recent e-Boekhouden confirmation emails using `BODY.PEEK[]`. This guarantees the user's inbox unread status (`\Seen` flag) is never modified.
2. **Credential Resolution**: IMAP credentials resolve from `EBOEKHOUDEN_MCP_IMAP_*` environment variables, `eboekhouden_mcp` Keyring entries (`--set-imap-credentials`), or existing `grobler_gmail` macOS Keychain items (`email` / `app_password`).
3. **Recursion Guard**: Only one automatic whitelist attempt is permitted per login flow (`_allow_auto_whitelist=False` on retry).
4. **Optional Static Egress Proxy**: Operators can set `EBOEKHOUDEN_MCP_WEB_PROXY` (e.g. `socks5://127.0.0.1:1080`) to route all web session requests through a fixed static exit node.

## Operator responsibilities

- Use a token for the intended administration and revoke tokens no longer used.
- Keep `.env` outside repositories, backups, chat messages, and shared folders.
- Keep the operating system, Python runtime, MCP client, and this package current.
- Review tool arguments and output before approving accounting changes.
- Restrict access to the local user account that launches the MCP server.
- Do not expose this stdio server through an unauthenticated network wrapper.

## Custom API hosts

`EBOEKHOUDEN_MCP_ALLOW_CUSTOM_API_URL=true` disables an important credential
destination guard. Use it only for a trusted HTTPS proxy or test endpoint you
operate and have reviewed. URLs containing embedded usernames or passwords are
always rejected.

## Incident response

If a token may have been exposed:

1. Revoke it in e-Boekhouden immediately.
2. Issue a replacement with the minimum access needed.
3. Remove the old value from MCP configuration, shell history, logs, backups,
   and any AI conversation where it appeared.
4. Review recent accounting activity for unexpected changes.
5. Treat a committed secret as compromised even after the commit is rewritten.

For a vulnerability in this project, follow [SECURITY.md](../SECURITY.md) and
use a private GitHub security advisory.
