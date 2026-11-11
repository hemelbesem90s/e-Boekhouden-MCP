"""Autonomous IMAP email poller for e-Boekhouden IP verification and whitelisting.

When e-Boekhouden detects a login attempt from an unverified IP address,
it sends an email containing a confirmation link to the account's registered address.
This module retrieves that email non-destructively via IMAP (BODY.PEEK), extracts the
verification code or approval URL, and authorizes the new IP automatically.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import email
import email.utils
import imaplib
import json
import logging
import os
import re
import sys
import time
from email.header import decode_header
from pathlib import Path

logger = logging.getLogger(__name__)

# Patterns matching e-Boekhouden login whitelist links
WHITELIST_URL_PATTERNS = (
    re.compile(
        r"https?://(?:secure20|secure)\.e-boekhouden\.nl/overig/login-whitelist\?[^\s\"\'<>]+",
        re.IGNORECASE,
    ),
    re.compile(
        r"https?://[^\s\"\'<>]+/overig/login-whitelist\?[^\s\"\'<>]*code=[A-Za-z0-9_\-]+",
        re.IGNORECASE,
    ),
)
WHITELIST_CODE_PATTERN = re.compile(
    r"[?&]code=([A-Za-z0-9_\-]+)",
    re.IGNORECASE,
)


def _decode_header_str(val: str | None) -> str:
    """Decode an RFC 2047 MIME header value to plain string."""
    if not val:
        return ""
    try:
        parts = decode_header(val)
        result: list[str] = []
        for text, charset in parts:
            if isinstance(text, bytes):
                result.append(text.decode(charset or "utf-8", errors="ignore"))
            else:
                result.append(str(text))
        return "".join(result)
    except Exception:
        return str(val)


def extract_whitelist_url_or_code(text: str) -> str | None:
    """Extract an e-Boekhouden whitelist approval URL or code from plain text or HTML."""
    if not text:
        return None

    # 1. Match full e-Boekhouden whitelist URL
    for pattern in WHITELIST_URL_PATTERNS:
        match = pattern.search(text)
        if match:
            raw_url = match.group(0).rstrip(".,;)>'\"")
            return raw_url

    # 2. Match direct ?code= query parameter in any URL fragment
    code_match = WHITELIST_CODE_PATTERN.search(text)
    if code_match:
        return code_match.group(1).strip()

    return None


def lookup_imap_credentials() -> tuple[str, str, str] | None:
    """Look up IMAP server, username, and password from environment or OS Keychain.

    Resolution precedence:
      1. Explicit environment variables:
         EBOEKHOUDEN_MCP_IMAP_SERVER, EBOEKHOUDEN_MCP_IMAP_USERNAME, EBOEKHOUDEN_MCP_IMAP_PASSWORD
      2. OS Keychain under service 'grobler_gmail' (email, app_password)
      3. Global config file: ~/.gemini/config/gmail_config.json
    """
    env_server = os.environ.get("EBOEKHOUDEN_MCP_IMAP_SERVER")
    env_user = os.environ.get("EBOEKHOUDEN_MCP_IMAP_USERNAME") or os.environ.get(
        "EBOEKHOUDEN_MCP_IMAP_EMAIL"
    )
    env_pass = os.environ.get("EBOEKHOUDEN_MCP_IMAP_PASSWORD")

    if env_user and env_pass:
        return (
            (env_server or "imap.gmail.com").strip(),
            env_user.strip(),
            env_pass.strip(),
        )

    # Check OS Keychain (service='grobler_gmail')
    try:
        import keyring

        email_addr = keyring.get_password("grobler_gmail", "email")
        app_pw = None
        if email_addr:
            app_pw = keyring.get_password("grobler_gmail", email_addr)
        if not app_pw:
            app_pw = keyring.get_password("grobler_gmail", "app_password")

        imap_srv = (
            keyring.get_password("grobler_gmail", "imap_server") or "imap.gmail.com"
        )

        if email_addr and app_pw:
            return (
                imap_srv.strip(),
                email_addr.strip(),
                app_pw.strip(),
            )

        # Check raw config blob stored in keyring
        for key in ("config", "gmail_config"):
            raw_cfg = keyring.get_password("grobler_gmail", key)
            if raw_cfg:
                parsed = json.loads(raw_cfg)
                gmail_section = (
                    parsed.get("gmail", {}) if isinstance(parsed, dict) else {}
                )
                if gmail_section.get("email") and gmail_section.get("app_password"):
                    return (
                        gmail_section.get("imap_server", "imap.gmail.com").strip(),
                        gmail_section["email"].strip(),
                        gmail_section["app_password"].strip(),
                    )
    except Exception as exc:
        logger.debug("Keyring IMAP credential lookup failed: %s", exc)

    # Check global config files
    candidates = [
        Path.home() / ".gemini" / "config" / "gmail_config.json",
        (Path(r"C:\LAB") if sys.platform == "win32" else Path.home() / "LAB")
        / "26031I_GRBL_Email_Synthesizer"
        / "config.json",
    ]
    for cfg_path in candidates:
        if cfg_path.is_file():
            try:
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
                gmail_section = data.get("gmail", {}) if isinstance(data, dict) else {}
                if gmail_section.get("email") and gmail_section.get("app_password"):
                    return (
                        gmail_section.get("imap_server", "imap.gmail.com").strip(),
                        gmail_section["email"].strip(),
                        gmail_section["app_password"].strip(),
                    )
            except Exception as exc:
                logger.debug("Failed reading %s: %s", cfg_path, exc)

    return None


def extract_message_body(msg: email.message.Message) -> str:
    """Extract plain text and/or HTML parts from an email.message.Message object."""
    body_parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            cdisp = str(part.get("Content-Disposition", ""))
            if ctype in ("text/plain", "text/html") and "attachment" not in cdisp:
                payload = part.get_payload(decode=True)
                if isinstance(payload, (bytes, bytearray)):
                    charset = part.get_content_charset() or "utf-8"
                    body_parts.append(payload.decode(charset, errors="ignore"))
    else:
        payload = msg.get_payload(decode=True)
        if isinstance(payload, (bytes, bytearray)):
            charset = msg.get_content_charset() or "utf-8"
            body_parts.append(payload.decode(charset, errors="ignore"))
    return "\n".join(body_parts)


def poll_for_whitelist_code(
    *,
    imap_server: str | None = None,
    username: str | None = None,
    password: str | None = None,
    timeout_seconds: float = 35.0,
    poll_interval: float = 3.0,
    lookback_seconds: int = 180,
    folder: str = "INBOX",
) -> str | None:
    """Poll IMAP non-destructively (BODY.PEEK) for e-Boekhouden IP verification links.

    Preserves the UNSEEN (unread) status of all mailbox messages.

    Args:
        imap_server: IMAP hostname (defaults to lookup_imap_credentials).
        username: IMAP username / email (defaults to lookup_imap_credentials).
        password: IMAP password / app password (defaults to lookup_imap_credentials).
        timeout_seconds: Total time to wait for the verification email to arrive.
        poll_interval: Seconds between successive mailbox checks.
        lookback_seconds: Max age of an acceptable email (default: 3 minutes).
        folder: Mailbox folder to inspect (default: 'INBOX').

    Returns:
        The extracted verification URL or approval code string, or None if timed out.
    """
    creds = lookup_imap_credentials()
    srv = imap_server or (creds[0] if creds else None) or "imap.gmail.com"
    usr = username or (creds[1] if creds else None)
    pwd = password or (creds[2] if creds else None)

    if not usr or not pwd:
        logger.warning(
            "Auto-whitelist aborted: No IMAP credentials configured (env or Keychain)."
        )
        return None

    deadline = time.time() + max(1.0, timeout_seconds)
    now_utc = dt.datetime.now(dt.timezone.utc)
    earliest_allowed = now_utc - dt.timedelta(seconds=lookback_seconds)

    mail: imaplib.IMAP4_SSL | None = None
    try:
        mail = imaplib.IMAP4_SSL(srv, timeout=15)
        mail.login(usr, pwd)

        status, _ = mail.select(folder, readonly=True)
        if status != "OK":
            logger.warning("Could not select IMAP folder '%s' in readonly mode", folder)
            return None

        while time.time() < deadline:
            # Search recent messages
            # Note: Fetching the highest UID/message numbers gives the newest incoming mail
            status, search_data = mail.search(None, "ALL")
            if status == "OK" and search_data and search_data[0]:
                msg_ids = search_data[0].split()
                # Inspect up to the 10 most recent messages
                recent_ids = msg_ids[-10:] if len(msg_ids) > 10 else msg_ids
                for mid in reversed(recent_ids):
                    # Fetch headers only using BODY.PEEK to keep message unread
                    res, mdata = mail.fetch(
                        mid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
                    )
                    if res != "OK" or not mdata or not isinstance(mdata[0], tuple):
                        continue

                    header_bytes = mdata[0][1]
                    if not isinstance(header_bytes, (bytes, bytearray)):
                        continue
                    header_msg = email.message_from_bytes(header_bytes)

                    from_hdr = _decode_header_str(header_msg.get("From", "")).lower()
                    subj_hdr = _decode_header_str(header_msg.get("Subject", "")).lower()
                    date_hdr = header_msg.get("Date", "")

                    # Verify message timestamp is recent
                    if date_hdr:
                        try:
                            msg_dt = email.utils.parsedate_to_datetime(date_hdr)
                            if (
                                msg_dt.tzinfo is None
                                or msg_dt.tzinfo.utcoffset(msg_dt) is None
                            ):
                                msg_dt = msg_dt.replace(tzinfo=dt.timezone.utc)
                            if msg_dt < earliest_allowed:
                                # Older than lookback window, skip
                                continue
                        except Exception:  # nosec B110
                            pass

                    # Check if sender or subject matches e-Boekhouden authorization
                    is_candidate = any(
                        s in from_hdr
                        for s in ("e-boekhouden", "skillsource", "noreply")
                    ) or any(
                        k in subj_hdr
                        for k in (
                            "whitelist",
                            "ip-adres",
                            "inloggen",
                            "verificatie",
                            "bevestig",
                            "locatie",
                        )
                    )

                    if not is_candidate:
                        continue

                    # Fetch full message body with BODY.PEEK
                    res_body, bdata = mail.fetch(mid, "(BODY.PEEK[])")
                    if res_body != "OK" or not bdata or not isinstance(bdata[0], tuple):
                        continue

                    raw_body_bytes = bdata[0][1]
                    if not isinstance(raw_body_bytes, (bytes, bytearray)):
                        continue
                    full_msg = email.message_from_bytes(raw_body_bytes)
                    body_text = extract_message_body(full_msg)

                    code_or_url = extract_whitelist_url_or_code(body_text)
                    if code_or_url:
                        logger.info(
                            "Found e-Boekhouden IP verification URL/code in recent email: %s",
                            code_or_url,
                        )
                        return code_or_url

            remaining = deadline - time.time()
            if remaining <= 0:
                break
            time.sleep(min(poll_interval, remaining))

    except Exception as exc:
        logger.warning("Error during IMAP IP whitelist polling: %s", exc)
        return None
    finally:
        if mail is not None:
            try:
                mail.close()
            except Exception:  # nosec B110
                pass
            try:
                mail.logout()
            except Exception:  # nosec B110
                pass

    return None


async def async_poll_for_whitelist_code(
    *,
    imap_server: str | None = None,
    username: str | None = None,
    password: str | None = None,
    timeout_seconds: float = 35.0,
    poll_interval: float = 3.0,
    lookback_seconds: int = 180,
    folder: str = "INBOX",
) -> str | None:
    """Async wrapper for poll_for_whitelist_code executed in a worker thread."""
    return await asyncio.to_thread(
        poll_for_whitelist_code,
        imap_server=imap_server,
        username=username,
        password=password,
        timeout_seconds=timeout_seconds,
        poll_interval=poll_interval,
        lookback_seconds=lookback_seconds,
        folder=folder,
    )
