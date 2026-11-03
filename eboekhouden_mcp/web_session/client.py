"""Modular web session client for e-Boekhouden using cookie-based authentication."""

from __future__ import annotations

import base64
import copy
import datetime as dt
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx


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


class WebSessionClient:
    """Client for e-Boekhouden web session API (secure20 / secure)."""

    def __init__(
        self,
        auth_token: str | None = None,
        base_url: str = "https://secure20.e-boekhouden.nl",
        timeout: float = 30.0,
        http_client: httpx.Client | None = None,
        async_http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.auth_token = auth_token.strip() if auth_token else ""
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._http = http_client
        self._async_http = async_http_client

    def set_token(self, token: str) -> None:
        """Update the web session auth token."""
        self.auth_token = token.strip()

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

    def _get_sync_client(self) -> httpx.Client:
        if self._http is not None:
            return self._http
        return httpx.Client(
            base_url=self.base_url,
            headers=self._get_headers(),
            timeout=self.timeout,
        )

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
                    t.get("email")
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
                "subject": str(resolved_subject),
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
        if self._async_http is not None:
            resp = await self._async_http.get(url, headers=self._get_headers())
            raw = self._check_response(resp)
            return {
                "rowCount": raw.get("rowCount", 0),
                "rows": parse_import_grid(raw),
                "raw": raw,
            }

        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._get_headers(),
            timeout=self.timeout,
        ) as client:
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
        if self._async_http is not None:
            resp = await self._async_http.get(url, headers=self._get_headers())
            return self._check_response(resp)

        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._get_headers(),
            timeout=self.timeout,
        ) as client:
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
        if self._async_http is not None:
            resp = await self._async_http.post(
                url, json=payload, headers=self._get_headers()
            )
            return self._check_response(resp)

        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._get_headers(),
            timeout=self.timeout,
        ) as client:
            resp = await client.post(url, json=payload)
            return self._check_response(resp)

    async def async_get_archive_folders(self) -> Any:
        """Async version of get_archive_folders."""
        url = f"{self.base_url}/v1/api/folder/directory"
        if self._async_http is not None:
            resp = await self._async_http.get(url, headers=self._get_headers())
            return self._check_response(resp)

        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._get_headers(),
            timeout=self.timeout,
        ) as client:
            resp = await client.get(url)
            return self._check_response(resp)

    async def async_get_archive_files(
        self, folder_id: int, include_deleted: bool = False
    ) -> Any:
        """Async version of get_archive_files."""
        inc_del = str(include_deleted).lower()
        url = f"{self.base_url}/v1/api/folder/{folder_id}/filelist?includeDelete={inc_del}"
        if self._async_http is not None:
            resp = await self._async_http.get(url, headers=self._get_headers())
            return self._check_response(resp)

        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._get_headers(),
            timeout=self.timeout,
        ) as client:
            resp = await client.get(url)
            return self._check_response(resp)

    async def async_get_recognized_invoice_model(self, file_id: int) -> Any:
        """Async version of get_recognized_invoice_model."""
        url = f"{self.base_url}/v1/api/mutatie/folder/{file_id}/model"
        if self._async_http is not None:
            resp = await self._async_http.get(url, headers=self._get_headers())
            return self._check_response(resp)

        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._get_headers(),
            timeout=self.timeout,
        ) as client:
            resp = await client.get(url)
            return self._check_response(resp)

    async def async_get_mutation_model_by_number(self, mutation_number: int) -> Any:
        """Async version of get_mutation_model_by_number."""
        url = f"{self.base_url}/v1/api/mutatie/nr/{mutation_number}/model"
        if self._async_http is not None:
            resp = await self._async_http.get(url, headers=self._get_headers())
            return self._check_response(resp)

        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._get_headers(),
            timeout=self.timeout,
        ) as client:
            resp = await client.get(url)
            return self._check_response(resp)

    async def async_save_web_mutation(self, payload: dict[str, Any]) -> Any:
        """Async version of save_web_mutation (POST for create, PUT when mutatie.nr is set)."""
        url = f"{self.base_url}/v1/api/mutatie"
        is_update = bool(payload.get("mutatie", {}).get("nr"))
        normalized = normalize_web_mutation_payload(payload, is_update=is_update)
        if self._async_http is not None:
            if is_update:
                resp = await self._async_http.put(
                    url, json=normalized, headers=self._get_headers()
                )
            else:
                resp = await self._async_http.post(
                    url, json=normalized, headers=self._get_headers()
                )
            return self._check_response(resp)

        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._get_headers(),
            timeout=self.timeout,
        ) as client:
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
        if self._async_http is not None:
            resp = await self._async_http.post(
                url, json=file_ids, headers=self._get_headers()
            )
            return self._check_response(resp)

        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._get_headers(),
            timeout=self.timeout,
        ) as client:
            resp = await client.post(url, json=file_ids)
            return self._check_response(resp)

    async def async_create_archive_folder(
        self, name: str, parent_folder_id: int = 0
    ) -> Any:
        """Async version of create_archive_folder."""
        url = f"{self.base_url}/v1/api/folder/directory"
        payload = {"parentFolderId": parent_folder_id, "name": name}
        if self._async_http is not None:
            resp = await self._async_http.post(
                url, json=payload, headers=self._get_headers()
            )
            return self._check_response(resp)

        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._get_headers(),
            timeout=self.timeout,
        ) as client:
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
        if self._async_http is not None:
            resp = await self._async_http.post(
                url, json=payload, headers=self._get_headers()
            )
            return self._check_response(resp)

        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._get_headers(),
            timeout=self.timeout,
        ) as client:
            resp = await client.post(url, json=payload)
            return self._check_response(resp)

    async def async_delete_web_mutations(
        self, mutation_numbers: list[int]
    ) -> dict[str, Any]:
        """Async version of delete_web_mutations."""
        grid_url = f"{self.base_url}/v1/api/mutatie/gridtable"
        del_url = f"{self.base_url}/v1/api/mutatie/bulk/delete"

        async def _execute_delete(
            client_obj: httpx.AsyncClient, use_headers: bool
        ) -> dict[str, Any]:
            hdrs = self._get_headers() if use_headers else None
            internal_ids: list[int] = []
            resolved_map: dict[int, int] = {}
            for nr in mutation_numbers:
                resp = await client_obj.post(
                    grid_url, json={"filter": {"nr": str(nr)}}, headers=hdrs
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
            del_resp = await client_obj.post(del_url, json=internal_ids, headers=hdrs)
            del_result = self._check_response(del_resp)
            return {
                "mutation_numbers": mutation_numbers,
                "resolved_ids": resolved_map,
                "response": del_result,
            }

        if self._async_http is not None:
            return await _execute_delete(self._async_http, use_headers=True)

        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._get_headers(),
            timeout=self.timeout,
        ) as client:
            return await _execute_delete(client, use_headers=False)

    async def async_get_time_projects(
        self, active_only: bool = False
    ) -> list[dict[str, Any]]:
        """Async version of get_time_projects."""
        active_flag = "true" if active_only else "false"
        getall_url = f"{self.base_url}/v1/api/project/getall/{active_flag}"
        grid_url = f"{self.base_url}/v1/api/project/gridtable?offset=0&limit=50&orderBy=&sortDirection=1"

        async def _fetch(
            client_obj: httpx.AsyncClient, use_headers: bool
        ) -> list[dict[str, Any]]:
            hdrs = self._get_headers() if use_headers else None
            resp_all = await client_obj.get(getall_url, headers=hdrs)
            raw_all = self._check_response(resp_all)
            projects = raw_all if isinstance(raw_all, list) else []

            resp_grid = await client_obj.get(grid_url, headers=hdrs)
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

        if self._async_http is not None:
            return await _fetch(self._async_http, use_headers=True)

        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._get_headers(),
            timeout=self.timeout,
        ) as client:
            return await _fetch(client, use_headers=False)

    async def async_get_time_activities(
        self, active_only: bool = False
    ) -> list[dict[str, Any]]:
        """Async version of get_time_activities."""
        active_flag = "true" if active_only else "false"
        url = f"{self.base_url}/v1/api/activiteit/getall/{active_flag}"
        if self._async_http is not None:
            resp = await self._async_http.get(url, headers=self._get_headers())
            raw = self._check_response(resp)
            return raw if isinstance(raw, list) else []

        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._get_headers(),
            timeout=self.timeout,
        ) as client:
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

        async def _fetch_entries(
            client_obj: httpx.AsyncClient, use_headers: bool
        ) -> tuple[int, list[dict[str, Any]]]:
            hdrs = self._get_headers() if use_headers else None
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
                resp = await client_obj.get(url, headers=hdrs)
                raw = self._check_response(resp)
                total_row_count = int(raw.get("rowCount", 0))
                page_rows = parse_gridtable_rows(raw)
                if not page_rows:
                    break
                all_rows.extend(page_rows)
                offset += len(page_rows)
                if offset >= total_row_count or len(page_rows) < 50:
                    break
            return total_row_count, all_rows

        if self._async_http is not None:
            total_row_count, all_rows = await _fetch_entries(
                self._async_http, use_headers=True
            )
        else:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                headers=self._get_headers(),
                timeout=self.timeout,
            ) as client:
                total_row_count, all_rows = await _fetch_entries(
                    client, use_headers=False
                )

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

    async def async_get_quote_metadata(self) -> dict[str, Any]:
        """Async version of get_quote_metadata."""

        async def _fetch(
            client_obj: httpx.AsyncClient, use_headers: bool
        ) -> dict[str, Any]:
            hdrs = self._get_headers() if use_headers else None
            statuses = self._check_response(
                await client_obj.get(
                    f"{self.base_url}/v1/api/offertestatus", headers=hdrs
                )
            )
            templates = self._check_response(
                await client_obj.get(
                    f"{self.base_url}/v1/api/offertesjabloon/selectlist", headers=hdrs
                )
            )
            users = self._check_response(
                await client_obj.get(
                    f"{self.base_url}/v1/api/user/selectlist", headers=hdrs
                )
            )
            next_nr = self._check_response(
                await client_obj.get(
                    f"{self.base_url}/v1/api/offerte/nummer/next", headers=hdrs
                )
            )
            return {
                "statuses": statuses if isinstance(statuses, list) else [],
                "templates": templates if isinstance(templates, list) else [],
                "users": users if isinstance(users, list) else [],
                "next_quote_number": str(next_nr) if next_nr is not None else "",
            }

        if self._async_http is not None:
            return await _fetch(self._async_http, use_headers=True)

        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._get_headers(),
            timeout=self.timeout,
        ) as client:
            return await _fetch(client, use_headers=False)

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
        if self._async_http is not None:
            resp = await self._async_http.get(url, headers=self._get_headers())
            raw = self._check_response(resp)
        else:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                headers=self._get_headers(),
                timeout=self.timeout,
            ) as client:
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

        async def _fetch(
            client_obj: httpx.AsyncClient, use_headers: bool
        ) -> dict[str, Any]:
            hdrs = self._get_headers() if use_headers else None
            model = self._check_response(
                await client_obj.get(
                    f"{self.base_url}/v1/api/offerte/{qid}", headers=hdrs
                )
            )
            dashboard = self._check_response(
                await client_obj.get(
                    f"{self.base_url}/v1/api/offerte/dashboard/{qid}", headers=hdrs
                )
            )
            in_ex = str((model or {}).get("inEx") or "EX").upper()
            is_incl_str = "true" if in_ex == "IN" else "false"
            regels_raw = self._check_response(
                await client_obj.get(
                    f"{self.base_url}/v1/api/offerteregel/gridtable/{qid}"
                    f"?isInclBtw={is_incl_str}&addArtikelVoorraad=false",
                    headers=hdrs,
                )
            )
            files_raw = self._check_response(
                await client_obj.get(
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

        if self._async_http is not None:
            return await _fetch(self._async_http, use_headers=True)

        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._get_headers(),
            timeout=self.timeout,
        ) as client:
            return await _fetch(client, use_headers=False)

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
        if self._async_http is not None:
            resp = await self._async_http.post(
                url, json=payload, headers=self._get_headers()
            )
            return self._check_response(resp)

        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._get_headers(),
            timeout=self.timeout,
        ) as client:
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

        async def _execute_create(
            client_obj: httpx.AsyncClient, use_headers: bool
        ) -> int:
            hdrs = self._get_headers() if use_headers else None
            create_resp = self._check_response(
                await client_obj.post(
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
                        await client_obj.get(
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
                        await client_obj.post(
                            f"{self.base_url}/v1/api/offerteregel",
                            json=line_payload,
                            headers=hdrs,
                        )
                    )
            return created_id

        if self._async_http is not None:
            created_id = await _execute_create(self._async_http, use_headers=True)
        else:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                headers=self._get_headers(),
                timeout=self.timeout,
            ) as client:
                created_id = await _execute_create(client, use_headers=False)

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

        async def _execute_update(
            client_obj: httpx.AsyncClient, use_headers: bool
        ) -> None:
            hdrs = self._get_headers() if use_headers else None
            existing = self._check_response(
                await client_obj.get(
                    f"{self.base_url}/v1/api/offerte/{qid}", headers=hdrs
                )
            )
            if not isinstance(existing, dict):
                raise WebSessionError(f"Quote {qid} not found.")

            resolved_status_id = existing.get("statusId")
            if status_id is not None or status is not None:
                statuses = self._check_response(
                    await client_obj.get(
                        f"{self.base_url}/v1/api/offertestatus", headers=hdrs
                    )
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
                await client_obj.put(
                    f"{self.base_url}/v1/api/offerte",
                    json=update_payload,
                    headers=hdrs,
                )
            )

            if items is not None:
                is_incl_str = "true" if norm_in_ex == "IN" else "false"
                regels_raw = self._check_response(
                    await client_obj.get(
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
                                await client_obj.delete(
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
                            await client_obj.get(
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
                            await client_obj.post(
                                f"{self.base_url}/v1/api/offerteregel",
                                json=line_payload,
                                headers=hdrs,
                            )
                        )

        if self._async_http is not None:
            await _execute_update(self._async_http, use_headers=True)
        else:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                headers=self._get_headers(),
                timeout=self.timeout,
            ) as client:
                await _execute_update(client, use_headers=False)

        if archive_file_id is not None:
            await self.async_link_file_to_quote(
                quote_id=qid, file_id=int(archive_file_id)
            )

        return await self.async_get_quote(quote_id=qid)

    async def async_delete_quote(self, quote_id: int) -> dict[str, Any]:
        """Async version of delete_quote."""
        qid = int(quote_id)
        url = f"{self.base_url}/v1/api/offerte/{qid}"
        if self._async_http is not None:
            resp = await self._async_http.delete(url, headers=self._get_headers())
            result = self._check_response(resp)
            return {
                "quote_id": qid,
                "deleted": True,
                "response": result,
            }

        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._get_headers(),
            timeout=self.timeout,
        ) as client:
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

        if self._async_http is not None:
            resp = await self._async_http.get(url, headers=self._get_headers())
            result = self._check_response(resp)
            return result if isinstance(result, dict) else {}

        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._get_headers(),
            timeout=self.timeout,
        ) as client:
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
                    t.get("email")
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
        if self._async_http is not None:
            resp = await self._async_http.post(
                url, json=payload, headers=self._get_headers()
            )
            result = self._check_response(resp)
        else:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                headers=self._get_headers(),
                timeout=self.timeout,
            ) as client:
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
            "subject": str(resolved_subject),
            "attach_ubl": bool(attach_ubl),
            "response": result,
        }
