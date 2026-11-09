"""Modular web session client for e-Boekhouden using cookie-based authentication."""

from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import hmac
import logging
import re
import secrets
import struct
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)


def generate_totp_code(
    secret: str,
    *,
    timestamp: float | None = None,
    time_step: int = 30,
    digits: int = 6,
) -> str:
    """Generate a 6-digit RFC 6238 TOTP code from a Base32 secret."""
    normalized = "".join(secret.strip().split()).upper().rstrip("=")
    if not normalized:
        raise ValueError("TOTP secret must not be empty")
    padding = "=" * (-len(normalized) % 8)
    key = base64.b32decode(normalized + padding, casefold=True)
    counter = int((timestamp if timestamp is not None else time.time()) // time_step)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % (
        10**digits
    )
    return str(code_int).zfill(digits)


REVERSE_CHARGE_BTW_CODES = frozenset({"BI_EU_INK", "BU_EU_INK", "VERL_INK"})
CLOUD_PATH_PREFIXES = (
    "h:\\",
    "h:/",
    "g:\\",
    "g:/",
    "//",
    "\\\\",
)
CLOUD_PATH_SUBSTRINGS = (
    "/library/cloudstorage/",
    "/library/mobile documents/",
    "/volumes/google drive",
    "shared drives",
    "onedrive",
    "dropbox",
)


class WebSessionError(Exception):
    """Base exception for e-Boekhouden web session client errors."""


class SessionExpiredError(WebSessionError):
    """Raised when the e-Boekhouden web session token has expired or is invalid."""


