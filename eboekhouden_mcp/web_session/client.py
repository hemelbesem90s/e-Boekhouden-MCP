"""Modular web session client for e-Boekhouden using cookie-based authentication."""

from __future__ import annotations

import base64
import copy
import json
import random
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx


REVERSE_CHARGE_BTW_CODES = frozenset({"BI_EU_INK", "BU_EU_INK", "VERL_INK"})
CLOUD_PATH_PREFIXES = ("h:\\", "h:/", "//", "\\\\")


class WebSessionError(Exception):
    """Base exception for e-Boekhouden web session client errors."""


class SessionExpiredError(WebSessionError):
    """Raised when the e-Boekhouden web session token has expired or is invalid."""


def _validate_local_file_path(file_path: str | Path) -> Path:
    """Enforce fail-closed local file access (reject cloud drives / UNC paths)."""
    raw_str = str(file_path).strip()
    lower_str = raw_str.lower()
    if any(lower_str.startswith(prefix) for prefix in CLOUD_PATH_PREFIXES) or "shared drives" in lower_str:
        raise ValueError(
            f"Refusing to read binary file from cloud/network path '{raw_str}'. "
            "Copy or stage the file in a local directory (e.g. C:\\Temp\\ or scratch/) first."
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
            normalized["folderKoppelId"] = -random.randint(1, 1_000_000_000)
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
            raise SessionExpiredError("e-Boekhouden web session expired or unauthorized")

        if (
            "SECURITY_010" in body
            or "Niet ingelogd" in body
            or '"errorType":"security"' in body
        ):
            raise SessionExpiredError("e-Boekhouden session expired (SECURITY_010 / Niet ingelogd)")

        if response.status_code not in (200, 201):
            raise WebSessionError(
                f"Web session API returned HTTP {response.status_code}: {body[:200]}"
            )

        if not body.strip():
            return {"success": True}

        try:
            data = response.json()
        except Exception as exc:
            raise WebSessionError(f"Failed to parse JSON response: {body[:200]}") from exc

        if isinstance(data, dict):
            error_type = data.get("errorType")
            if error_type:
                if error_type == "security":
                    raise SessionExpiredError(f"Security error: {data.get('message', 'session invalid')}")
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
                    if isinstance(row, list) and len(row) >= 2 and str(row[1]) == str(nr):
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

    # ---------------- Async Methods ---------------- #

    async def async_get_import_grid(self, offset: int = 0, limit: int = 50) -> dict[str, Any]:
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

    async def async_get_mutatie_by_afschrift(self, params: dict[str, Any]) -> dict[str, Any]:
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

    async def async_link_file_to_mutation(self, mutation_id: int, file_id: int) -> dict[str, Any]:
        """Async version of link_file_to_mutation."""
        url = f"{self.base_url}/v1/api/folderkoppel/koppel/item"
        payload = {
            "koppelId": mutation_id,
            "folders": [{"id": file_id, "soort": "F"}],
            "koppelType": "MUT",
        }
        if self._async_http is not None:
            resp = await self._async_http.post(url, json=payload, headers=self._get_headers())
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
                resp = await self._async_http.put(url, json=normalized, headers=self._get_headers())
            else:
                resp = await self._async_http.post(url, json=normalized, headers=self._get_headers())
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

        async def _execute_delete(client_obj: httpx.AsyncClient, use_headers: bool) -> dict[str, Any]:
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
                    if isinstance(row, list) and len(row) >= 2 and str(row[1]) == str(nr):
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

