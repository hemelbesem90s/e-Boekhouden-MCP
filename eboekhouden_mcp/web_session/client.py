"""Modular web session client for e-Boekhouden using cookie-based authentication."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

import httpx


class WebSessionError(Exception):
    """Base exception for e-Boekhouden web session client errors."""


class SessionExpiredError(WebSessionError):
    """Raised when the e-Boekhouden web session token has expired or is invalid."""


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
            headers["Cookie"] = f"auth-token={self.auth_token}"
        return headers

    def _check_response(self, response: httpx.Response) -> dict[str, Any]:
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