class IPVerificationRequiredError(WebSessionError):
    """Raised when e-Boekhouden blocks web login due to an unrecognized IP address."""

    code: str = "IP_VERIFICATION_REQUIRED"

    def __init__(
        self,
        message: str = (
            "e-Boekhouden web login requires IP address authorization. "
            "A verification email with an approval link/code has been dispatched to your registered address. "
            "Please click the confirmation link in the email or approve via whitelist_web_session_ip(code)."
        ),
        *,
        verification_code: str | None = None,
        verification_url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.verification_code = verification_code
        self.verification_url = verification_url


class TwoFactorRequiredError(WebSessionError):
    """Raised when e-Boekhouden web login requires a 2FA verification code."""

    code: str = "TWO_FACTOR_REQUIRED"

    def __init__(
        self,
        message: str = (
            "e-Boekhouden web login requires a 2FA verification code (tweestapsverificatie). "
            "Provide the TOTP secret or pass two_factor_code to login()."
        ),
    ) -> None:
        super().__init__(message)


def _validate_local_file_path(file_path: str | Path) -> Path:
    """Enforce fail-closed local file access (reject cloud drives / UNC paths)."""
    raw_str = str(file_path).strip()
    lower_str = raw_str.lower()
    norm_slash = lower_str.replace("\\", "/")

    is_cloud_path = any(
        lower_str.startswith(prefix) for prefix in CLOUD_PATH_PREFIXES
    ) or any(sub in norm_slash for sub in CLOUD_PATH_SUBSTRINGS)

    if is_cloud_path:
        raise ValueError(
            f"Refusing to read binary file from cloud/network path '{raw_str}' "
            "(Cloud Storage Hydration Protection). "
            "Copy or stage the file in a local directory (e.g. /tmp, $TMPDIR, or C:\\Temp\\) first."
        )
    path = Path(raw_str)
    if not path.is_file():
        raise FileNotFoundError(f"Local archive upload file not found: {raw_str}")
    return path


def normalize_web_mutation_payload(
    payload: dict[str, Any], *, is_update: bool
) -> dict[str, Any]:
    """Sanitize a web mutation model payload to prevent MUT_026, MUT_112, MUT_117, and MutNr errors."""
    normalized = copy.deepcopy(payload)
    mutatie = normalized.get("mutatie")
    if not isinstance(mutatie, dict):
        return normalized

    if not is_update:
        if mutatie.get("nr") in (0, None, ""):
            mutatie.pop("nr", None)
        if not normalized.get("folderKoppelId"):
            normalized["folderKoppelId"] = -(secrets.randbelow(1_000_000_000) + 1)
    else:
        if not normalized.get("folderKoppelId") and mutatie.get("nr"):
            normalized["folderKoppelId"] = mutatie["nr"]

    soort = mutatie.get("soort")
    regels = normalized.get("mutatieRegels")
    if isinstance(regels, list):
        has_reverse_charge = False
        for row in regels:
            if not isinstance(row, dict):
                continue
            if row.get("kostenplaats") in (0, None, ""):
                row.pop("kostenplaats", None)
            if row.get("btwCode") in REVERSE_CHARGE_BTW_CODES:
                has_reverse_charge = True
            if soort in (3, 4):
                open_post = row.pop("openPostModel", None) or {}
                if not row.get("relatieId"):
                    rel_id = open_post.get("relatieId") or mutatie.get("relatieId")
                    if rel_id:
                        row["relatieId"] = rel_id
                if not row.get("factuur"):
                    factuur = open_post.get("factuur") or mutatie.get("factuur")
                    if factuur:
                        row["factuur"] = str(factuur)
                row.pop("kostenplaats", None)
        if has_reverse_charge:
            mutatie["inEx"] = "EX"

    return normalized


def parse_import_grid(raw_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Transform column-indexed grid data into structured row dictionaries."""
    data_rows = raw_data.get("data", [])
    fields = [
        "id",
        "datum",
        "rekening",
        "mutDatum",
        "mutBedrag",
        "mutOmschrijving",
        "mutFactuur",
        "grootboekId",
        "opmerking",
        "hasFiles",
        "verwerkFailureReason",
    ]

    rows: list[dict[str, Any]] = []
    for item in data_rows:
        row: dict[str, Any] = {}
        for i, field in enumerate(fields):
            if i < len(item):
                row[field] = item[i]
        rows.append(row)
    return rows


def parse_gridtable_rows(raw_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Transform any e-Boekhouden gridtable response into dictionaries using colMetadata."""
    if not isinstance(raw_data, dict):
        return []
    col_meta = raw_data.get("colMetadata", [])
    col_names = [
        str(col.get("name", f"col_{idx}"))
        for idx, col in enumerate(col_meta)
        if isinstance(col, dict)
    ]
    data_rows = raw_data.get("data", [])
    rows: list[dict[str, Any]] = []
    for item in data_rows:
        if not isinstance(item, list):
            continue
        row: dict[str, Any] = {}
        for idx, name in enumerate(col_names):
            if idx < len(item):
                row[name] = item[idx]
        rows.append(row)
    return rows


def _vat_rate_for_code(btw_code: str | None) -> float:
    """Resolve VAT rate multiplier from an e-Boekhouden BTW code."""
    if not btw_code:
        return 0.0
    code = btw_code.upper()
    if "21" in code:
        return 0.21
    if "9" in code:
        return 0.09
    return 0.0


def summarize_time_entries(
    rows: list[dict[str, Any]],
    projects_by_id: dict[int, dict[str, Any]] | None = None,
    activities_by_name: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compute financial and hour/km totals and per-project WIP breakdown for time entries."""
    proj_map = projects_by_id or {}
    act_map = activities_by_name or {}

    total_hours = 0.0
    total_km = 0.0
    total_excl = 0.0
    total_vat = 0.0
    by_project: dict[str, dict[str, Any]] = {}

    for row in rows:
        hrs = float(row.get("aantalUren") or 0.0)
        hourly_rate = float(row.get("uurtarief") or 0.0)
        km = float(row.get("aantalKilometers") or 0.0)
        km_rate = float(row.get("kilometertarief") or 0.0)
        excl = round(hrs * hourly_rate + km * km_rate, 2)

        act_name = str(row.get("activiteit") or "")
        act_info = act_map.get(act_name, {})
        btw_code = act_info.get("btwCode")
        if not btw_code:
            proj_label = str(row.get("project") or "")
            if any(tag in act_name or tag in proj_label for tag in ("BCKE", "_EU")):
                btw_code = "BI_EU_VERK_D"
            else:
                btw_code = "HOOG_VERK_21"
        vat_rate = _vat_rate_for_code(btw_code)
        vat_amt = round(excl * vat_rate, 2)
        incl = round(excl + vat_amt, 2)

        row["amountExcl"] = excl
        row["vatCode"] = btw_code
        row["vatAmount"] = vat_amt
        row["amountIncl"] = incl

        proj_id = int(row.get("projectId") or 0)
        proj_name = str(row.get("project") or f"project_{proj_id}")
        p_meta = proj_map.get(proj_id, {})
        if p_meta.get("relatieId") and "relatieId" not in row:
            row["relatieId"] = p_meta["relatieId"]
        if p_meta.get("relatie") and "relatie" not in row:
            row["relatie"] = p_meta["relatie"]

        date_str = str(row.get("datum") or "")[:10]
        bucket = by_project.get(proj_name)
        if bucket is None:
            bucket = {
                "project_id": proj_id,
                "project": proj_name,
                "relation_id": p_meta.get("relatieId"),
                "client": p_meta.get("relatie"),
                "active": p_meta.get("actief"),
                "entry_count": 0,
                "hours": 0.0,
                "kilometers": 0.0,
                "total_excl_vat": 0.0,
                "total_vat": 0.0,
                "total_incl_vat": 0.0,
                "vat_code": btw_code,
                "date_start": date_str or None,
                "date_end": date_str or None,
            }
            by_project[proj_name] = bucket

        bucket["entry_count"] += 1
        bucket["hours"] = round(bucket["hours"] + hrs, 2)
        bucket["kilometers"] = round(bucket["kilometers"] + km, 2)
        bucket["total_excl_vat"] = round(bucket["total_excl_vat"] + excl, 2)
        bucket["total_vat"] = round(bucket["total_vat"] + vat_amt, 2)
        bucket["total_incl_vat"] = round(bucket["total_incl_vat"] + incl, 2)
        if date_str:
            if not bucket["date_start"] or date_str < bucket["date_start"]:
                bucket["date_start"] = date_str
            if not bucket["date_end"] or date_str > bucket["date_end"]:
                bucket["date_end"] = date_str

        total_hours = round(total_hours + hrs, 2)
        total_km = round(total_km + km, 2)
        total_excl = round(total_excl + excl, 2)
        total_vat = round(total_vat + vat_amt, 2)

    return {
        "entry_count": len(rows),
        "total_hours": total_hours,
        "total_kilometers": total_km,
        "total_excl_vat": total_excl,
        "total_vat": total_vat,
        "total_incl_vat": round(total_excl + total_vat, 2),
        "by_project": sorted(
            by_project.values(), key=lambda x: x["total_excl_vat"], reverse=True
        ),
    }


def build_afschrift_params(
    id: int,
    rekening: str,
    mut_datum: str,
    mut_bedrag: float,
    mut_omschrijving: str,
    mut_factuur: str = "",
    grootboek_id: int = 0,
) -> dict[str, str]:
    """Build query parameters for the /v1/api/mutatie/getmutatiebyafschrift endpoint."""
    mut_soort = "4" if mut_bedrag < 0 else "3"
    return {
        "id": str(id),
        "rekening": str(rekening),
        "mutDatum": str(mut_datum),
        "mutBedrag": f"{mut_bedrag:.2f}",
        "mutOmschrijving": str(mut_omschrijving),
        "mutFactuur": str(mut_factuur),
        "grootboekId": str(grootboek_id),
        "mutSoort": mut_soort,
    }


def parse_eb_date_dict(
    date_val: str | dict[str, Any] | dt.date | None,
    *,
    base_date: dict[str, int] | None = None,
    default_offset_days: int = 0,
) -> dict[str, int]:
    """Convert a date string (YYYY-MM-DD or DD-MM-YYYY), date, or dict to e-Boekhouden's {year, month, day} dict."""
    if isinstance(date_val, dict) and all(
        k in date_val for k in ("year", "month", "day")
    ):
        return {
            "year": int(date_val["year"]),
            "month": int(date_val["month"]),
            "day": int(date_val["day"]),
        }
    if isinstance(date_val, (dt.date, dt.datetime)):
        return {
            "year": int(date_val.year),
            "month": int(date_val.month),
            "day": int(date_val.day),
        }
    if isinstance(date_val, str) and date_val.strip():
        raw = date_val.strip()[:10]
        parts = raw.split("-")
        if len(parts) == 3:
            if len(parts[0]) == 4:
                return {
                    "year": int(parts[0]),
                    "month": int(parts[1]),
                    "day": int(parts[2]),
                }
            return {
                "year": int(parts[2]),
                "month": int(parts[1]),
                "day": int(parts[0]),
            }
        raise ValueError(
            f"Invalid date format '{date_val}'. Expected YYYY-MM-DD or DD-MM-YYYY."
        )

    if base_date:
        anchor = dt.date(
            int(base_date["year"]), int(base_date["month"]), int(base_date["day"])
        )
    else:
        anchor = dt.date.today()
    target = anchor + dt.timedelta(days=default_offset_days)
    return {
        "year": int(target.year),
        "month": int(target.month),
        "day": int(target.day),
    }


def format_eb_date_dict(date_val: Any) -> str | None:
    """Format an e-Boekhouden {year, month, day} dict or ISO string to YYYY-MM-DD."""
    if isinstance(date_val, dict) and all(
        k in date_val for k in ("year", "month", "day")
    ):
        return f"{int(date_val['year']):04d}-{int(date_val['month']):02d}-{int(date_val['day']):02d}"
    if isinstance(date_val, str) and date_val.strip():
        return date_val.strip()[:10]
    return None


def resolve_quote_status_id(
    statuses: list[dict[str, Any]],
    status_id: int | None = None,
    status: str | None = None,
) -> int:
    """Resolve an e-Boekhouden offertestatus ID by integer ID, name/substring, or default to first status."""
    if status_id is not None:
        return int(status_id)
    if status and status.strip():
        needle = status.strip().lower()
        for s in statuses:
            label = str(s.get("status") or "").lower()
            if needle == label or needle in label:
                return int(s["id"])
        raise ValueError(
            f"Unknown quote status '{status}'. Available statuses: "
            f"{[s.get('status') for s in statuses]}"
        )
    if statuses and statuses[0].get("id") is not None:
        return int(statuses[0]["id"])
    raise WebSessionError("No quote statuses (offertestatus) found in e-Boekhouden.")


def resolve_revenue_ledger_id(
    ledgers: list[dict[str, Any]],
    ledger_id: int | None = None,
    vat_code: str = "HOOG_VERK_21",
) -> int:
    """Resolve a revenue ledger ID by internal ID, ledger code (e.g. 810200), or VAT code."""
    if ledger_id is not None:
        lid = int(ledger_id)
        for gbr in ledgers:
            if int(gbr.get("id") or 0) == lid:
                return lid
            code_str = str(gbr.get("code") or "").strip().lstrip("0")
            if code_str and code_str == str(lid).lstrip("0"):
                return int(gbr["id"])
        return lid

    vc = (vat_code or "HOOG_VERK_21").upper()
    preferred_codes: tuple[str, ...]
    if "BI_EU" in vc:
        preferred_codes = ("810300", "8000", "810200")
    elif "BU_EU" in vc:
        preferred_codes = ("810400", "8000", "810200")
    elif "LAAG" in vc or "9" in vc:
        preferred_codes = ("810100", "8000", "810200")
    elif "VERL" in vc:
        preferred_codes = ("810700", "8000", "810200")
    elif "VRIJ" in vc:
        preferred_codes = ("810600", "8000", "810200")
    elif "GEEN" in vc or "NUL" in vc:
        preferred_codes = ("810500", "8000", "810200")
    else:
        preferred_codes = ("810200", "8000", "801000")

    for target_code in preferred_codes:
        for gbr in ledgers:
            code_str = str(gbr.get("code") or "").strip()
            if code_str == target_code or code_str.lstrip("0") == target_code.lstrip(
                "0"
            ):
                return int(gbr["id"])

    for gbr in ledgers:
        if str(gbr.get("code") or "").strip().startswith("8"):
            return int(gbr["id"])
    if ledgers and ledgers[0].get("id") is not None:
        return int(ledgers[0]["id"])
    raise WebSessionError("Could not resolve a default revenue ledger (grootboekId).")


def build_quote_line_payload(
    item: dict[str, Any],
    *,
    quote_id: int,
    in_ex_vat: str = "EX",
    volgorde: int = 1,
    ledgers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Construct an e-Boekhouden offerteregel payload with automatic VAT price calculation."""
    vat_code = str(item.get("vat_code") or item.get("btwCode") or "HOOG_VERK_21")
    vat_rate = _vat_rate_for_code(vat_code)
    raw_qty = (
        item["quantity"]
        if item.get("quantity") is not None
        else (item["aantal"] if item.get("aantal") is not None else 1.0)
    )
    quantity = float(raw_qty)
    raw_unit_price = (
        item["price_per_unit"]
        if item.get("price_per_unit") is not None
        else (item["prijsExcl"] if item.get("prijsExcl") is not None else 0.0)
    )
    raw_price = float(raw_unit_price)
    is_incl = str(in_ex_vat or "EX").upper() == "IN"
    if is_incl:
        prijs_incl = round(raw_price, 2)
        prijs_excl = round(raw_price / (1.0 + vat_rate), 2) if vat_rate else prijs_incl
    else:
        prijs_excl = round(raw_price, 2)
        prijs_incl = round(raw_price * (1.0 + vat_rate), 2)

    raw_ledger = item.get("ledger_id") or item.get("grootboekId")
    ledger_id = resolve_revenue_ledger_id(
        ledgers or [],
        ledger_id=int(raw_ledger) if raw_ledger is not None else None,
        vat_code=vat_code,
    )

    payload: dict[str, Any] = {
        "id": int(item.get("id") or 0),
        "offerteId": int(quote_id),
        "aantal": quantity,
        "volgorde": int(item.get("volgorde") or volgorde),
        "omschrijving": str(item.get("description") or item.get("omschrijving") or ""),
        "prijsExcl": prijs_excl,
        "prijsIncl": prijs_incl,
        "btwCode": vat_code,
        "grootboekId": ledger_id,
        "kortingIsPerc": bool(
            item.get("discount_is_percentage", item.get("kortingIsPerc", True))
        ),
    }
    raw_unit_id = item.get("unit_id") or item.get("eenheidId")
    if raw_unit_id is not None:
        payload["eenheidId"] = int(raw_unit_id)
    if item.get("code"):
        payload["code"] = str(item["code"])[:20]
    raw_product_id = item.get("product_id") or item.get("artikelId")
    if raw_product_id is not None:
        payload["artikelId"] = int(raw_product_id)
    raw_cc_id = item.get("cost_center_id") or item.get("kostenplaatsId")
    if raw_cc_id is not None:
        payload["kostenplaatsId"] = int(raw_cc_id)
    discount = (
        item.get("discount")
        if item.get("discount") is not None
        else item.get("korting")
    )
    if discount is not None and float(discount) != 0.0:
        payload["korting"] = float(discount)
    return payload


def _extract_v1_error_message(html_text: str) -> str | None:
    """Extract explicit error message from classic V1 HTML response (<span id="ERR_MSG">)."""
    m = re.search(
        r'<span id=["\']ERR_MSG["\'][^>]*>(.*?)</span>',
        html_text,
        re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return None
    raw = m.group(1)
    cleaned = re.sub(r"<[^>]+>", " ", raw)
    text = " ".join(cleaned.split()).strip()
    return text if text else None


class _SyncAutoAuthClient:
    """Thin sync wrapper around httpx.Client that auto-authenticates on SessionExpiredError."""

    def __init__(self, owner: "WebSessionClient", inner: httpx.Client) -> None:
        self._owner = owner
        self._inner = inner

    def _call(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if not self._owner.auth_token and self._owner.has_login_credentials():
            self._owner.login(persist_to_keyring=self._owner.persist_to_keyring)
        kwargs["headers"] = self._owner._get_headers()
        resp = getattr(self._inner, method)(url, **kwargs)
        if (
            self._owner._is_session_expired_response(resp)
            and self._owner.has_login_credentials()
        ):
            self._owner.login(persist_to_keyring=self._owner.persist_to_keyring)
            kwargs["headers"] = self._owner._get_headers()
            resp = getattr(self._inner, method)(url, **kwargs)
        return resp

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._call("get", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._call("post", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._call("put", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._call("delete", url, **kwargs)

    def close(self) -> None:
        self._inner.close()


class _AsyncAutoAuthClient:
    """Thin async wrapper around httpx.AsyncClient that auto-authenticates on SessionExpiredError."""

    def __init__(
        self,
        owner: "WebSessionClient",
        inner: httpx.AsyncClient,
        *,
        close_on_exit: bool,
    ) -> None:
        self._owner = owner
        self._inner = inner
        self._close_on_exit = close_on_exit

    async def __aenter__(self) -> "_AsyncAutoAuthClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._close_on_exit:
            await self._inner.aclose()

    async def _call(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if not self._owner.auth_token and self._owner.has_login_credentials():
            await self._owner.async_login(
                persist_to_keyring=self._owner.persist_to_keyring
            )
        kwargs["headers"] = self._owner._get_headers()
        resp = await getattr(self._inner, method)(url, **kwargs)
        if (
            self._owner._is_session_expired_response(resp)
            and self._owner.has_login_credentials()
        ):
            await self._owner.async_login(
                persist_to_keyring=self._owner.persist_to_keyring
            )
            kwargs["headers"] = self._owner._get_headers()
            resp = await getattr(self._inner, method)(url, **kwargs)
        return resp

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self._call("get", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self._call("post", url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self._call("put", url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self._call("delete", url, **kwargs)


class WebSessionClient:
    """Client for e-Boekhouden web session API (secure20 / secure)."""

    def __init__(
        self,
        auth_token: str | None = None,
        base_url: str = "https://secure20.e-boekhouden.nl",
        timeout: float = 30.0,
        http_client: httpx.Client | None = None,
        async_http_client: httpx.AsyncClient | None = None,
        username: str | None = None,
        password: str | None = None,
        totp_secret: str | None = None,
        keyring_service_name: str = "eboekhouden_mcp",
        persist_to_keyring: bool = False,
    ) -> None:
        self.auth_token = auth_token.strip() if auth_token else ""
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._http = http_client
        self._async_http = async_http_client
        self.username = username.strip() if username else ""
        self.password = password.strip() if password else ""
        self.totp_secret = totp_secret.strip() if totp_secret else ""
        self.keyring_service_name = keyring_service_name
        self.persist_to_keyring = persist_to_keyring

    def set_token(self, token: str) -> None:
        """Update the web session auth token."""
        self.auth_token = token.strip()

    def set_credentials(
        self,
        username: str | None = None,
        password: str | None = None,
        totp_secret: str | None = None,
    ) -> None:
        """Update the web session login credentials used for automatic token refresh."""
        if username is not None:
            self.username = username.strip()
        if password is not None:
            self.password = password.strip()
        if totp_secret is not None:
            self.totp_secret = totp_secret.strip()

    def has_login_credentials(self) -> bool:
        """Return True if both username and password are available for auto-login."""
        return bool(self.username and self.password)

    def _build_login_payload(
        self,
        username: str | None = None,
        password: str | None = None,
        two_factor_code: str | None = None,
    ) -> dict[str, str]:
        resolved_user = (username if username is not None else self.username).strip()
        resolved_pass = (password if password is not None else self.password).strip()
        if not resolved_user or not resolved_pass:
            raise WebSessionError(
                "Web session login requires both username and password. "
                "Store them in keyring via --set-web-credentials or set "
                "EBOEKHOUDEN_MCP_WEB_USERNAME and EBOEKHOUDEN_MCP_WEB_PASSWORD."
            )
        resolved_code = ""
        if two_factor_code is not None:
            resolved_code = two_factor_code.strip()
        elif self.totp_secret:
            resolved_code = generate_totp_code(self.totp_secret)
        return {
            "username": resolved_user,
            "password": resolved_pass,
            "twoFactorCode": resolved_code,
        }

    def _v1_login_url(self) -> str:
        """Resolve the classic V1 login endpoint URL (e.g. https://secure.e-boekhouden.nl/bh/inloggen.asp)."""
        if "secure20." in self.base_url:
            v1_base = self.base_url.replace("secure20.", "secure.")
        elif "accept20." in self.base_url:
            v1_base = self.base_url.replace("accept20.", "accept.")
        else:
            v1_base = self.base_url
        return f"{v1_base.rstrip('/')}/bh/inloggen.asp"

    @staticmethod
    def _extract_v1_login_fields(
        html_text: str, response: httpx.Response
    ) -> tuple[str, str]:
        """Extract txtFormCheck CSRF token and SRVRID cookie from GET /bh/inloggen.asp."""
        m = re.search(
            r'name=["\']txtFormCheck["\']\s+value=["\']([^"\']+)["\']', html_text
        )
        if not m:
            m = re.search(
                r'value=["\']([^"\']+)["\']\s+name=["\']txtFormCheck["\']', html_text
            )
        form_check = m.group(1) if m else ""
        srvrid = response.cookies.get("SRVRID") or ""
        if not srvrid:
            for sc in response.headers.get_list("set-cookie"):
                sm = re.search(r"SRVRID=([^;\s]+)", sc)
                if sm:
                    srvrid = sm.group(1)
                    break
        return form_check, srvrid

    @staticmethod
    def _extract_auth_token_from_response(response: httpx.Response) -> str | None:
        """Extract auth-token JWT from Set-Cookie headers, response cookies, or JSON body."""
        all_resps = list(getattr(response, "history", []) or []) + [response]
        for r in reversed(all_resps):
            try:
                tok = r.cookies.get("auth-token")
                if tok and tok.strip():
                    return tok.strip()
            except Exception:  # nosec B110
                pass
            for sc in r.headers.get_list("set-cookie"):
                m = re.search(r"auth-token=([^;\s]+)", sc)
                if m and m.group(1).strip():
                    return m.group(1).strip()
        if response.status_code in (200, 201):
            try:
                data = response.json()
                if isinstance(data, dict) and isinstance(data.get("token"), str):
                    tok = data["token"].strip()
                    if tok:
                        return tok
            except Exception:  # nosec B110
                pass
        return None

    def _persist_token(self, token: str, *, persist_to_keyring: bool) -> str:
        clean_token = token.strip()
        self.set_token(clean_token)
        if persist_to_keyring:
            try:
                from eboekhouden_mcp.config import set_keyring_secret

                set_keyring_secret(
                    self.keyring_service_name, "web_session_token", clean_token
                )
            except Exception:  # nosec B110
                pass
        return clean_token

    def _parse_login_response(
        self, response: httpx.Response, *, persist_to_keyring: bool
    ) -> str:
        cookie_or_json_token = self._extract_auth_token_from_response(response)
        if cookie_or_json_token:
            return self._persist_token(
                cookie_or_json_token, persist_to_keyring=persist_to_keyring
            )

        body = response.text
        lower_body = body.lower()

        # 1. Check for lockout
        if "lockout" in lower_body:
            mins_val = None
            if response.status_code not in (200, 201):
                try:
                    err_data = response.json()
                    if (
                        isinstance(err_data, dict)
                        and err_data.get("errorType") == "Lockout"
                    ):
                        mins_val = err_data.get("lockoutMinutes")
                except Exception:  # nosec B110
                    pass
            if mins_val is None:
                m_lock = re.search(r"(\d+)\s*(?:minuten|minute)", body, re.IGNORECASE)
                mins_val = m_lock.group(1) if m_lock else "15"
            raise WebSessionError(
                f"e-Boekhouden login locked out for {mins_val} minute(s)."
            )

        # 2. Check for explicit IP verification challenge in JSON responses
        json_data: dict[str, Any] | None = None
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                json_data = parsed
        except Exception:  # nosec B110
            pass

        if json_data is not None:
            err_type = str(json_data.get("errorType") or "").lower()
            msg = str(json_data.get("message") or "")
            code_val = str(json_data.get("code") or "").upper()
            if (
                err_type
                in (
                    "ip_verification",
                    "unauthorized_ip",
                    "ipverification",
                    "ipchallenge",
                    "ip_not_whitelisted",
                )
                or code_val
                in (
                    "IP_VERIFICATION_REQUIRED",
                    "NEW_IP",
                    "SECURITY_IP",
                    "IP_NOT_WHITELISTED",
                )
                or any(
                    k in msg.lower()
                    for k in (
                        "ip-adres",
                        "nieuw ip",
                        "onbekend ip",
                        "whitelist",
                        "verifieer uw ip",
                        "bevestig uw ip",
                    )
                )
                or json_data.get("requiresIpVerification") is True
                or json_data.get("whitelistRequired") is True
            ):
                raise IPVerificationRequiredError(
                    f"e-Boekhouden web login requires IP address authorization: {msg or 'unrecognized IP address'}. "
                    "A verification email with an approval link/code has been dispatched to your registered address. "
                    "Please click the verification link or approve the code via whitelist_web_session_ip(code)."
                )
            if (
                err_type in ("two_factor", "twofactor", "2fa", "tfa")
                or code_val in ("TWO_FACTOR_REQUIRED", "2FA_REQUIRED", "OTP_REQUIRED")
                or json_data.get("requiresTwoFactor") is True
            ):
                raise TwoFactorRequiredError(
                    f"e-Boekhouden web login requires 2FA verification: {msg or 'two-factor authentication required'}."
                )

        # 3. Check for explicit IP challenge in HTML / text responses
        v1_err = _extract_v1_error_message(body)
        ip_challenge_patterns = (
            "ip-adres",
            "onbekend ip",
            "nieuw ip",
            "login-whitelist",
            "bevestig uw ip",
            "verifieer uw ip",
            "ongeautoriseerd ip",
            "locatie die we nog niet kennen",
            "vanaf een locatie die we",
            "bevestigen dat jij het bent",
            "bevestigen dat u het bent",
        )
        if any(k in lower_body for k in ip_challenge_patterns) or (
            v1_err and any(k in v1_err.lower() for k in ip_challenge_patterns)
        ):
            err_detail = v1_err or "Nieuw of onbekend IP-adres/locatie gedetecteerd"
            raise IPVerificationRequiredError(
                f"e-Boekhouden web login blocked ({err_detail}). "
                "A verification email with an authorization link has been sent to your registered address. "
                "Approve the IP address by clicking the email link or passing the code to whitelist_web_session_ip(code)."
            )

        # 4. Check for 2FA in HTML
        if "tweestapsverificatie" in lower_body or "app-two-factor-form" in lower_body:
            raise TwoFactorRequiredError(
                "e-Boekhouden web login requires 2FA verification code (tweestapsverificatie)."
            )

        # 5. Handle non-200 responses
        if response.status_code not in (200, 201):
            if json_data is not None:
                err_text = str(
                    json_data.get("message")
                    or json_data.get("code")
                    or json_data.get("errorType")
                    or ""
                ).strip()
                if err_text:
                    if (
                        "onjuist" in err_text.lower()
                        or "gebruikersnaam" in err_text.lower()
                    ):
                        raise WebSessionError(
                            f"e-Boekhouden web login failed: {err_text} "
                            "(Note: If logging in from a new IP address or location for the first time in 30 days, "
                            "e-Boekhouden may block access and dispatch an email verification link to authorize this IP address. "
                            "Check your email for an IP confirmation message and approve the link or code before retrying.)"
                        )
                    raise WebSessionError(f"e-Boekhouden web login failed: {err_text}")
            raise WebSessionError(
                f"e-Boekhouden web login failed (HTTP {response.status_code}): {body[:200]}"
            )

        # 6. For 200/201 responses:
        # Check classic V1 ERR_MSG container (avoids false-positive matches on harmless footer links)
        if v1_err:
            if "onjuist" in v1_err.lower() or "gebruikersnaam" in v1_err.lower():
                raise WebSessionError(
                    f"e-Boekhouden web login failed: {v1_err}. "
                    "(Note: If logging in from a new IP address or location for the first time in 30 days, "
                    "e-Boekhouden may block access and dispatch an email verification link to authorize this IP address. "
                    "Check your email for an IP confirmation message and approve the link or code before retrying.)"
                )
            raise WebSessionError(f"e-Boekhouden web login failed: {v1_err}")

        # Check JSON response token
        if json_data is not None:
            token = json_data.get("token")
            if not isinstance(token, str) or not token.strip():
                raise WebSessionError(
                    "e-Boekhouden web login response did not contain a valid token."
                )
            return self._persist_token(token, persist_to_keyring=persist_to_keyring)

        # If HTML form returned without auth token
        if 'id="txtEmail"' in body or 'name="txtEmail"' in body:
            raise WebSessionError(
                "e-Boekhouden web login failed: login form returned without an authentication token. "
                "(Note: If logging in from a new IP address or location for the first time in 30 days, "
                "e-Boekhouden may block access and dispatch an email verification link to authorize this IP address. "
                "Check your email for an IP confirmation message.)"
            )

        raise WebSessionError(f"Failed to parse login response JSON: {body[:200]}")

    def _login_via_v1_session(
        self,
        client: httpx.Client,
        username: str,
        password: str,
        two_factor_code: str = "",
        *,
        persist_to_keyring: bool,
    ) -> str:
        """Authenticate via classic V1 hybrid login (GET + POST /bh/inloggen.asp) to mint auth-token JWT."""
        v1_url = self._v1_login_url()
        get_resp = client.get(
            v1_url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "User-Agent": self._get_headers()["user-agent"],
            },
        )
        form_check, srvrid = self._extract_v1_login_fields(get_resp.text, get_resp)
        form_data = {
            "txtEmail": username,
            "txtWachtwoord": password,
            "submit1": "Inloggen",
            "txtFormCheck": form_check,
            "FP": "2557579786",
            "XSRVX": srvrid,
        }
        if two_factor_code:
            form_data["txt2FA"] = two_factor_code
            form_data["txtCode"] = two_factor_code
        post_resp = client.post(
            f"{v1_url}?LOGIN=1",
            data=form_data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": v1_url,
                "User-Agent": self._get_headers()["user-agent"],
            },
        )
        return self._parse_login_response(
            post_resp, persist_to_keyring=persist_to_keyring
        )

    async def _async_login_via_v1_session(
        self,
        client: httpx.AsyncClient,
        username: str,
        password: str,
        two_factor_code: str = "",
        *,
        persist_to_keyring: bool,
    ) -> str:
        """Async version of classic V1 hybrid login (GET + POST /bh/inloggen.asp)."""
        v1_url = self._v1_login_url()
        get_resp = await client.get(
            v1_url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "User-Agent": self._get_headers()["user-agent"],
            },
        )
        form_check, srvrid = self._extract_v1_login_fields(get_resp.text, get_resp)
        form_data = {
            "txtEmail": username,
            "txtWachtwoord": password,
            "submit1": "Inloggen",
            "txtFormCheck": form_check,
            "FP": "2557579786",
            "XSRVX": srvrid,
        }
        if two_factor_code:
            form_data["txt2FA"] = two_factor_code
            form_data["txtCode"] = two_factor_code
        post_resp = await client.post(
            f"{v1_url}?LOGIN=1",
            data=form_data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": v1_url,
                "User-Agent": self._get_headers()["user-agent"],
            },
        )
        return self._parse_login_response(
            post_resp, persist_to_keyring=persist_to_keyring
        )

    def login(
        self,
        username: str | None = None,
        password: str | None = None,
        two_factor_code: str | None = None,
        *,
        persist_to_keyring: bool | None = None,
    ) -> str:
        """Authenticate against e-Boekhouden web session and update self.auth_token."""
        payload = self._build_login_payload(username, password, two_factor_code)
        should_persist = (
            self.persist_to_keyring
            if persist_to_keyring is None
            else bool(persist_to_keyring)
        )
        url = f"{self.base_url}/v1/api/auth/login"
        headers = {
            k: v
            for k, v in self._get_headers().items()
            if k not in ("Authorization", "Cookie")
        }
        if self._http is not None:
            resp = self._http.post(url, json=payload, headers=headers)
            if resp.status_code >= 500:
                return self._login_via_v1_session(
                    self._http,
                    payload["username"],
                    payload["password"],
                    payload.get("twoFactorCode", ""),
                    persist_to_keyring=should_persist,
                )
            return self._parse_login_response(resp, persist_to_keyring=should_persist)

        with httpx.Client(
            follow_redirects=True,
            timeout=self.timeout,
        ) as client:
            return self._login_via_v1_session(
                client,
                payload["username"],
                payload["password"],
                payload.get("twoFactorCode", ""),
                persist_to_keyring=should_persist,
            )

    async def async_login(
        self,
        username: str | None = None,
        password: str | None = None,
        two_factor_code: str | None = None,
        *,
        persist_to_keyring: bool | None = None,
    ) -> str:
        """Async version of e-Boekhouden web session login."""
        payload = self._build_login_payload(username, password, two_factor_code)
        should_persist = (
            self.persist_to_keyring
            if persist_to_keyring is None
            else bool(persist_to_keyring)
        )
        url = f"{self.base_url}/v1/api/auth/login"
        headers = {
            k: v
            for k, v in self._get_headers().items()
            if k not in ("Authorization", "Cookie")
        }
        if self._async_http is not None:
            resp = await self._async_http.post(url, json=payload, headers=headers)
            if resp.status_code >= 500:
                return await self._async_login_via_v1_session(
                    self._async_http,
                    payload["username"],
                    payload["password"],
                    payload.get("twoFactorCode", ""),
                    persist_to_keyring=should_persist,
                )
            return self._parse_login_response(resp, persist_to_keyring=should_persist)

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=self.timeout,
        ) as client:
            return await self._async_login_via_v1_session(
                client,
                payload["username"],
                payload["password"],
                payload.get("twoFactorCode", ""),
                persist_to_keyring=should_persist,
            )

    @staticmethod
    def _extract_whitelist_code(code_or_url: str) -> str:
        """Extract verification code from a raw code or confirmation URL."""
        raw = str(code_or_url).strip()
        if not raw:
            raise ValueError("Verification code or confirmation URL must not be empty.")
        m = re.search(r"[?&]code=([^&\s#]+)", raw)
        if m:
            return m.group(1).strip()
        return raw.strip("'\"")

    def _handle_whitelist_response(
        self, response: httpx.Response, code: str
    ) -> dict[str, Any]:
        """Parse response from POST /v1/api/loginwhitelist/whitelist."""
        body = response.text.strip().strip('"')
        if response.status_code in (200, 201):
            if "Success" in body:
                result: dict[str, Any] = {
                    "success": True,
                    "status": "Success",
                    "code": code,
                    "message": "Bedankt voor de bevestiging. Vanaf nu kun je inloggen vanaf dit IP-adres.",
                }
                if self.has_login_credentials():
                    try:
                        fresh_token = self.login(
                            persist_to_keyring=self.persist_to_keyring
                        )
                        result["auto_login"] = True
                        result["auth_token"] = fresh_token
                    except Exception as exc:
                        logger.warning("Auto-login after IP whitelist failed: %s", exc)
                        result["auto_login"] = False
                return result
            if "Already whitelisted" in body:
                result = {
                    "success": True,
                    "status": "Already whitelisted",
                    "code": code,
                    "message": "Je IP-adres is al toegevoegd aan de whitelist.",
                }
                if self.has_login_credentials():
                    try:
                        fresh_token = self.login(
                            persist_to_keyring=self.persist_to_keyring
                        )
                        result["auto_login"] = True
                        result["auth_token"] = fresh_token
                    except Exception as exc:
                        logger.warning("Auto-login after IP whitelist failed: %s", exc)
                        result["auto_login"] = False
                return result
            if "Code invalid" in body or "invalid" in body.lower():
                raise WebSessionError(
                    "The IP verification code is invalid or has expired. "
                    "Please initiate a new login or action to receive a fresh verification link from e-Boekhouden."
                )
            raise WebSessionError(f"IP whitelist failed: {body[:200]}")
        raise WebSessionError(
            f"IP whitelist request failed (HTTP {response.status_code}): {body[:200]}"
        )

    def whitelist_ip(self, code_or_url: str) -> dict[str, Any]:
        """Authorize the current IP address using the verification code or approval link sent by e-Boekhouden."""
        code = self._extract_whitelist_code(code_or_url)
        url = f"{self.base_url}/v1/api/loginwhitelist/whitelist"
        headers = {
            k: v
            for k, v in self._get_headers().items()
            if k not in ("Authorization", "Cookie")
        }
        if self._http is not None:
            resp = self._http.post(url, params={"code": code}, headers=headers)
            return self._handle_whitelist_response(resp, code)
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, params={"code": code}, headers=headers)
            return self._handle_whitelist_response(resp, code)

    async def async_whitelist_ip(self, code_or_url: str) -> dict[str, Any]:
        """Async version of whitelist_ip."""
        code = self._extract_whitelist_code(code_or_url)
        url = f"{self.base_url}/v1/api/loginwhitelist/whitelist"
        headers = {
            k: v
            for k, v in self._get_headers().items()
            if k not in ("Authorization", "Cookie")
        }
        if self._async_http is not None:
            resp = await self._async_http.post(
                url, params={"code": code}, headers=headers
            )
            return self._handle_whitelist_response(resp, code)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, params={"code": code}, headers=headers)
            return self._handle_whitelist_response(resp, code)

    def _get_headers(self) -> dict[str, str]:
        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "nl,en-GB;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "dnt": "1",
            "origin": self.base_url,
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
        }
        if self.auth_token:
            headers["Authorization"] = self.auth_token
            headers["Cookie"] = f"auth-token={self.auth_token}"
        return headers

    @staticmethod
    def _is_session_expired_response(response: httpx.Response) -> bool:
        """Return True if an HTTP response indicates an expired or unauthenticated session."""
        if response.status_code == 401:
            return True
        body = response.text
        if (
            "SECURITY_010" in body
            or "Niet ingelogd" in body
            or '"errorType":"security"' in body
        ):
            return True
        if response.status_code in (200, 201) and body.strip():
            try:
                data = response.json()
                if isinstance(data, dict) and data.get("errorType") == "security":
                    return True
            except Exception:  # nosec B110
                pass
        return False

    def _check_response(self, response: httpx.Response) -> Any:
        """Validate response status and detect session expiration or upstream error envelopes."""
        body = response.text
        if response.status_code == 401:
            raise SessionExpiredError(
                "e-Boekhouden web session expired or unauthorized"
            )

        if (
            "SECURITY_010" in body
            or "Niet ingelogd" in body
            or '"errorType":"security"' in body
        ):
            raise SessionExpiredError(
                "e-Boekhouden session expired (SECURITY_010 / Niet ingelogd)"
            )

        if response.status_code not in (200, 201, 204):
            raise WebSessionError(
                f"Web session API returned HTTP {response.status_code}: {body[:200]}"
            )

        if not body.strip():
            return {"success": True}

        try:
            data = response.json()
        except Exception as exc:
            raise WebSessionError(
                f"Failed to parse JSON response: {body[:200]}"
            ) from exc

        if isinstance(data, dict):
            error_type = data.get("errorType")
            if error_type:
                if error_type == "security":
                    raise SessionExpiredError(
                        f"Security error: {data.get('message', 'session invalid')}"
                    )
                msg = data.get("message") or data.get("code") or error_type
                raise WebSessionError(f"e-Boekhouden: {msg}")

        return data

    # ---------------- Sync Methods ---------------- #

    def _get_sync_client(self) -> httpx.Client | _SyncAutoAuthClient:
        if self._http is not None:
            return _SyncAutoAuthClient(self, self._http)
        raw_client = httpx.Client(
            base_url=self.base_url,
            headers=self._get_headers(),
            timeout=self.timeout,
        )
        return _SyncAutoAuthClient(self, raw_client)

    def _get_async_client(self) -> _AsyncAutoAuthClient:
        if self._async_http is not None:
            return _AsyncAutoAuthClient(self, self._async_http, close_on_exit=False)
        raw_client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._get_headers(),
            timeout=self.timeout,
        )
        return _AsyncAutoAuthClient(self, raw_client, close_on_exit=True)

    def get_import_grid(self, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        """Fetch unprocessed bank statement rows from /v1/api/import/gridtable.

        Returns:
            Dictionary with 'rowCount', 'rows' (parsed list of dicts), and 'raw'.
        """
        url = f"{self.base_url}/v1/api/import/gridtable?offset={offset}&limit={limit}&orderBy=&sortDirection=1"
        client = self._get_sync_client()
        try:
            resp = client.get(url, headers=self._get_headers())
            raw = self._check_response(resp)
            return {
                "rowCount": raw.get("rowCount", 0),
                "rows": parse_import_grid(raw),
                "raw": raw,
            }
        finally:
            if self._http is None:
                client.close()

    def get_mutatie_by_afschrift(self, params: dict[str, Any]) -> dict[str, Any]:
        """Fetch pre-fill suggestion for a bank statement line from /v1/api/mutatie/getmutatiebyafschrift."""
        query_str = urlencode({k: str(v) for k, v in params.items()})
        url = f"{self.base_url}/v1/api/mutatie/getmutatiebyafschrift?{query_str}"
        client = self._get_sync_client()
        try:
            resp = client.get(url, headers=self._get_headers())
            return self._check_response(resp)
        finally:
            if self._http is None:
                client.close()

    def link_file_to_mutation(self, mutation_id: int, file_id: int) -> dict[str, Any]:
        """Attach archive file to a mutation via POST /v1/api/folderkoppel/koppel/item."""
        url = f"{self.base_url}/v1/api/folderkoppel/koppel/item"
        payload = {
            "koppelId": mutation_id,
            "folders": [{"id": file_id, "soort": "F"}],
            "koppelType": "MUT",
        }
        client = self._get_sync_client()
        try:
            resp = client.post(url, json=payload, headers=self._get_headers())
            return self._check_response(resp)
        finally:
            if self._http is None:
                client.close()

    def get_archive_folders(self) -> Any:
        """Fetch Digital Archive folders via GET /v1/api/folder/directory."""
        url = f"{self.base_url}/v1/api/folder/directory"
        client = self._get_sync_client()
        try:
            resp = client.get(url, headers=self._get_headers())
            return self._check_response(resp)
        finally:
            if self._http is None:
                client.close()

    def get_archive_files(self, folder_id: int, include_deleted: bool = False) -> Any:
        """Fetch files and subfolders in a Digital Archive folder via GET /v1/api/folder/{folder_id}/filelist."""
        inc_del = str(include_deleted).lower()
        url = f"{self.base_url}/v1/api/folder/{folder_id}/filelist?includeDelete={inc_del}"
        client = self._get_sync_client()
        try:
            resp = client.get(url, headers=self._get_headers())
            return self._check_response(resp)
        finally:
            if self._http is None:
                client.close()

    def get_recognized_invoice_model(self, file_id: int) -> Any:
        """Fetch internal AI OCR recognition model for a Digital Archive file via GET /v1/api/mutatie/folder/{file_id}/model."""
        url = f"{self.base_url}/v1/api/mutatie/folder/{file_id}/model"
        client = self._get_sync_client()
        try:
            resp = client.get(url, headers=self._get_headers())
            return self._check_response(resp)
        finally:
            if self._http is None:
                client.close()

    def get_mutation_model_by_number(self, mutation_number: int) -> Any:
        """Fetch full web mutation model for an existing mutation number via GET /v1/api/mutatie/nr/{mutation_number}/model."""
        url = f"{self.base_url}/v1/api/mutatie/nr/{mutation_number}/model"
        client = self._get_sync_client()
        try:
            resp = client.get(url, headers=self._get_headers())
            return self._check_response(resp)
        finally:
            if self._http is None:
                client.close()

    def save_web_mutation(self, payload: dict[str, Any]) -> Any:
        """Create (POST) or update (PUT) a mutation in the web UI format via /v1/api/mutatie."""
        url = f"{self.base_url}/v1/api/mutatie"
        is_update = bool(payload.get("mutatie", {}).get("nr"))
        normalized = normalize_web_mutation_payload(payload, is_update=is_update)
        client = self._get_sync_client()
        try:
            if is_update:
                resp = client.put(url, json=normalized, headers=self._get_headers())
            else:
                resp = client.post(url, json=normalized, headers=self._get_headers())
            return self._check_response(resp)
        finally:
            if self._http is None:
                client.close()

    def move_archive_files(self, file_ids: list[int], target_folder_id: int) -> Any:
        """Move files in the Digital Archive via POST /v1/api/folder/bulk/update/{target_folder_id}."""
        url = f"{self.base_url}/v1/api/folder/bulk/update/{target_folder_id}"
        client = self._get_sync_client()
        try:
            resp = client.post(url, json=file_ids, headers=self._get_headers())
            return self._check_response(resp)
        finally:
            if self._http is None:
                client.close()

    def create_archive_folder(self, name: str, parent_folder_id: int = 0) -> Any:
        """Create a Digital Archive folder via POST /v1/api/folder/directory."""
        url = f"{self.base_url}/v1/api/folder/directory"
        payload = {"parentFolderId": parent_folder_id, "name": name}
        client = self._get_sync_client()
        try:
            resp = client.post(url, json=payload, headers=self._get_headers())
            return self._check_response(resp)
        finally:
            if self._http is None:
                client.close()

    def upload_archive_file(
        self,
        file_path: str | Path,
        folder_id: int = 0,
        file_name: str | None = None,
        overwrite: bool = True,
    ) -> Any:
        """Upload a local file to a Digital Archive folder via POST /v1/api/folder/upload."""
        path = _validate_local_file_path(file_path)
        resolved_name = file_name or path.name
        b64_data = base64.b64encode(path.read_bytes()).decode("ascii")
        url = f"{self.base_url}/v1/api/folder/upload"
        payload = {
            "fileName": resolved_name,
            "data": b64_data,
            "overwrite": overwrite,
            "folderId": folder_id,
        }
        client = self._get_sync_client()
        try:
            resp = client.post(url, json=payload, headers=self._get_headers())
            return self._check_response(resp)
        finally:
            if self._http is None:
                client.close()

    def delete_web_mutations(self, mutation_numbers: list[int]) -> dict[str, Any]:
        """Resolve mutation numbers (nr) to internal grid IDs and delete via POST /v1/api/mutatie/bulk/delete."""
        grid_url = f"{self.base_url}/v1/api/mutatie/gridtable"
        del_url = f"{self.base_url}/v1/api/mutatie/bulk/delete"
        client = self._get_sync_client()
        try:
            internal_ids: list[int] = []
            resolved_map: dict[int, int] = {}
            for nr in mutation_numbers:
                resp = client.post(
                    grid_url,
                    json={"filter": {"nr": str(nr)}},
                    headers=self._get_headers(),
                )
                grid_data = self._check_response(resp)
                rows = grid_data.get("data", []) if isinstance(grid_data, dict) else []
                for row in rows:
                    if (
                        isinstance(row, list)
                        and len(row) >= 2
                        and str(row[1]) == str(nr)
                    ):
                        internal_ids.append(int(row[0]))
                        resolved_map[int(nr)] = int(row[0])
                        break
            if not internal_ids:
                raise WebSessionError(
                    f"No matching internal mutation IDs found for mutation_numbers={mutation_numbers}"
                )
            del_resp = client.post(
                del_url, json=internal_ids, headers=self._get_headers()
            )
            del_result = self._check_response(del_resp)
            return {
                "mutation_numbers": mutation_numbers,
                "resolved_ids": resolved_map,
                "response": del_result,
            }
        finally:
            if self._http is None:
                client.close()

    def get_time_projects(self, active_only: bool = False) -> list[dict[str, Any]]:
        """Fetch time-tracking projects via /v1/api/project/getall and /v1/api/project/gridtable."""
        active_flag = "true" if active_only else "false"
        getall_url = f"{self.base_url}/v1/api/project/getall/{active_flag}"
        grid_url = f"{self.base_url}/v1/api/project/gridtable?offset=0&limit=50&orderBy=&sortDirection=1"
        client = self._get_sync_client()
        try:
            resp_all = client.get(getall_url, headers=self._get_headers())
            raw_all = self._check_response(resp_all)
            projects = raw_all if isinstance(raw_all, list) else []

            resp_grid = client.get(grid_url, headers=self._get_headers())
            raw_grid = self._check_response(resp_grid)
            grid_rows = parse_gridtable_rows(raw_grid)
            rel_by_id = {
                int(r["id"]): r.get("relatie")
                for r in grid_rows
                if r.get("id") is not None
            }

            result: list[dict[str, Any]] = []
            for p in projects:
                if not isinstance(p, dict):
                    continue
                pid = int(p.get("id") or 0)
                item = dict(p)
                if pid in rel_by_id:
                    item["relatie"] = rel_by_id[pid]
                result.append(item)
            return result
        finally:
            if self._http is None:
                client.close()

    def get_time_activities(self, active_only: bool = False) -> list[dict[str, Any]]:
        """Fetch time-tracking activities and rates via /v1/api/activiteit/getall."""
        active_flag = "true" if active_only else "false"
        url = f"{self.base_url}/v1/api/activiteit/getall/{active_flag}"
        client = self._get_sync_client()
        try:
            resp = client.get(url, headers=self._get_headers())
            raw = self._check_response(resp)
            return raw if isinstance(raw, list) else []
        finally:
            if self._http is None:
                client.close()

    def _build_time_entries_query(
        self,
        *,
        offset: int,
        limit: int,
        status: str,
        project_id: int | None,
        activity_id: int | None,
        period_start: str | None,
        period_end: str | None,
        include_non_billable: bool,
        include_notes: bool,
    ) -> str:
        status_norm = (status or "uninvoiced").strip().lower()
        if status_norm in ("uninvoiced", "factureren", "wip", "open"):
            zoeken_in = "FACTUREREN"
        elif status_norm in ("invoiced", "gefactureerd", "billed"):
            zoeken_in = "GEFACTUREERD"
        else:
            zoeken_in = "0"

        params: dict[str, str] = {
            "offset": str(offset),
            "limit": str(min(limit, 50)),
            "orderBy": "",
            "sortDirection": "1",
            "zoekenIn": zoeken_in,
            "showOpmerkingen": "true" if include_notes else "false",
        }
        if include_non_billable:
            params["nietFacturabel"] = "true"
        if project_id:
            params["projectId"] = str(project_id)
        if activity_id:
            params["activiteitId"] = str(activity_id)
        if period_start:
            params["periodStart"] = str(period_start)
        if period_end:
            params["periodEnd"] = str(period_end)
        return f"{self.base_url}/v1/api/uur/factureren/gridtable?{urlencode(params)}"

    def get_time_entries(
        self,
        *,
        status: str = "uninvoiced",
        project_id: int | None = None,
        activity_id: int | None = None,
        period_start: str | None = None,
        period_end: str | None = None,
        include_non_billable: bool = False,
        include_notes: bool = True,
        max_rows: int = 500,
    ) -> dict[str, Any]:
        """Fetch time entries from /v1/api/uur/factureren/gridtable with automatic 50-row pagination."""
        client = self._get_sync_client()
        try:
            all_rows: list[dict[str, Any]] = []
            offset = 0
            total_row_count = 0
            while len(all_rows) < max_rows:
                page_limit = min(50, max_rows - len(all_rows))
                url = self._build_time_entries_query(
                    offset=offset,
                    limit=page_limit,
                    status=status,
                    project_id=project_id,
                    activity_id=activity_id,
                    period_start=period_start,
                    period_end=period_end,
                    include_non_billable=include_non_billable,
                    include_notes=include_notes,
                )
                resp = client.get(url, headers=self._get_headers())
                raw = self._check_response(resp)
                total_row_count = int(raw.get("rowCount", 0))
                page_rows = parse_gridtable_rows(raw)
                if not page_rows:
                    break
                all_rows.extend(page_rows)
                offset += len(page_rows)
                if offset >= total_row_count or len(page_rows) < 50:
                    break

            projects = self.get_time_projects(active_only=False)
            activities = self.get_time_activities(active_only=False)
            proj_map = {int(p["id"]): p for p in projects if p.get("id") is not None}
            act_map = {str(a["naam"]): a for a in activities if a.get("naam")}

            summary = summarize_time_entries(all_rows, proj_map, act_map)
            return {
                "status_filter": status,
                "rowCount": total_row_count,
                "fetchedCount": len(all_rows),
                "summary": summary,
                "rows": all_rows,
            }
        finally:
            if self._http is None:
                client.close()

    def get_time_project(self, project_id: int) -> dict[str, Any]:
        """Fetch a single time-tracking project via GET /v1/api/project/{project_id}."""
        client = self._get_sync_client()
        try:
            url = f"{self.base_url}/v1/api/project/{int(project_id)}"
            resp = client.get(url, headers=self._get_headers())
            return self._check_response(resp)
        finally:
            if self._http is None:
                client.close()

    def create_time_project(
        self,
        *,
        name: str,
        relation_id: int | None = None,
        active: bool = True,
        invoice_template_id: int | None = None,
        invoicing_method: int = 1,
        group_by: int = 0,
        description_type: int = 1,
        hourly_rate: float = 0.0,
        hourly_rate_in_ex: str = "EX",
        hourly_rate_ledger_id: int | None = None,
        hourly_rate_vat_code: str | None = None,
        km_rate: float = 0.0,
        km_rate_in_ex: str = "EX",
        km_rate_ledger_id: int | None = None,
        km_rate_vat_code: str | None = None,
        cost_center_id: int = 0,
        notes: str = "",
        limit_enabled: bool = False,
        limit_hours: float = 0.0,
        limit_type: int = 0,
        alert_percentage: int = 0,
        alert_user_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        """Create a time project via POST /v1/api/project."""
        client = self._get_sync_client()
        try:
            hdrs = self._get_headers()
            need_ledgers = (
                hourly_rate_ledger_id is not None and hourly_rate_ledger_id < 1_000_000
            ) or (km_rate_ledger_id is not None and km_rate_ledger_id < 1_000_000)
            ledgers: list[dict[str, Any]] = []
            if need_ledgers:
                gbr_resp = self._check_response(
                    client.get(
                        f"{self.base_url}/v1/api/grootboekrekening/selectlist/actief/VWBAL",
                        headers=hdrs,
                    )
                )
                if isinstance(gbr_resp, list):
                    ledgers = gbr_resp

            resolved_hr_ledger = hourly_rate_ledger_id
            if resolved_hr_ledger is not None and resolved_hr_ledger < 1_000_000:
                resolved_hr_ledger = resolve_revenue_ledger_id(
                    ledgers,
                    ledger_id=hourly_rate_ledger_id,
                    vat_code=hourly_rate_vat_code or "BI_EU_VERK_D",
                )

            resolved_km_ledger = km_rate_ledger_id
            if resolved_km_ledger is not None and resolved_km_ledger < 1_000_000:
                resolved_km_ledger = resolve_revenue_ledger_id(
                    ledgers,
                    ledger_id=km_rate_ledger_id,
                    vat_code=km_rate_vat_code or "BI_EU_VERK_D",
                )

            payload: dict[str, Any] = {
                "id": 0,
                "naam": str(name),
                "actief": bool(active),
                "relatieId": int(relation_id) if relation_id is not None else 0,
                "factuurSjabloonId": (
                    int(invoice_template_id) if invoice_template_id is not None else 0
                ),
                "factuurWijze": int(invoicing_method),
                "factuurGroeperenOp": int(group_by),
                "factuurOmschrijvingType": int(description_type),
                "uurtarief": float(hourly_rate),
                "uurtariefInEx": (
                    "IN" if str(hourly_rate_in_ex).upper() == "IN" else "EX"
                ),
                "uurtariefGrootboekrekeningId": (
                    int(resolved_hr_ledger) if resolved_hr_ledger else 0
                ),
                "uurtariefBtwCode": str(hourly_rate_vat_code or ""),
                "kmtarief": float(km_rate),
                "kmtariefInEx": "IN" if str(km_rate_in_ex).upper() == "IN" else "EX",
                "kmtariefGrootboekrekeningId": (
                    int(resolved_km_ledger) if resolved_km_ledger else 0
                ),
                "kmtariefBtwCode": str(km_rate_vat_code or ""),
                "kostenplaatsId": int(cost_center_id or 0),
                "notities": str(notes or ""),
                "stelLimietIn": bool(limit_enabled),
                "limiet": float(limit_hours),
                "limietType": int(limit_type),
                "stuurMeldingBijPercentage": int(alert_percentage),
                "stuurMeldingGebruikerIds": alert_user_ids or [],
            }
            url = f"{self.base_url}/v1/api/project"
            resp = client.post(url, json=payload, headers=hdrs)
            result = self._check_response(resp)
            return (
                result
                if isinstance(result, dict)
                else {"id": result, "project": payload}
            )
        finally:
            if self._http is None:
                client.close()

    def update_time_project(
        self,
        project_id: int,
        *,
        name: str | None = None,
        active: bool | None = None,
        relation_id: int | None = None,
        invoice_template_id: int | None = None,
        invoicing_method: int | None = None,
        group_by: int | None = None,
        description_type: int | None = None,
        hourly_rate: float | None = None,
        hourly_rate_in_ex: str | None = None,
        hourly_rate_ledger_id: int | None = None,
        hourly_rate_vat_code: str | None = None,
        km_rate: float | None = None,
        km_rate_in_ex: str | None = None,
        km_rate_ledger_id: int | None = None,
        km_rate_vat_code: str | None = None,
        cost_center_id: int | None = None,
        notes: str | None = None,
        limit_enabled: bool | None = None,
        limit_hours: float | None = None,
        limit_type: int | None = None,
        alert_percentage: int | None = None,
        alert_user_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        """Update a time project via PUT /v1/api/project."""
        client = self._get_sync_client()
        try:
            hdrs = self._get_headers()
            current = self.get_time_project(project_id)
            payload = dict(current)

            need_ledgers = (
                hourly_rate_ledger_id is not None and hourly_rate_ledger_id < 1_000_000
            ) or (km_rate_ledger_id is not None and km_rate_ledger_id < 1_000_000)
            ledgers: list[dict[str, Any]] = []
            if need_ledgers:
                gbr_resp = self._check_response(
                    client.get(
                        f"{self.base_url}/v1/api/grootboekrekening/selectlist/actief/VWBAL",
                        headers=hdrs,
                    )
                )
                if isinstance(gbr_resp, list):
                    ledgers = gbr_resp

            if name is not None:
                payload["naam"] = str(name)
            if active is not None:
                payload["actief"] = bool(active)
            if relation_id is not None:
                payload["relatieId"] = int(relation_id)
            if invoice_template_id is not None:
                payload["factuurSjabloonId"] = int(invoice_template_id)
            if invoicing_method is not None:
                payload["factuurWijze"] = int(invoicing_method)
            if group_by is not None:
                payload["factuurGroeperenOp"] = int(group_by)
            if description_type is not None:
                payload["factuurOmschrijvingType"] = int(description_type)
            if hourly_rate is not None:
                payload["uurtarief"] = float(hourly_rate)
            if hourly_rate_in_ex is not None:
                payload["uurtariefInEx"] = (
                    "IN" if str(hourly_rate_in_ex).upper() == "IN" else "EX"
                )
            if hourly_rate_ledger_id is not None:
                hr_lid = hourly_rate_ledger_id
                if hr_lid < 1_000_000:
                    hr_lid = resolve_revenue_ledger_id(
                        ledgers,
                        ledger_id=hr_lid,
                        vat_code=hourly_rate_vat_code
                        or payload.get("uurtariefBtwCode")
                        or "BI_EU_VERK_D",
                    )
                payload["uurtariefGrootboekrekeningId"] = int(hr_lid)
            if hourly_rate_vat_code is not None:
                payload["uurtariefBtwCode"] = str(hourly_rate_vat_code)
            if km_rate is not None:
                payload["kmtarief"] = float(km_rate)
            if km_rate_in_ex is not None:
                payload["kmtariefInEx"] = (
                    "IN" if str(km_rate_in_ex).upper() == "IN" else "EX"
                )
            if km_rate_ledger_id is not None:
                km_lid = km_rate_ledger_id
                if km_lid < 1_000_000:
                    km_lid = resolve_revenue_ledger_id(
                        ledgers,
                        ledger_id=km_lid,
                        vat_code=km_rate_vat_code
                        or payload.get("kmtariefBtwCode")
                        or "BI_EU_VERK_D",
                    )
                payload["kmtariefGrootboekrekeningId"] = int(km_lid)
            if km_rate_vat_code is not None:
                payload["kmtariefBtwCode"] = str(km_rate_vat_code)
            if cost_center_id is not None:
                payload["kostenplaatsId"] = int(cost_center_id)
            if notes is not None:
                payload["notities"] = str(notes)
            if limit_enabled is not None:
                payload["stelLimietIn"] = bool(limit_enabled)
            if limit_hours is not None:
                payload["limiet"] = float(limit_hours)
            if limit_type is not None:
                payload["limietType"] = int(limit_type)
            if alert_percentage is not None:
                payload["stuurMeldingBijPercentage"] = int(alert_percentage)
            if alert_user_ids is not None:
                payload["stuurMeldingGebruikerIds"] = alert_user_ids

            url = f"{self.base_url}/v1/api/project"
            resp = client.put(url, json=payload, headers=hdrs)
            result = self._check_response(resp)
            return (
                result
                if isinstance(result, dict)
                else {"id": int(project_id), "project": payload}
            )
        finally:
            if self._http is None:
                client.close()

    def get_time_entry(self, entry_id: int) -> dict[str, Any]:
        """Fetch a single time entry via GET /v1/api/uur/{entry_id}."""
        client = self._get_sync_client()
        try:
            url = f"{self.base_url}/v1/api/uur/{int(entry_id)}"
            resp = client.get(url, headers=self._get_headers())
            return self._check_response(resp)
        finally:
            if self._http is None:
                client.close()

    def create_time_entry(
        self,
        *,
        project_id: int,
        activity_id: int,
        hours: float,
        date: str | dict[str, int] | None = None,
        employee_id: int | None = None,
        notes: str = "",
        billable: bool = True,
        kilometers: float = 0.0,
    ) -> dict[str, Any]:
        """Create a time entry via POST /v1/api/uur."""
        client = self._get_sync_client()
        try:
            hdrs = self._get_headers()
            resolved_emp_id = employee_id
            if resolved_emp_id is None:
                try:
                    users_resp = self._check_response(
                        client.get(
                            f"{self.base_url}/v1/api/user/selectlist", headers=hdrs
                        )
                    )
                    if (
                        isinstance(users_resp, list)
                        and users_resp
                        and users_resp[0].get("id")
                    ):
                        resolved_emp_id = int(users_resp[0]["id"])
                except Exception:
                    resolved_emp_id = 0

            datum_dict = parse_eb_date_dict(date, default_offset_days=0)
            payload: dict[str, Any] = {
                "id": 0,
                "datum": datum_dict,
                "gebruikerId": int(resolved_emp_id or 0),
                "projectId": int(project_id),
                "activiteitId": int(activity_id),
                "aantalUren": float(hours),
                "opmerkingen": str(notes or ""),
                "aantalKilometers": float(kilometers or 0.0),
            }
            url = f"{self.base_url}/v1/api/uur"
            resp = client.post(url, json=payload, headers=hdrs)
            result = self._check_response(resp)
            return (
                result if isinstance(result, dict) else {"id": result, "entry": payload}
            )
        finally:
            if self._http is None:
                client.close()

    def update_time_entry(
        self,
        entry_id: int,
        *,
        date: str | dict[str, int] | None = None,
        hours: float | None = None,
        project_id: int | None = None,
        activity_id: int | None = None,
        employee_id: int | None = None,
        notes: str | None = None,
        billable: bool | None = None,
        kilometers: float | None = None,
    ) -> dict[str, Any]:
        """Update an existing time entry via PUT /v1/api/uur."""
        client = self._get_sync_client()
        try:
            current = self.get_time_entry(entry_id)
            payload = dict(current)
            if date is not None:
                payload["datum"] = parse_eb_date_dict(date)
            if hours is not None:
                payload["aantalUren"] = float(hours)
                payload.pop("aantal", None)
            if project_id is not None:
                payload["projectId"] = int(project_id)
            if activity_id is not None:
                payload["activiteitId"] = int(activity_id)
            if employee_id is not None:
                payload["gebruikerId"] = int(employee_id)
                payload.pop("medewerkerId", None)
            if notes is not None:
                payload["opmerkingen"] = str(notes)
            if kilometers is not None:
                payload["aantalKilometers"] = float(kilometers)
                payload.pop("kilometers", None)

            url = f"{self.base_url}/v1/api/uur"
            resp = client.put(url, json=payload, headers=self._get_headers())
            result = self._check_response(resp)
            return (
                result
                if isinstance(result, dict)
                else {"id": int(entry_id), "entry": payload}
            )
        finally:
            if self._http is None:
                client.close()

    def delete_time_entry(self, entry_id: int) -> dict[str, Any]:
        """Delete a time entry via DELETE /v1/api/uur/{entry_id}."""
        client = self._get_sync_client()
        try:
            url = f"{self.base_url}/v1/api/uur/{int(entry_id)}"
            resp = client.delete(url, headers=self._get_headers())
            res = self._check_response(resp)
            return {"success": True, "entry_id": int(entry_id), "response": res}
        finally:
            if self._http is None:
                client.close()

    def invoice_time_entries(
        self,
        entry_ids: list[int],
        *,
        date: str | None = None,
        group_by: int = 4,
        description_type: int = 2,
        process_directly: bool = True,
        send_directly: bool = False,
        reference: str | None = None,
        invoice_text: str | None = None,
    ) -> dict[str, Any]:
        """Convert time entries into an invoice via POST /v1/api/uur/factureren."""
        if date:
            date_str = format_eb_date_dict(parse_eb_date_dict(date)) or str(date)[:10]
        else:
            date_str = dt.date.today().isoformat()

        payload: dict[str, Any] = {
            "ids": [int(eid) for eid in entry_ids],
            "groeperenOp": int(group_by),
            "datum": date_str,
            "omschrijvingType": int(description_type),
            "verwerkDirect": bool(process_directly),
            "verstuurDirect": bool(send_directly),
        }
        url = f"{self.base_url}/v1/api/uur/factureren"
        client = self._get_sync_client()
        try:
            resp = client.post(url, json=payload, headers=self._get_headers())
            result = self._check_response(resp)
            if isinstance(result, dict):
                error_count = result.get("errorCount", 0)
                error_messages = result.get("errorMessages") or []
                if error_count > 0 or error_messages:
                    raise WebSessionError(
                        f"Failed to invoice time entries: {', '.join(str(m) for m in error_messages) or 'errorCount > 0'}"
                    )

            created_ids: list[int] = []
            if isinstance(result, dict) and isinstance(result.get("ids"), list):
                created_ids = [int(i) for i in result["ids"]]

            updated_invoices: list[dict[str, Any]] = []
            if (reference is not None or invoice_text is not None) and created_ids:
                for inv_id in created_ids:
                    upd = self.update_invoice(
                        invoice_id=inv_id,
                        reference=reference,
                        invoice_text=invoice_text,
                    )
                    updated_invoices.append(upd)

            return {
                "success": True,
                "invoice_ids": created_ids,
                "reference": reference,
                "invoice_text": invoice_text,
                "response": result,
                "updated_invoices": updated_invoices,
            }
        finally:
            if self._http is None:
                client.close()

    def update_invoice(
        self,
        invoice_id: int,
        *,
        reference: str | None = None,
        invoice_text: str | None = None,
        date: str | None = None,
        payment_term_days: int | None = None,
        template_id: int | None = None,
        internal_note: str | None = None,
    ) -> dict[str, Any]:
        """Update invoice header fields via PUT /v1/api/factuur/model and trigger PDF regeneration."""
        client = self._get_sync_client()
        try:
            hdrs = self._get_headers()
            get_url = f"{self.base_url}/v1/api/factuur/{int(invoice_id)}"
            resp = client.get(get_url, headers=hdrs)
            raw_data = self._check_response(resp)

            factuur_obj = (
                dict(raw_data["factuur"])
                if isinstance(raw_data, dict) and "factuur" in raw_data
                else dict(raw_data)
            )

            if reference is not None:
                factuur_obj["kenmerk"] = str(reference)
            if invoice_text is not None:
                factuur_obj["factuurtekst"] = str(invoice_text)
            if date is not None:
                factuur_obj["datum"] = parse_eb_date_dict(date)
            if payment_term_days is not None:
                factuur_obj["betalingstermijn"] = int(payment_term_days)
            if template_id is not None:
                factuur_obj["sjabloonId"] = int(template_id)
            if internal_note is not None:
                factuur_obj["notitie"] = str(internal_note)

            model_payload = {
                "factuur": factuur_obj,
                "factuurItems": {"insert": [], "update": [], "delete": []},
            }
            put_url = f"{self.base_url}/v1/api/factuur/model"
            put_resp = client.put(put_url, json=model_payload, headers=hdrs)
            model_res = self._check_response(put_resp)

            # Schedule PDF regeneration
            sched_url = f"{self.base_url}/v1/api/factuur/pdfinvoiceschedule?factuurId={int(invoice_id)}"
            try:
                sched_resp = client.get(sched_url, headers=hdrs)
                self._check_response(sched_resp)
            except Exception as sched_err:
                logger.warning(
                    "Could not schedule PDF invoice regeneration: %s", sched_err
                )

            return {
                "success": True,
                "invoice_id": int(invoice_id),
                "reference": factuur_obj.get("kenmerk"),
                "invoice_text": factuur_obj.get("factuurtekst"),
                "response": model_res,
            }
        finally:
            if self._http is None:
                client.close()

    def _build_quotes_query(
        self,
        *,
        offset: int = 0,
        limit: int = 50,
        quote_number: str | None = None,
        relation_id: int | None = None,
        status_id: int | None = None,
        assigned_user_id: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        expiration_from: str | None = None,
        expiration_to: str | None = None,
    ) -> str:
        params: dict[str, str] = {
            "offset": str(offset),
            "limit": str(min(limit, 500)),
            "orderBy": "",
            "sortDirection": "1",
        }
        if quote_number:
            params["nummer"] = str(quote_number)
        if relation_id is not None:
            params["relatieId"] = str(relation_id)
        if status_id is not None:
            params["statusId"] = str(status_id)
        if assigned_user_id is not None:
            params["behandelGebruikerId"] = str(assigned_user_id)
        if date_from:
            params["datumVan"] = str(date_from)
        if date_to:
            params["datumTot"] = str(date_to)
        if expiration_from:
            params["vervaldatumVan"] = str(expiration_from)
        if expiration_to:
            params["vervaldatumTot"] = str(expiration_to)
        return f"{self.base_url}/v1/api/offerte/gridtable?{urlencode(params)}"

    def get_quote_metadata(self) -> dict[str, Any]:
        """Fetch quote statuses, templates, users, and next quote number."""
        client = self._get_sync_client()
        try:
            hdrs = self._get_headers()
            statuses = self._check_response(
                client.get(f"{self.base_url}/v1/api/offertestatus", headers=hdrs)
            )
            templates = self._check_response(
                client.get(
                    f"{self.base_url}/v1/api/offertesjabloon/selectlist", headers=hdrs
                )
            )
            users = self._check_response(
                client.get(f"{self.base_url}/v1/api/user/selectlist", headers=hdrs)
            )
            next_nr = self._check_response(
                client.get(f"{self.base_url}/v1/api/offerte/nummer/next", headers=hdrs)
            )
            return {
                "statuses": statuses if isinstance(statuses, list) else [],
                "templates": templates if isinstance(templates, list) else [],
                "users": users if isinstance(users, list) else [],
                "next_quote_number": str(next_nr) if next_nr is not None else "",
            }
        finally:
            if self._http is None:
                client.close()

    def list_quotes(
        self,
        *,
        offset: int = 0,
        limit: int = 50,
        quote_number: str | None = None,
        relation_id: int | None = None,
        status_id: int | None = None,
        assigned_user_id: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        expiration_from: str | None = None,
        expiration_to: str | None = None,
        include_metadata: bool = True,
    ) -> dict[str, Any]:
        """List quotes (offertes) from /v1/api/offerte/gridtable with optional filters and metadata."""
        url = self._build_quotes_query(
            offset=offset,
            limit=limit,
            quote_number=quote_number,
            relation_id=relation_id,
            status_id=status_id,
            assigned_user_id=assigned_user_id,
            date_from=date_from,
            date_to=date_to,
            expiration_from=expiration_from,
            expiration_to=expiration_to,
        )
        client = self._get_sync_client()
        try:
            resp = client.get(url, headers=self._get_headers())
            raw = self._check_response(resp)
            rows = parse_gridtable_rows(raw)
            total_excl = round(sum(float(r.get("totaalExcl") or 0.0) for r in rows), 2)
            total_vat = round(sum(float(r.get("totaalbtw") or 0.0) for r in rows), 2)
            total_incl = round(sum(float(r.get("totaalIncl") or 0.0) for r in rows), 2)
            result: dict[str, Any] = {
                "rowCount": int(raw.get("rowCount", len(rows))),
                "fetchedCount": len(rows),
                "summary": {
                    "total_excl_vat": total_excl,
                    "total_vat": total_vat,
                    "total_incl_vat": total_incl,
                },
                "quotes": rows,
            }
            if include_metadata:
                result["metadata"] = self.get_quote_metadata()
            return result
        finally:
            if self._http is None:
                client.close()

    def get_quote(
        self,
        quote_id: int | None = None,
        quote_number: str | None = None,
    ) -> dict[str, Any]:
        """Fetch full details of a quote by ID or quote_number, including line items and attached files."""
        resolved_id = quote_id
        if resolved_id is None:
            if not quote_number:
                raise ValueError("Either quote_id or quote_number must be provided.")
            listing = self.list_quotes(
                quote_number=quote_number, include_metadata=False
            )
            for q in listing.get("quotes", []):
                if (
                    str(q.get("nummer") or "").strip().lower()
                    == quote_number.strip().lower()
                ):
                    resolved_id = int(q["id"])
                    break
            if resolved_id is None and listing.get("quotes"):
                resolved_id = int(listing["quotes"][0]["id"])
            if resolved_id is None:
                raise WebSessionError(
                    f"No quote found matching quote_number='{quote_number}'"
                )

        qid = int(resolved_id)
        client = self._get_sync_client()
        try:
            hdrs = self._get_headers()
            model = self._check_response(
                client.get(f"{self.base_url}/v1/api/offerte/{qid}", headers=hdrs)
            )
            dashboard = self._check_response(
                client.get(
                    f"{self.base_url}/v1/api/offerte/dashboard/{qid}", headers=hdrs
                )
            )
            in_ex = str((model or {}).get("inEx") or "EX").upper()
            is_incl_str = "true" if in_ex == "IN" else "false"
            regels_raw = self._check_response(
                client.get(
                    f"{self.base_url}/v1/api/offerteregel/gridtable/{qid}"
                    f"?isInclBtw={is_incl_str}&addArtikelVoorraad=false",
                    headers=hdrs,
                )
            )
            files_raw = self._check_response(
                client.get(
                    f"{self.base_url}/v1/api/folder/gekoppeldgridtable"
                    f"?offset=0&limit=50&orderBy=&sortDirection=1&koppelId={qid}&koppelSoort=OF",
                    headers=hdrs,
                )
            )
            items = parse_gridtable_rows(regels_raw)
            attached_files = parse_gridtable_rows(files_raw)
            return {
                "id": qid,
                "quote_number": model.get("nummer") or dashboard.get("nummer"),
                "status": dashboard.get("status"),
                "status_id": model.get("statusId"),
                "relation_id": model.get("relatieId") or dashboard.get("relatieId"),
                "relation_code": dashboard.get("relatieCode"),
                "relation_name": dashboard.get("relatieBedrijf"),
                "date": format_eb_date_dict(model.get("datum"))
                or format_eb_date_dict(dashboard.get("datum")),
                "expiration_date": format_eb_date_dict(model.get("vervaldatum"))
                or format_eb_date_dict(dashboard.get("vervalDatum")),
                "reference": model.get("kenmerk")
                or dashboard.get("kenmerkKlant")
                or "",
                "in_ex_vat": in_ex,
                "template_id": model.get("sjabloonId"),
                "assigned_user_id": model.get("behandelGebruikerId"),
                "assigned_user": dashboard.get("inBehandelingBij"),
                "public_note": model.get("notitiePubliek") or "",
                "internal_note": model.get("notitieIntern") or "",
                "total_excl_vat": float(model.get("totaalExcl") or 0.0),
                "total_vat": float(model.get("totaalBtw") or 0.0),
                "total_incl_vat": float(model.get("totaalIncl") or 0.0),
                "items": items,
                "attached_files": attached_files,
                "model": model,
                "dashboard": dashboard,
            }
        finally:
            if self._http is None:
                client.close()

    def link_file_to_quote(self, quote_id: int, file_id: int) -> dict[str, Any]:
        """Attach a Digital Archive file to a quote via POST /v1/api/folderkoppel/koppel/item (koppelType='OF')."""
        url = f"{self.base_url}/v1/api/folderkoppel/koppel/item"
        payload = {
            "koppelId": int(quote_id),
            "folders": [{"id": int(file_id), "soort": "F"}],
            "koppelType": "OF",
        }
        client = self._get_sync_client()
        try:
            resp = client.post(url, json=payload, headers=self._get_headers())
            return self._check_response(resp)
        finally:
            if self._http is None:
                client.close()

    def create_quote(
        self,
        *,
        relation_id: int,
        items: list[dict[str, Any]] | None = None,
        quote_number: str | None = None,
        date: str | None = None,
        expiration_date: str | None = None,
        expiration_days: int = 30,
        status_id: int | None = None,
        status: str | None = None,
        template_id: int | None = None,
        assigned_user_id: int | None = None,
        in_ex_vat: str = "EX",
        reference: str = "",
        public_note: str = "",
        internal_note: str = "",
        invoice_email_template_id: int | str | None = None,
        archive_file_id: int | None = None,
    ) -> dict[str, Any]:
        """Create a quote (offerte), insert line items, optionally attach an archive PDF, and return full quote."""
        meta = self.get_quote_metadata()
        resolved_status_id = resolve_quote_status_id(
            meta["statuses"], status_id=status_id, status=status
        )
        resolved_number = (
            quote_number.strip()
            if quote_number and quote_number.strip()
            else meta["next_quote_number"]
        )
        if not resolved_number:
            raise WebSessionError(
                "Could not determine quote_number (/v1/api/offerte/nummer/next returned empty)."
            )

        resolved_template_id = template_id
        if resolved_template_id is None:
            active_templates = [
                t for t in meta["templates"] if not t.get("inactief")
            ] or meta["templates"]
            if active_templates and active_templates[0].get("id") is not None:
                resolved_template_id = int(active_templates[0]["id"])
        if resolved_template_id is None:
            raise WebSessionError(
                "No quote template (offertesjabloon) found in e-Boekhouden."
            )

        resolved_user_id = assigned_user_id
        if resolved_user_id is None and meta["users"]:
            resolved_user_id = int(meta["users"][0]["id"])

        datum_dict = parse_eb_date_dict(date, default_offset_days=0)
        verval_dict = parse_eb_date_dict(
            expiration_date, base_date=datum_dict, default_offset_days=expiration_days
        )
        norm_in_ex = "IN" if str(in_ex_vat or "EX").upper() == "IN" else "EX"

        header_payload: dict[str, Any] = {
            "statusId": int(resolved_status_id),
            "nummer": str(resolved_number),
            "behandelGebruikerId": resolved_user_id,
            "relatieId": int(relation_id),
            "kenmerk": str(reference or "")[:50],
            "datum": datum_dict,
            "vervaldatum": verval_dict,
            "sjabloonId": int(resolved_template_id),
            "factuurEmailsjabloonId": (
                invoice_email_template_id
                if invoice_email_template_id is not None
                else ""
            ),
            "notitiePubliek": str(public_note or ""),
            "notitieIntern": str(internal_note or ""),
            "inEx": norm_in_ex,
        }

        client = self._get_sync_client()
        try:
            hdrs = self._get_headers()
            create_resp = self._check_response(
                client.post(
                    f"{self.base_url}/v1/api/offerte",
                    json=header_payload,
                    headers=hdrs,
                )
            )
            created_id = int(
                create_resp["id"]
                if isinstance(create_resp, dict) and "id" in create_resp
                else create_resp
            )

            if items:
                need_ledgers = any(
                    not (it.get("ledger_id") or it.get("grootboekId"))
                    or int(it.get("ledger_id") or it.get("grootboekId") or 0)
                    < 1_000_000
                    for it in items
                )
                ledgers: list[dict[str, Any]] = []
                if need_ledgers:
                    gbr_resp = self._check_response(
                        client.get(
                            f"{self.base_url}/v1/api/grootboekrekening/selectlist/actief/VWBAL",
                            headers=hdrs,
                        )
                    )
                    if isinstance(gbr_resp, list):
                        ledgers = gbr_resp

                for idx, item_data in enumerate(items, start=1):
                    line_payload = build_quote_line_payload(
                        item_data,
                        quote_id=created_id,
                        in_ex_vat=norm_in_ex,
                        volgorde=idx,
                        ledgers=ledgers,
                    )
                    self._check_response(
                        client.post(
                            f"{self.base_url}/v1/api/offerteregel",
                            json=line_payload,
                            headers=hdrs,
                        )
                    )

            if archive_file_id is not None:
                self.link_file_to_quote(
                    quote_id=created_id, file_id=int(archive_file_id)
                )

            return self.get_quote(quote_id=created_id)
        finally:
            if self._http is None:
                client.close()

    def update_quote(
        self,
        *,
        quote_id: int,
        relation_id: int | None = None,
        quote_number: str | None = None,
        date: str | None = None,
        expiration_date: str | None = None,
        status_id: int | None = None,
        status: str | None = None,
        template_id: int | None = None,
        assigned_user_id: int | None = None,
        in_ex_vat: str | None = None,
        reference: str | None = None,
        public_note: str | None = None,
        internal_note: str | None = None,
        items: list[dict[str, Any]] | None = None,
        replace_items: bool = True,
        archive_file_id: int | None = None,
    ) -> dict[str, Any]:
        """Update an existing quote header, optionally replace/append line items or attach an archive file."""
        qid = int(quote_id)
        client = self._get_sync_client()
        try:
            hdrs = self._get_headers()
            existing = self._check_response(
                client.get(f"{self.base_url}/v1/api/offerte/{qid}", headers=hdrs)
            )
            if not isinstance(existing, dict):
                raise WebSessionError(f"Quote {qid} not found.")

            resolved_status_id = existing.get("statusId")
            if status_id is not None or status is not None:
                statuses = self._check_response(
                    client.get(f"{self.base_url}/v1/api/offertestatus", headers=hdrs)
                )
                resolved_status_id = resolve_quote_status_id(
                    statuses if isinstance(statuses, list) else [],
                    status_id=status_id,
                    status=status,
                )

            datum_dict = (
                parse_eb_date_dict(date) if date is not None else existing.get("datum")
            )
            verval_dict = (
                parse_eb_date_dict(expiration_date)
                if expiration_date is not None
                else existing.get("vervaldatum")
            )
            norm_in_ex = (
                ("IN" if str(in_ex_vat).upper() == "IN" else "EX")
                if in_ex_vat is not None
                else str(existing.get("inEx") or "EX")
            )

            update_payload: dict[str, Any] = {
                "id": qid,
                "statusId": int(resolved_status_id or 0),
                "nummer": (
                    str(quote_number)
                    if quote_number is not None
                    else existing.get("nummer")
                ),
                "behandelGebruikerId": (
                    int(assigned_user_id)
                    if assigned_user_id is not None
                    else existing.get("behandelGebruikerId")
                ),
                "relatieId": (
                    int(relation_id)
                    if relation_id is not None
                    else existing.get("relatieId")
                ),
                "kenmerk": (
                    str(reference)[:50]
                    if reference is not None
                    else (existing.get("kenmerk") or "")
                ),
                "datum": datum_dict,
                "vervaldatum": verval_dict,
                "sjabloonId": (
                    int(template_id)
                    if template_id is not None
                    else existing.get("sjabloonId")
                ),
                "factuurEmailsjabloonId": existing.get("factuurEmailsjabloonId", ""),
                "notitiePubliek": (
                    str(public_note)
                    if public_note is not None
                    else (existing.get("notitiePubliek") or "")
                ),
                "notitieIntern": (
                    str(internal_note)
                    if internal_note is not None
                    else (existing.get("notitieIntern") or "")
                ),
                "inEx": norm_in_ex,
            }

            self._check_response(
                client.put(
                    f"{self.base_url}/v1/api/offerte",
                    json=update_payload,
                    headers=hdrs,
                )
            )

            if items is not None:
                is_incl_str = "true" if norm_in_ex == "IN" else "false"
                regels_raw = self._check_response(
                    client.get(
                        f"{self.base_url}/v1/api/offerteregel/gridtable/{qid}"
                        f"?isInclBtw={is_incl_str}&addArtikelVoorraad=false",
                        headers=hdrs,
                    )
                )
                existing_rows = parse_gridtable_rows(regels_raw)
                start_volgorde = len(existing_rows) + 1
                if replace_items and existing_rows:
                    for row in existing_rows:
                        if row.get("id") is not None:
                            self._check_response(
                                client.delete(
                                    f"{self.base_url}/v1/api/offerteregel/{int(row['id'])}",
                                    headers=hdrs,
                                )
                            )
                    start_volgorde = 1

                if items:
                    need_ledgers = any(
                        not (it.get("ledger_id") or it.get("grootboekId"))
                        or int(it.get("ledger_id") or it.get("grootboekId") or 0)
                        < 1_000_000
                        for it in items
                    )
                    ledgers: list[dict[str, Any]] = []
                    if need_ledgers:
                        gbr_resp = self._check_response(
                            client.get(
                                f"{self.base_url}/v1/api/grootboekrekening/selectlist/actief/VWBAL",
                                headers=hdrs,
                            )
                        )
                        if isinstance(gbr_resp, list):
                            ledgers = gbr_resp

                    for idx, item_data in enumerate(items, start=start_volgorde):
                        line_payload = build_quote_line_payload(
                            item_data,
                            quote_id=qid,
                            in_ex_vat=norm_in_ex,
                            volgorde=idx,
                            ledgers=ledgers,
                        )
                        self._check_response(
                            client.post(
                                f"{self.base_url}/v1/api/offerteregel",
                                json=line_payload,
                                headers=hdrs,
                            )
                        )

            if archive_file_id is not None:
                self.link_file_to_quote(quote_id=qid, file_id=int(archive_file_id))

            return self.get_quote(quote_id=qid)
        finally:
            if self._http is None:
                client.close()

    def delete_quote(self, quote_id: int) -> dict[str, Any]:
        """Delete a quote by ID via DELETE /v1/api/offerte/{quote_id}."""
        qid = int(quote_id)
        url = f"{self.base_url}/v1/api/offerte/{qid}"
        client = self._get_sync_client()
        try:
            resp = client.delete(url, headers=self._get_headers())
            result = self._check_response(resp)
            return {
                "quote_id": qid,
                "deleted": True,
                "response": result,
            }
        finally:
            if self._http is None:
                client.close()

    def get_invoice_mail_data(
        self, invoice_id: int, template_id: int | None = None
    ) -> dict[str, Any]:
        """Fetch pre-rendered email template and recipient data for an invoice."""
        if template_id is not None:
            url = f"{self.base_url}/v1/api/factuur/maildata/{int(template_id)}/{int(invoice_id)}"
        else:
            url = f"{self.base_url}/v1/api/factuur/{int(invoice_id)}/maildata"

        client = self._get_sync_client()
        try:
            resp = client.get(url, headers=self._get_headers())
            result = self._check_response(resp)
            return result if isinstance(result, dict) else {}
        finally:
            if self._http is None:
                client.close()

    def send_invoice_email(
        self,
        invoice_id: int,
        recipient_email: str | None = None,
        sender_email: str | None = None,
        sender_name: str | None = None,
        subject: str | None = None,
        body: str | None = None,
        cc_email: str | None = None,
        bcc_email: str | None = None,
        attach_ubl: bool = True,
        email_template_id: int | None = None,
    ) -> dict[str, Any]:
        """Send an invoice by email via POST /v1/api/factuur/mail."""
        mail_data: dict[str, Any] = {}
        if email_template_id is not None or not (recipient_email and subject and body):
            try:
                mail_data = self.get_invoice_mail_data(
                    invoice_id=invoice_id, template_id=email_template_id
                )
            except Exception:
                mail_data = {}

        resolved_recipient = recipient_email
        if not resolved_recipient:
            resolved_recipient = mail_data.get("emailAan")
            if not resolved_recipient and isinstance(mail_data.get("to"), list):
                emails = [
                    str(t["email"])
                    for t in mail_data["to"]
                    if isinstance(t, dict) and t.get("email")
                ]
                if emails:
                    resolved_recipient = ", ".join(emails)

        if not resolved_recipient:
            raise ValueError(
                f"No recipient email address found for invoice {invoice_id}. "
                "Specify recipient_email explicitly."
            )

        resolved_subject = (
            subject or mail_data.get("subject") or f"Factuur {invoice_id}"
        )
        resolved_body = body or mail_data.get("body") or ""
        resolved_from_email = (
            sender_email or mail_data.get("emailVan") or mail_data.get("vanEmail") or ""
        )
        resolved_from_name = (
            sender_name or mail_data.get("naam") or mail_data.get("vanNaam") or ""
        )
        resolved_cc = cc_email or mail_data.get("ccEmail") or mail_data.get("cc") or ""
        resolved_bcc = (
            bcc_email or mail_data.get("bccEmail") or mail_data.get("bcc") or ""
        )

        payload: dict[str, Any] = {
            "factuurId": int(invoice_id),
            "aanEmail": str(resolved_recipient),
            "vanEmail": str(resolved_from_email),
            "vanNaam": str(resolved_from_name),
            "ccEmail": str(resolved_cc),
            "bccEmail": str(resolved_bcc),
            "subject": str(resolved_subject),
            "body": str(resolved_body),
            "factuurUbl": bool(attach_ubl),
            "rememberMessage": False,
            "useEmailSjabloon": False,
            "bijlagen": [],
        }

        url = f"{self.base_url}/v1/api/factuur/mail"
        client = self._get_sync_client()
        try:
            resp = client.post(url, json=payload, headers=self._get_headers())
            result = self._check_response(resp)
            if isinstance(result, dict):
                error_count = result.get("errorCount", 0)
                error_msg = result.get("errorMessage")
                if (error_count and error_count > 0) or error_msg:
                    raise WebSessionError(
                        f"Failed to send invoice email for invoice {invoice_id}: {error_msg or 'errorCount > 0'}"
                    )
            return {
                "success": True,
                "invoice_id": int(invoice_id),
                "recipient_email": str(resolved_recipient),
                "from_email": str(resolved_from_email),
                "from_name": str(resolved_from_name),
                "email_template_id": email_template_id,
                "subject": str(resolved_subject),
                "body": str(resolved_body),
                "attach_ubl": bool(attach_ubl),
                "response": result,
            }
        finally:
            if self._http is None:
                client.close()

    # ---------------- Async Methods ---------------- #

    async def async_get_import_grid(
        self, offset: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        """Async version of get_import_grid."""
        url = f"{self.base_url}/v1/api/import/gridtable?offset={offset}&limit={limit}&orderBy=&sortDirection=1"
        async with self._get_async_client() as client:
            resp = await client.get(url)
            raw = self._check_response(resp)
            return {
                "rowCount": raw.get("rowCount", 0),
                "rows": parse_import_grid(raw),
                "raw": raw,
            }

    async def async_get_mutatie_by_afschrift(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Async version of get_mutatie_by_afschrift."""
        query_str = urlencode({k: str(v) for k, v in params.items()})
        url = f"{self.base_url}/v1/api/mutatie/getmutatiebyafschrift?{query_str}"
        async with self._get_async_client() as client:
            resp = await client.get(url)
            return self._check_response(resp)

    async def async_link_file_to_mutation(
        self, mutation_id: int, file_id: int
    ) -> dict[str, Any]:
        """Async version of link_file_to_mutation."""
        url = f"{self.base_url}/v1/api/folderkoppel/koppel/item"
        payload = {
            "koppelId": mutation_id,
            "folders": [{"id": file_id, "soort": "F"}],
            "koppelType": "MUT",
        }
        async with self._get_async_client() as client:
            resp = await client.post(url, json=payload)
            return self._check_response(resp)

    async def async_get_archive_folders(self) -> Any:
        """Async version of get_archive_folders."""
        url = f"{self.base_url}/v1/api/folder/directory"
        async with self._get_async_client() as client:
            resp = await client.get(url)
            return self._check_response(resp)

    async def async_get_archive_files(
        self, folder_id: int, include_deleted: bool = False
    ) -> Any:
        """Async version of get_archive_files."""
        inc_del = str(include_deleted).lower()
        url = f"{self.base_url}/v1/api/folder/{folder_id}/filelist?includeDelete={inc_del}"
        async with self._get_async_client() as client:
            resp = await client.get(url)
            return self._check_response(resp)

    async def async_get_recognized_invoice_model(self, file_id: int) -> Any:
        """Async version of get_recognized_invoice_model."""
        url = f"{self.base_url}/v1/api/mutatie/folder/{file_id}/model"
        async with self._get_async_client() as client:
            resp = await client.get(url)
            return self._check_response(resp)

    async def async_get_mutation_model_by_number(self, mutation_number: int) -> Any:
        """Async version of get_mutation_model_by_number."""
        url = f"{self.base_url}/v1/api/mutatie/nr/{mutation_number}/model"
        async with self._get_async_client() as client:
            resp = await client.get(url)
            return self._check_response(resp)

    async def async_save_web_mutation(self, payload: dict[str, Any]) -> Any:
        """Async version of save_web_mutation (POST for create, PUT when mutatie.nr is set)."""
        url = f"{self.base_url}/v1/api/mutatie"
        is_update = bool(payload.get("mutatie", {}).get("nr"))
        normalized = normalize_web_mutation_payload(payload, is_update=is_update)
        async with self._get_async_client() as client:
            if is_update:
                resp = await client.put(url, json=normalized)
            else:
                resp = await client.post(url, json=normalized)
            return self._check_response(resp)

    async def async_move_archive_files(
        self, file_ids: list[int], target_folder_id: int
    ) -> Any:
        """Async version of move_archive_files."""
        url = f"{self.base_url}/v1/api/folder/bulk/update/{target_folder_id}"
        async with self._get_async_client() as client:
            resp = await client.post(url, json=file_ids)
            return self._check_response(resp)

    async def async_create_archive_folder(
        self, name: str, parent_folder_id: int = 0
    ) -> Any:
        """Async version of create_archive_folder."""
        url = f"{self.base_url}/v1/api/folder/directory"
        payload = {"parentFolderId": parent_folder_id, "name": name}
        async with self._get_async_client() as client:
            resp = await client.post(url, json=payload)
            return self._check_response(resp)

    async def async_upload_archive_file(
        self,
        file_path: str | Path,
        folder_id: int = 0,
        file_name: str | None = None,
        overwrite: bool = True,
    ) -> Any:
        """Async version of upload_archive_file."""
        path = _validate_local_file_path(file_path)
        resolved_name = file_name or path.name
        b64_data = base64.b64encode(path.read_bytes()).decode("ascii")
        url = f"{self.base_url}/v1/api/folder/upload"
        payload = {
            "fileName": resolved_name,
            "data": b64_data,
            "overwrite": overwrite,
            "folderId": folder_id,
        }
        async with self._get_async_client() as client:
            resp = await client.post(url, json=payload)
            return self._check_response(resp)

    async def async_delete_web_mutations(
        self, mutation_numbers: list[int]
    ) -> dict[str, Any]:
        """Async version of delete_web_mutations."""
        grid_url = f"{self.base_url}/v1/api/mutatie/gridtable"
        del_url = f"{self.base_url}/v1/api/mutatie/bulk/delete"

        async with self._get_async_client() as client:
            internal_ids: list[int] = []
            resolved_map: dict[int, int] = {}
            for nr in mutation_numbers:
                resp = await client.post(grid_url, json={"filter": {"nr": str(nr)}})
                grid_data = self._check_response(resp)
                rows = grid_data.get("data", []) if isinstance(grid_data, dict) else []
                for row in rows:
                    if (
                        isinstance(row, list)
                        and len(row) >= 2
                        and str(row[1]) == str(nr)
                    ):
                        internal_ids.append(int(row[0]))
                        resolved_map[int(nr)] = int(row[0])
                        break
            if not internal_ids:
                raise WebSessionError(
                    f"No matching internal mutation IDs found for mutation_numbers={mutation_numbers}"
                )
            del_resp = await client.post(del_url, json=internal_ids)
            del_result = self._check_response(del_resp)
            return {
                "mutation_numbers": mutation_numbers,
                "resolved_ids": resolved_map,
                "response": del_result,
            }

    async def async_get_time_projects(
        self, active_only: bool = False
    ) -> list[dict[str, Any]]:
        """Async version of get_time_projects."""
        active_flag = "true" if active_only else "false"
        getall_url = f"{self.base_url}/v1/api/project/getall/{active_flag}"
        grid_url = f"{self.base_url}/v1/api/project/gridtable?offset=0&limit=50&orderBy=&sortDirection=1"

        async with self._get_async_client() as client:
            resp_all = await client.get(getall_url)
            raw_all = self._check_response(resp_all)
            projects = raw_all if isinstance(raw_all, list) else []

            resp_grid = await client.get(grid_url)
            raw_grid = self._check_response(resp_grid)
            grid_rows = parse_gridtable_rows(raw_grid)
            rel_by_id = {
                int(r["id"]): r.get("relatie")
                for r in grid_rows
                if r.get("id") is not None
            }

            result: list[dict[str, Any]] = []
            for p in projects:
                if not isinstance(p, dict):
                    continue
                pid = int(p.get("id") or 0)
                item = dict(p)
                if pid in rel_by_id:
                    item["relatie"] = rel_by_id[pid]
                result.append(item)
            return result

    async def async_get_time_activities(
        self, active_only: bool = False
    ) -> list[dict[str, Any]]:
        """Async version of get_time_activities."""
        active_flag = "true" if active_only else "false"
        url = f"{self.base_url}/v1/api/activiteit/getall/{active_flag}"
        async with self._get_async_client() as client:
            resp = await client.get(url)
            raw = self._check_response(resp)
            return raw if isinstance(raw, list) else []

    async def async_get_time_entries(
        self,
        *,
        status: str = "uninvoiced",
        project_id: int | None = None,
        activity_id: int | None = None,
        period_start: str | None = None,
        period_end: str | None = None,
        include_non_billable: bool = False,
        include_notes: bool = True,
        max_rows: int = 500,
    ) -> dict[str, Any]:
        """Async version of get_time_entries with automatic 50-row pagination."""
        async with self._get_async_client() as client:
            all_rows: list[dict[str, Any]] = []
            offset = 0
            total_row_count = 0
            while len(all_rows) < max_rows:
                page_limit = min(50, max_rows - len(all_rows))
                url = self._build_time_entries_query(
                    offset=offset,
                    limit=page_limit,
                    status=status,
                    project_id=project_id,
                    activity_id=activity_id,
                    period_start=period_start,
                    period_end=period_end,
                    include_non_billable=include_non_billable,
                    include_notes=include_notes,
                )
                resp = await client.get(url)
                raw = self._check_response(resp)
                total_row_count = int(raw.get("rowCount", 0))
                page_rows = parse_gridtable_rows(raw)
                if not page_rows:
                    break
                all_rows.extend(page_rows)
                offset += len(page_rows)
                if offset >= total_row_count or len(page_rows) < 50:
                    break

        projects = await self.async_get_time_projects(active_only=False)
        activities = await self.async_get_time_activities(active_only=False)
        proj_map = {int(p["id"]): p for p in projects if p.get("id") is not None}
        act_map = {str(a["naam"]): a for a in activities if a.get("naam")}

        summary = summarize_time_entries(all_rows, proj_map, act_map)
        return {
            "status_filter": status,
            "rowCount": total_row_count,
            "fetchedCount": len(all_rows),
            "summary": summary,
            "rows": all_rows,
        }

    async def async_get_time_project(self, project_id: int) -> dict[str, Any]:
        """Fetch a single time project via GET /v1/api/project/{project_id}."""
        url = f"{self.base_url}/v1/api/project/{int(project_id)}"
        async with self._get_async_client() as client:
            resp = await client.get(url)
            return self._check_response(resp)

    async def async_create_time_project(
        self,
        *,
        name: str,
        relation_id: int | None = None,
        active: bool = True,
        invoice_template_id: int | None = None,
        invoicing_method: int = 1,
        group_by: int = 0,
        description_type: int = 1,
        hourly_rate: float = 0.0,
        hourly_rate_in_ex: str = "EX",
        hourly_rate_ledger_id: int | None = None,
        hourly_rate_vat_code: str | None = None,
        km_rate: float = 0.0,
        km_rate_in_ex: str = "EX",
        km_rate_ledger_id: int | None = None,
        km_rate_vat_code: str | None = None,
        cost_center_id: int = 0,
        notes: str = "",
        limit_enabled: bool = False,
        limit_hours: float = 0.0,
        limit_type: int = 0,
        alert_percentage: int = 0,
        alert_user_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        """Create a time project via POST /v1/api/project."""
        need_ledgers = (
            hourly_rate_ledger_id is not None and hourly_rate_ledger_id < 1_000_000
        ) or (km_rate_ledger_id is not None and km_rate_ledger_id < 1_000_000)
        ledgers: list[dict[str, Any]] = []
        async with self._get_async_client() as client:
            if need_ledgers:
                resp_gbr = await client.get(
                    f"{self.base_url}/v1/api/grootboekrekening/selectlist/actief/VWBAL"
                )
                gbr_resp = self._check_response(resp_gbr)
                if isinstance(gbr_resp, list):
                    ledgers = gbr_resp

            resolved_hr_ledger = hourly_rate_ledger_id
            if resolved_hr_ledger is not None and resolved_hr_ledger < 1_000_000:
                resolved_hr_ledger = resolve_revenue_ledger_id(
                    ledgers,
                    ledger_id=hourly_rate_ledger_id,
                    vat_code=hourly_rate_vat_code or "BI_EU_VERK_D",
                )

            resolved_km_ledger = km_rate_ledger_id
            if resolved_km_ledger is not None and resolved_km_ledger < 1_000_000:
                resolved_km_ledger = resolve_revenue_ledger_id(
                    ledgers,
                    ledger_id=km_rate_ledger_id,
                    vat_code=km_rate_vat_code or "BI_EU_VERK_D",
                )

            payload: dict[str, Any] = {
                "id": 0,
                "naam": str(name),
                "actief": bool(active),
                "relatieId": int(relation_id) if relation_id is not None else 0,
                "factuurSjabloonId": (
                    int(invoice_template_id) if invoice_template_id is not None else 0
                ),
                "factuurWijze": int(invoicing_method),
                "factuurGroeperenOp": int(group_by),
                "factuurOmschrijvingType": int(description_type),
                "uurtarief": float(hourly_rate),
                "uurtariefInEx": (
                    "IN" if str(hourly_rate_in_ex).upper() == "IN" else "EX"
                ),
                "uurtariefGrootboekrekeningId": (
                    int(resolved_hr_ledger) if resolved_hr_ledger else 0
                ),
                "uurtariefBtwCode": str(hourly_rate_vat_code or ""),
                "kmtarief": float(km_rate),
                "kmtariefInEx": "IN" if str(km_rate_in_ex).upper() == "IN" else "EX",
                "kmtariefGrootboekrekeningId": (
                    int(resolved_km_ledger) if resolved_km_ledger else 0
                ),
                "kmtariefBtwCode": str(km_rate_vat_code or ""),
                "kostenplaatsId": int(cost_center_id or 0),
                "notities": str(notes or ""),
                "stelLimietIn": bool(limit_enabled),
                "limiet": float(limit_hours),
                "limietType": int(limit_type),
                "stuurMeldingBijPercentage": int(alert_percentage),
                "stuurMeldingGebruikerIds": alert_user_ids or [],
            }
            url = f"{self.base_url}/v1/api/project"
            resp = await client.post(url, json=payload)
            result = self._check_response(resp)
            return (
                result
                if isinstance(result, dict)
                else {"id": result, "project": payload}
            )

    async def async_update_time_project(
        self,
        project_id: int,
        *,
        name: str | None = None,
        active: bool | None = None,
        relation_id: int | None = None,
        invoice_template_id: int | None = None,
        invoicing_method: int | None = None,
        group_by: int | None = None,
        description_type: int | None = None,
        hourly_rate: float | None = None,
        hourly_rate_in_ex: str | None = None,
        hourly_rate_ledger_id: int | None = None,
        hourly_rate_vat_code: str | None = None,
        km_rate: float | None = None,
        km_rate_in_ex: str | None = None,
        km_rate_ledger_id: int | None = None,
        km_rate_vat_code: str | None = None,
        cost_center_id: int | None = None,
        notes: str | None = None,
        limit_enabled: bool | None = None,
        limit_hours: float | None = None,
        limit_type: int | None = None,
        alert_percentage: int | None = None,
        alert_user_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        """Update a time project via PUT /v1/api/project."""
        current = await self.async_get_time_project(project_id)
        payload = dict(current)

        need_ledgers = (
            hourly_rate_ledger_id is not None and hourly_rate_ledger_id < 1_000_000
        ) or (km_rate_ledger_id is not None and km_rate_ledger_id < 1_000_000)
        ledgers: list[dict[str, Any]] = []
        async with self._get_async_client() as client:
            if need_ledgers:
                resp_gbr = await client.get(
                    f"{self.base_url}/v1/api/grootboekrekening/selectlist/actief/VWBAL"
                )
                gbr_resp = self._check_response(resp_gbr)
                if isinstance(gbr_resp, list):
                    ledgers = gbr_resp

            if name is not None:
                payload["naam"] = str(name)
            if active is not None:
                payload["actief"] = bool(active)
            if relation_id is not None:
                payload["relatieId"] = int(relation_id)
            if invoice_template_id is not None:
                payload["factuurSjabloonId"] = int(invoice_template_id)
            if invoicing_method is not None:
                payload["factuurWijze"] = int(invoicing_method)
            if group_by is not None:
                payload["factuurGroeperenOp"] = int(group_by)
            if description_type is not None:
                payload["factuurOmschrijvingType"] = int(description_type)
            if hourly_rate is not None:
                payload["uurtarief"] = float(hourly_rate)
            if hourly_rate_in_ex is not None:
                payload["uurtariefInEx"] = (
                    "IN" if str(hourly_rate_in_ex).upper() == "IN" else "EX"
                )
            if hourly_rate_ledger_id is not None:
                hr_lid = hourly_rate_ledger_id
                if hr_lid < 1_000_000:
                    hr_lid = resolve_revenue_ledger_id(
                        ledgers,
                        ledger_id=hr_lid,
                        vat_code=hourly_rate_vat_code
                        or payload.get("uurtariefBtwCode")
                        or "BI_EU_VERK_D",
                    )
                payload["uurtariefGrootboekrekeningId"] = int(hr_lid)
            if hourly_rate_vat_code is not None:
                payload["uurtariefBtwCode"] = str(hourly_rate_vat_code)
            if km_rate is not None:
                payload["kmtarief"] = float(km_rate)
            if km_rate_in_ex is not None:
                payload["kmtariefInEx"] = (
                    "IN" if str(km_rate_in_ex).upper() == "IN" else "EX"
                )
            if km_rate_ledger_id is not None:
                km_lid = km_rate_ledger_id
                if km_lid < 1_000_000:
                    km_lid = resolve_revenue_ledger_id(
                        ledgers,
                        ledger_id=km_lid,
                        vat_code=km_rate_vat_code
                        or payload.get("kmtariefBtwCode")
                        or "BI_EU_VERK_D",
                    )
                payload["kmtariefGrootboekrekeningId"] = int(km_lid)
            if km_rate_vat_code is not None:
                payload["kmtariefBtwCode"] = str(km_rate_vat_code)
            if cost_center_id is not None:
                payload["kostenplaatsId"] = int(cost_center_id)
            if notes is not None:
                payload["notities"] = str(notes)
            if limit_enabled is not None:
                payload["stelLimietIn"] = bool(limit_enabled)
            if limit_hours is not None:
                payload["limiet"] = float(limit_hours)
            if limit_type is not None:
                payload["limietType"] = int(limit_type)
            if alert_percentage is not None:
                payload["stuurMeldingBijPercentage"] = int(alert_percentage)
            if alert_user_ids is not None:
                payload["stuurMeldingGebruikerIds"] = alert_user_ids

            url = f"{self.base_url}/v1/api/project"
            resp = await client.put(url, json=payload)
            result = self._check_response(resp)
            return (
                result
                if isinstance(result, dict)
                else {"id": int(project_id), "project": payload}
            )

    async def async_get_time_entry(self, entry_id: int) -> dict[str, Any]:
        """Fetch a single time entry via GET /v1/api/uur/{entry_id}."""
        url = f"{self.base_url}/v1/api/uur/{int(entry_id)}"
        async with self._get_async_client() as client:
            resp = await client.get(url)
            return self._check_response(resp)

    async def async_create_time_entry(
        self,
        *,
        project_id: int,
        activity_id: int,
        hours: float,
        date: str | dict[str, int] | None = None,
        employee_id: int | None = None,
        notes: str = "",
        billable: bool = True,
        kilometers: float = 0.0,
    ) -> dict[str, Any]:
        """Create a time entry via POST /v1/api/uur."""
        resolved_emp_id = employee_id
        async with self._get_async_client() as client:
            if resolved_emp_id is None:
                try:
                    users_resp = self._check_response(
                        await client.get(f"{self.base_url}/v1/api/user/selectlist")
                    )
                    if (
                        isinstance(users_resp, list)
                        and users_resp
                        and users_resp[0].get("id")
                    ):
                        resolved_emp_id = int(users_resp[0]["id"])
                except Exception:
                    resolved_emp_id = 0

            datum_dict = parse_eb_date_dict(date, default_offset_days=0)
            payload: dict[str, Any] = {
                "id": 0,
                "datum": datum_dict,
                "gebruikerId": int(resolved_emp_id or 0),
                "projectId": int(project_id),
                "activiteitId": int(activity_id),
                "aantalUren": float(hours),
                "opmerkingen": str(notes or ""),
                "aantalKilometers": float(kilometers or 0.0),
            }
            url = f"{self.base_url}/v1/api/uur"
            resp = await client.post(url, json=payload)
            result = self._check_response(resp)
            return (
                result if isinstance(result, dict) else {"id": result, "entry": payload}
            )

    async def async_update_time_entry(
        self,
        entry_id: int,
        *,
        date: str | dict[str, int] | None = None,
        hours: float | None = None,
        project_id: int | None = None,
        activity_id: int | None = None,
        employee_id: int | None = None,
        notes: str | None = None,
        billable: bool | None = None,
        kilometers: float | None = None,
    ) -> dict[str, Any]:
        """Update an existing time entry via PUT /v1/api/uur."""
        current = await self.async_get_time_entry(entry_id)
        payload = dict(current)
        if date is not None:
            payload["datum"] = parse_eb_date_dict(date)
        if hours is not None:
            payload["aantalUren"] = float(hours)
            payload.pop("aantal", None)
        if project_id is not None:
            payload["projectId"] = int(project_id)
        if activity_id is not None:
            payload["activiteitId"] = int(activity_id)
        if employee_id is not None:
            payload["gebruikerId"] = int(employee_id)
            payload.pop("medewerkerId", None)
        if notes is not None:
            payload["opmerkingen"] = str(notes)
        if kilometers is not None:
            payload["aantalKilometers"] = float(kilometers)
            payload.pop("kilometers", None)

        url = f"{self.base_url}/v1/api/uur"
        async with self._get_async_client() as client:
            resp = await client.put(url, json=payload)
            result = self._check_response(resp)
            return (
                result
                if isinstance(result, dict)
                else {"id": int(entry_id), "entry": payload}
            )

    async def async_delete_time_entry(self, entry_id: int) -> dict[str, Any]:
        """Delete a time entry via DELETE /v1/api/uur/{entry_id}."""
        url = f"{self.base_url}/v1/api/uur/{int(entry_id)}"
        async with self._get_async_client() as client:
            resp = await client.delete(url)
            res = self._check_response(resp)
            return {"success": True, "entry_id": int(entry_id), "response": res}

    async def async_invoice_time_entries(
        self,
        entry_ids: list[int],
        *,
        date: str | None = None,
        group_by: int = 4,
        description_type: int = 2,
        process_directly: bool = True,
        send_directly: bool = False,
        reference: str | None = None,
        invoice_text: str | None = None,
    ) -> dict[str, Any]:
        """Convert time entries into an invoice via POST /v1/api/uur/factureren."""
        if date:
            date_str = format_eb_date_dict(parse_eb_date_dict(date)) or str(date)[:10]
        else:
            date_str = dt.date.today().isoformat()

        payload: dict[str, Any] = {
            "ids": [int(eid) for eid in entry_ids],
            "groeperenOp": int(group_by),
            "datum": date_str,
            "omschrijvingType": int(description_type),
            "verwerkDirect": bool(process_directly),
            "verstuurDirect": bool(send_directly),
        }
        url = f"{self.base_url}/v1/api/uur/factureren"
        async with self._get_async_client() as client:
            resp = await client.post(url, json=payload)
            result = self._check_response(resp)

        if isinstance(result, dict):
            error_count = result.get("errorCount", 0)
            error_messages = result.get("errorMessages") or []
            if error_count > 0 or error_messages:
                raise WebSessionError(
                    f"Failed to invoice time entries: {', '.join(str(m) for m in error_messages) or 'errorCount > 0'}"
                )

        created_ids: list[int] = []
        if isinstance(result, dict) and isinstance(result.get("ids"), list):
            created_ids = [int(i) for i in result["ids"]]

        updated_invoices: list[dict[str, Any]] = []
        if (reference is not None or invoice_text is not None) and created_ids:
            for inv_id in created_ids:
                upd = await self.async_update_invoice(
                    invoice_id=inv_id,
                    reference=reference,
                    invoice_text=invoice_text,
                )
                updated_invoices.append(upd)

        return {
            "success": True,
            "invoice_ids": created_ids,
            "reference": reference,
            "invoice_text": invoice_text,
            "response": result,
            "updated_invoices": updated_invoices,
        }

    async def async_update_invoice(
        self,
        invoice_id: int,
        *,
        reference: str | None = None,
        invoice_text: str | None = None,
        date: str | None = None,
        payment_term_days: int | None = None,
        template_id: int | None = None,
        internal_note: str | None = None,
    ) -> dict[str, Any]:
        """Update invoice header fields via PUT /v1/api/factuur/model and trigger PDF regeneration."""
        async with self._get_async_client() as client:
            get_url = f"{self.base_url}/v1/api/factuur/{int(invoice_id)}"
            resp = await client.get(get_url)
            raw_data = self._check_response(resp)

            factuur_obj = (
                dict(raw_data["factuur"])
                if isinstance(raw_data, dict) and "factuur" in raw_data
                else dict(raw_data)
            )

            if reference is not None:
                factuur_obj["kenmerk"] = str(reference)
            if invoice_text is not None:
                factuur_obj["factuurtekst"] = str(invoice_text)
            if date is not None:
                factuur_obj["datum"] = parse_eb_date_dict(date)
            if payment_term_days is not None:
                factuur_obj["betalingstermijn"] = int(payment_term_days)
            if template_id is not None:
                factuur_obj["sjabloonId"] = int(template_id)
            if internal_note is not None:
                factuur_obj["notitie"] = str(internal_note)

            model_payload = {
                "factuur": factuur_obj,
                "factuurItems": {"insert": [], "update": [], "delete": []},
            }
            put_url = f"{self.base_url}/v1/api/factuur/model"
            put_resp = await client.put(put_url, json=model_payload)
            model_res = self._check_response(put_resp)

            # Schedule PDF regeneration
            sched_url = f"{self.base_url}/v1/api/factuur/pdfinvoiceschedule?factuurId={int(invoice_id)}"
            try:
                sched_resp = await client.get(sched_url)
                self._check_response(sched_resp)
            except Exception as sched_err:
                logger.warning(
                    "Could not schedule PDF invoice regeneration: %s", sched_err
                )

            return {
                "success": True,
                "invoice_id": int(invoice_id),
                "reference": factuur_obj.get("kenmerk"),
                "invoice_text": factuur_obj.get("factuurtekst"),
                "response": model_res,
            }

    async def async_get_quote_metadata(self) -> dict[str, Any]:
        """Async version of get_quote_metadata."""
        async with self._get_async_client() as client:
            statuses = self._check_response(
                await client.get(f"{self.base_url}/v1/api/offertestatus")
            )
            templates = self._check_response(
                await client.get(f"{self.base_url}/v1/api/offertesjabloon/selectlist")
            )
            users = self._check_response(
                await client.get(f"{self.base_url}/v1/api/user/selectlist")
            )
            next_nr = self._check_response(
                await client.get(f"{self.base_url}/v1/api/offerte/nummer/next")
            )
            return {
                "statuses": statuses if isinstance(statuses, list) else [],
                "templates": templates if isinstance(templates, list) else [],
                "users": users if isinstance(users, list) else [],
                "next_quote_number": str(next_nr) if next_nr is not None else "",
            }

    async def async_list_quotes(
        self,
        *,
        offset: int = 0,
        limit: int = 50,
        quote_number: str | None = None,
        relation_id: int | None = None,
        status_id: int | None = None,
        assigned_user_id: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        expiration_from: str | None = None,
        expiration_to: str | None = None,
        include_metadata: bool = True,
    ) -> dict[str, Any]:
        """Async version of list_quotes."""
        url = self._build_quotes_query(
            offset=offset,
            limit=limit,
            quote_number=quote_number,
            relation_id=relation_id,
            status_id=status_id,
            assigned_user_id=assigned_user_id,
            date_from=date_from,
            date_to=date_to,
            expiration_from=expiration_from,
            expiration_to=expiration_to,
        )
        async with self._get_async_client() as client:
            resp = await client.get(url)
            raw = self._check_response(resp)

        rows = parse_gridtable_rows(raw)
        total_excl = round(sum(float(r.get("totaalExcl") or 0.0) for r in rows), 2)
        total_vat = round(sum(float(r.get("totaalbtw") or 0.0) for r in rows), 2)
        total_incl = round(sum(float(r.get("totaalIncl") or 0.0) for r in rows), 2)
        result: dict[str, Any] = {
            "rowCount": int(raw.get("rowCount", len(rows))),
            "fetchedCount": len(rows),
            "summary": {
                "total_excl_vat": total_excl,
                "total_vat": total_vat,
                "total_incl_vat": total_incl,
            },
            "quotes": rows,
        }
        if include_metadata:
            result["metadata"] = await self.async_get_quote_metadata()
        return result

    async def async_get_quote(
        self,
        quote_id: int | None = None,
        quote_number: str | None = None,
    ) -> dict[str, Any]:
        """Async version of get_quote."""
        resolved_id = quote_id
        if resolved_id is None:
            if not quote_number:
                raise ValueError("Either quote_id or quote_number must be provided.")
            listing = await self.async_list_quotes(
                quote_number=quote_number, include_metadata=False
            )
            for q in listing.get("quotes", []):
                if (
                    str(q.get("nummer") or "").strip().lower()
                    == quote_number.strip().lower()
                ):
                    resolved_id = int(q["id"])
                    break
            if resolved_id is None and listing.get("quotes"):
                resolved_id = int(listing["quotes"][0]["id"])
            if resolved_id is None:
                raise WebSessionError(
                    f"No quote found matching quote_number='{quote_number}'"
                )

        qid = int(resolved_id)

        async with self._get_async_client() as client:
            model = self._check_response(
                await client.get(f"{self.base_url}/v1/api/offerte/{qid}")
            )
            dashboard = self._check_response(
                await client.get(f"{self.base_url}/v1/api/offerte/dashboard/{qid}")
            )
            in_ex = str((model or {}).get("inEx") or "EX").upper()
            is_incl_str = "true" if in_ex == "IN" else "false"
            regels_raw = self._check_response(
                await client.get(
                    f"{self.base_url}/v1/api/offerteregel/gridtable/{qid}"
                    f"?isInclBtw={is_incl_str}&addArtikelVoorraad=false"
                )
            )
            files_raw = self._check_response(
                await client.get(
                    f"{self.base_url}/v1/api/folder/gekoppeldgridtable"
                    f"?offset=0&limit=50&orderBy=&sortDirection=1&koppelId={qid}&koppelSoort=OF"
                )
            )
            items = parse_gridtable_rows(regels_raw)
            attached_files = parse_gridtable_rows(files_raw)
            return {
                "id": qid,
                "quote_number": model.get("nummer") or dashboard.get("nummer"),
                "status": dashboard.get("status"),
                "status_id": model.get("statusId"),
                "relation_id": model.get("relatieId") or dashboard.get("relatieId"),
                "relation_code": dashboard.get("relatieCode"),
                "relation_name": dashboard.get("relatieBedrijf"),
                "date": format_eb_date_dict(model.get("datum"))
                or format_eb_date_dict(dashboard.get("datum")),
                "expiration_date": format_eb_date_dict(model.get("vervaldatum"))
                or format_eb_date_dict(dashboard.get("vervalDatum")),
                "reference": model.get("kenmerk")
                or dashboard.get("kenmerkKlant")
                or "",
                "in_ex_vat": in_ex,
                "template_id": model.get("sjabloonId"),
                "assigned_user_id": model.get("behandelGebruikerId"),
                "assigned_user": dashboard.get("inBehandelingBij"),
                "public_note": model.get("notitiePubliek") or "",
                "internal_note": model.get("notitieIntern") or "",
                "total_excl_vat": float(model.get("totaalExcl") or 0.0),
                "total_vat": float(model.get("totaalBtw") or 0.0),
                "total_incl_vat": float(model.get("totaalIncl") or 0.0),
                "items": items,
                "attached_files": attached_files,
                "model": model,
                "dashboard": dashboard,
            }

    async def async_link_file_to_quote(
        self, quote_id: int, file_id: int
    ) -> dict[str, Any]:
        """Async version of link_file_to_quote."""
        url = f"{self.base_url}/v1/api/folderkoppel/koppel/item"
        payload = {
            "koppelId": int(quote_id),
            "folders": [{"id": int(file_id), "soort": "F"}],
            "koppelType": "OF",
        }
        async with self._get_async_client() as client:
            resp = await client.post(url, json=payload)
            return self._check_response(resp)

    async def async_create_quote(
        self,
        *,
        relation_id: int,
        items: list[dict[str, Any]] | None = None,
        quote_number: str | None = None,
        date: str | None = None,
        expiration_date: str | None = None,
        expiration_days: int = 30,
        status_id: int | None = None,
        status: str | None = None,
        template_id: int | None = None,
        assigned_user_id: int | None = None,
        in_ex_vat: str = "EX",
        reference: str = "",
        public_note: str = "",
        internal_note: str = "",
        invoice_email_template_id: int | str | None = None,
        archive_file_id: int | None = None,
    ) -> dict[str, Any]:
        """Async version of create_quote."""
        meta = await self.async_get_quote_metadata()
        resolved_status_id = resolve_quote_status_id(
            meta["statuses"], status_id=status_id, status=status
        )
        resolved_number = (
            quote_number.strip()
            if quote_number and quote_number.strip()
            else meta["next_quote_number"]
        )
        if not resolved_number:
            raise WebSessionError(
                "Could not determine quote_number (/v1/api/offerte/nummer/next returned empty)."
            )

        resolved_template_id = template_id
        if resolved_template_id is None:
            active_templates = [
                t for t in meta["templates"] if not t.get("inactief")
            ] or meta["templates"]
            if active_templates and active_templates[0].get("id") is not None:
                resolved_template_id = int(active_templates[0]["id"])
        if resolved_template_id is None:
            raise WebSessionError(
                "No quote template (offertesjabloon) found in e-Boekhouden."
            )

        resolved_user_id = assigned_user_id
        if resolved_user_id is None and meta["users"]:
            resolved_user_id = int(meta["users"][0]["id"])

        datum_dict = parse_eb_date_dict(date, default_offset_days=0)
        verval_dict = parse_eb_date_dict(
            expiration_date, base_date=datum_dict, default_offset_days=expiration_days
        )
        norm_in_ex = "IN" if str(in_ex_vat or "EX").upper() == "IN" else "EX"

        header_payload: dict[str, Any] = {
            "statusId": int(resolved_status_id),
            "nummer": str(resolved_number),
            "behandelGebruikerId": resolved_user_id,
            "relatieId": int(relation_id),
            "kenmerk": str(reference or "")[:50],
            "datum": datum_dict,
            "vervaldatum": verval_dict,
            "sjabloonId": int(resolved_template_id),
            "factuurEmailsjabloonId": (
                invoice_email_template_id
                if invoice_email_template_id is not None
                else ""
            ),
            "notitiePubliek": str(public_note or ""),
            "notitieIntern": str(internal_note or ""),
            "inEx": norm_in_ex,
        }

        async with self._get_async_client() as client:
            create_resp = self._check_response(
                await client.post(
                    f"{self.base_url}/v1/api/offerte",
                    json=header_payload,
                )
            )
            created_id = int(
                create_resp["id"]
                if isinstance(create_resp, dict) and "id" in create_resp
                else create_resp
            )

            if items:
                need_ledgers = any(
                    not (it.get("ledger_id") or it.get("grootboekId"))
                    or int(it.get("ledger_id") or it.get("grootboekId") or 0)
                    < 1_000_000
                    for it in items
                )
                ledgers: list[dict[str, Any]] = []
                if need_ledgers:
                    gbr_resp = self._check_response(
                        await client.get(
                            f"{self.base_url}/v1/api/grootboekrekening/selectlist/actief/VWBAL"
                        )
                    )
                    if isinstance(gbr_resp, list):
                        ledgers = gbr_resp

                for idx, item_data in enumerate(items, start=1):
                    line_payload = build_quote_line_payload(
                        item_data,
                        quote_id=created_id,
                        in_ex_vat=norm_in_ex,
                        volgorde=idx,
                        ledgers=ledgers,
                    )
                    self._check_response(
                        await client.post(
                            f"{self.base_url}/v1/api/offerteregel",
                            json=line_payload,
                        )
                    )

        if archive_file_id is not None:
            await self.async_link_file_to_quote(
                quote_id=created_id, file_id=int(archive_file_id)
            )

        return await self.async_get_quote(quote_id=created_id)

    async def async_update_quote(
        self,
        *,
        quote_id: int,
        relation_id: int | None = None,
        quote_number: str | None = None,
        date: str | None = None,
        expiration_date: str | None = None,
        status_id: int | None = None,
        status: str | None = None,
        template_id: int | None = None,
        assigned_user_id: int | None = None,
        in_ex_vat: str | None = None,
        reference: str | None = None,
        public_note: str | None = None,
        internal_note: str | None = None,
        items: list[dict[str, Any]] | None = None,
        replace_items: bool = True,
        archive_file_id: int | None = None,
    ) -> dict[str, Any]:
        """Async version of update_quote."""
        qid = int(quote_id)

        async with self._get_async_client() as client:
            existing = self._check_response(
                await client.get(f"{self.base_url}/v1/api/offerte/{qid}")
            )
            if not isinstance(existing, dict):
                raise WebSessionError(f"Quote {qid} not found.")

            resolved_status_id = existing.get("statusId")
            if status_id is not None or status is not None:
                statuses = self._check_response(
                    await client.get(f"{self.base_url}/v1/api/offertestatus")
                )
                resolved_status_id = resolve_quote_status_id(
                    statuses if isinstance(statuses, list) else [],
                    status_id=status_id,
                    status=status,
                )

            datum_dict = (
                parse_eb_date_dict(date) if date is not None else existing.get("datum")
            )
            verval_dict = (
                parse_eb_date_dict(expiration_date)
                if expiration_date is not None
                else existing.get("vervaldatum")
            )
            norm_in_ex = (
                ("IN" if str(in_ex_vat).upper() == "IN" else "EX")
                if in_ex_vat is not None
                else str(existing.get("inEx") or "EX")
            )

            update_payload: dict[str, Any] = {
                "id": qid,
                "statusId": int(resolved_status_id or 0),
                "nummer": (
                    str(quote_number)
                    if quote_number is not None
                    else existing.get("nummer")
                ),
                "behandelGebruikerId": (
                    int(assigned_user_id)
                    if assigned_user_id is not None
                    else existing.get("behandelGebruikerId")
                ),
                "relatieId": (
                    int(relation_id)
                    if relation_id is not None
                    else existing.get("relatieId")
                ),
                "kenmerk": (
                    str(reference)[:50]
                    if reference is not None
                    else (existing.get("kenmerk") or "")
                ),
                "datum": datum_dict,
                "vervaldatum": verval_dict,
                "sjabloonId": (
                    int(template_id)
                    if template_id is not None
                    else existing.get("sjabloonId")
                ),
                "factuurEmailsjabloonId": existing.get("factuurEmailsjabloonId", ""),
                "notitiePubliek": (
                    str(public_note)
                    if public_note is not None
                    else (existing.get("notitiePubliek") or "")
                ),
                "notitieIntern": (
                    str(internal_note)
                    if internal_note is not None
                    else (existing.get("notitieIntern") or "")
                ),
                "inEx": norm_in_ex,
            }

            self._check_response(
                await client.put(
                    f"{self.base_url}/v1/api/offerte",
                    json=update_payload,
                )
            )

            if items is not None:
                is_incl_str = "true" if norm_in_ex == "IN" else "false"
                regels_raw = self._check_response(
                    await client.get(
                        f"{self.base_url}/v1/api/offerteregel/gridtable/{qid}"
                        f"?isInclBtw={is_incl_str}&addArtikelVoorraad=false"
                    )
                )
                existing_rows = parse_gridtable_rows(regels_raw)
                start_volgorde = len(existing_rows) + 1
                if replace_items and existing_rows:
                    for row in existing_rows:
                        if row.get("id") is not None:
                            self._check_response(
                                await client.delete(
                                    f"{self.base_url}/v1/api/offerteregel/{int(row['id'])}"
                                )
                            )
                    start_volgorde = 1

                if items:
                    need_ledgers = any(
                        not (it.get("ledger_id") or it.get("grootboekId"))
                        or int(it.get("ledger_id") or it.get("grootboekId") or 0)
                        < 1_000_000
                        for it in items
                    )
                    ledgers: list[dict[str, Any]] = []
                    if need_ledgers:
                        gbr_resp = self._check_response(
                            await client.get(
                                f"{self.base_url}/v1/api/grootboekrekening/selectlist/actief/VWBAL"
                            )
                        )
                        if isinstance(gbr_resp, list):
                            ledgers = gbr_resp

                    for idx, item_data in enumerate(items, start=start_volgorde):
                        line_payload = build_quote_line_payload(
                            item_data,
                            quote_id=qid,
                            in_ex_vat=norm_in_ex,
                            volgorde=idx,
                            ledgers=ledgers,
                        )
                        self._check_response(
                            await client.post(
                                f"{self.base_url}/v1/api/offerteregel",
                                json=line_payload,
                            )
                        )

        if archive_file_id is not None:
            await self.async_link_file_to_quote(
                quote_id=qid, file_id=int(archive_file_id)
            )

        return await self.async_get_quote(quote_id=qid)

    async def async_delete_quote(self, quote_id: int) -> dict[str, Any]:
        """Async version of delete_quote."""
        qid = int(quote_id)
        url = f"{self.base_url}/v1/api/offerte/{qid}"
        async with self._get_async_client() as client:
            resp = await client.delete(url)
            result = self._check_response(resp)
            return {
                "quote_id": qid,
                "deleted": True,
                "response": result,
            }

    async def async_get_invoice_mail_data(
        self, invoice_id: int, template_id: int | None = None
    ) -> dict[str, Any]:
        """Async version of get_invoice_mail_data."""
        if template_id is not None:
            url = f"{self.base_url}/v1/api/factuur/maildata/{int(template_id)}/{int(invoice_id)}"
        else:
            url = f"{self.base_url}/v1/api/factuur/{int(invoice_id)}/maildata"

        async with self._get_async_client() as client:
            resp = await client.get(url)
            result = self._check_response(resp)
            return result if isinstance(result, dict) else {}

    async def async_send_invoice_email(
        self,
        invoice_id: int,
        recipient_email: str | None = None,
        sender_email: str | None = None,
        sender_name: str | None = None,
        subject: str | None = None,
        body: str | None = None,
        cc_email: str | None = None,
        bcc_email: str | None = None,
        attach_ubl: bool = True,
        email_template_id: int | None = None,
    ) -> dict[str, Any]:
        """Async version of send_invoice_email."""
        mail_data: dict[str, Any] = {}
        if email_template_id is not None or not (recipient_email and subject and body):
            try:
                mail_data = await self.async_get_invoice_mail_data(
                    invoice_id=invoice_id, template_id=email_template_id
                )
            except Exception:
                mail_data = {}

        resolved_recipient = recipient_email
        if not resolved_recipient:
            resolved_recipient = mail_data.get("emailAan")
            if not resolved_recipient and isinstance(mail_data.get("to"), list):
                emails = [
                    str(t["email"])
                    for t in mail_data["to"]
                    if isinstance(t, dict) and t.get("email")
                ]
                if emails:
                    resolved_recipient = ", ".join(emails)

        if not resolved_recipient:
            raise ValueError(
                f"No recipient email address found for invoice {invoice_id}. "
                "Specify recipient_email explicitly."
            )

        resolved_subject = (
            subject or mail_data.get("subject") or f"Factuur {invoice_id}"
        )
        resolved_body = body or mail_data.get("body") or ""
        resolved_from_email = (
            sender_email or mail_data.get("emailVan") or mail_data.get("vanEmail") or ""
        )
        resolved_from_name = (
            sender_name or mail_data.get("naam") or mail_data.get("vanNaam") or ""
        )
        resolved_cc = cc_email or mail_data.get("ccEmail") or mail_data.get("cc") or ""
        resolved_bcc = (
            bcc_email or mail_data.get("bccEmail") or mail_data.get("bcc") or ""
        )

        payload: dict[str, Any] = {
            "factuurId": int(invoice_id),
            "aanEmail": str(resolved_recipient),
            "vanEmail": str(resolved_from_email),
            "vanNaam": str(resolved_from_name),
            "ccEmail": str(resolved_cc),
            "bccEmail": str(resolved_bcc),
            "subject": str(resolved_subject),
            "body": str(resolved_body),
            "factuurUbl": bool(attach_ubl),
            "rememberMessage": False,
            "useEmailSjabloon": False,
            "bijlagen": [],
        }

        url = f"{self.base_url}/v1/api/factuur/mail"
        async with self._get_async_client() as client:
            resp = await client.post(url, json=payload)
            result = self._check_response(resp)

        if isinstance(result, dict):
            error_count = result.get("errorCount", 0)
            error_msg = result.get("errorMessage")
            if (error_count and error_count > 0) or error_msg:
                raise WebSessionError(
                    f"Failed to send invoice email for invoice {invoice_id}: {error_msg or 'errorCount > 0'}"
                )

        return {
            "success": True,
            "invoice_id": int(invoice_id),
            "recipient_email": str(resolved_recipient),
            "from_email": str(resolved_from_email),
            "from_name": str(resolved_from_name),
            "email_template_id": email_template_id,
            "subject": str(resolved_subject),
            "body": str(resolved_body),
            "attach_ubl": bool(attach_ubl),
            "response": result,
        }
