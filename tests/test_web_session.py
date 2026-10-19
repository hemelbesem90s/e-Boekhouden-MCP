"""Unit and mock tests for e-Boekhouden Web Session Bridge client and tools."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest
from pydantic import SecretStr

from eboekhouden_mcp.config import MCPConfig
from eboekhouden_mcp.server import EBoekhoudenMCPServer
from eboekhouden_mcp.web_session.client import (
    SessionExpiredError,
    WebSessionClient,
    WebSessionError,
    build_afschrift_params,
    parse_import_grid,
)
from eboekhouden_mcp.web_session.tools import (
    AttachFileToMutationTool,
    GetUnprocessedBankTransactionsTool,
    SuggestMutationForBankStatementTool,
)


# --------------------------------------------------------------------------- #
# Grid parsing & parameter building tests
# --------------------------------------------------------------------------- #


def test_parse_import_grid_columns():
    """Verify that column-indexed grid data is correctly transformed to row dicts."""
    sample_grid = {
        "rowCount": 2,
        "data": [
            [
                101,
                "2026-03-01",
                "NL91ABNA0123456789",
                "2026-03-01",
                -45.50,
                "KPN B.V. Factuur 9988",
                "9988",
                1000,
                "automatische incasso",
                True,
                "",
            ],
            [
                102,
                "2026-03-02",
                "NL91ABNA0123456789",
                "2026-03-02",
                1250.00,
                "Klant B.V. Factuur 2026-01",
                "2026-01",
                1000,
                "",
                False,
                "",
            ],
        ],
    }

    rows = parse_import_grid(sample_grid)
    assert len(rows) == 2
    assert rows[0]["id"] == 101
    assert rows[0]["mutBedrag"] == -45.50
    assert rows[0]["mutOmschrijving"] == "KPN B.V. Factuur 9988"
    assert rows[0]["mutFactuur"] == "9988"
    assert rows[0]["grootboekId"] == 1000
    assert rows[0]["hasFiles"] is True

    assert rows[1]["id"] == 102
    assert rows[1]["mutBedrag"] == 1250.00


def test_build_afschrift_params():
    """Test URL param generation for debit and credit transactions."""
    # Negative amount -> mutSoort = "4" (betaling verstuurd)
    debit_params = build_afschrift_params(
        id=55,
        rekening="NL91ABNA0123456789",
        mut_datum="2026-03-10",
        mut_bedrag=-85.00,
        mut_omschrijving="Hosting payment",
        mut_factuur="INV-88",
        grootboek_id=1000,
    )
    assert debit_params["id"] == "55"
    assert debit_params["mutBedrag"] == "-85.00"
    assert debit_params["mutSoort"] == "4"
    assert debit_params["mutFactuur"] == "INV-88"

    # Positive amount -> mutSoort = "3" (betaling ontvangen)
    credit_params = build_afschrift_params(
        id=56,
        rekening="NL91ABNA0123456789",
        mut_datum="2026-03-11",
        mut_bedrag=250.00,
        mut_omschrijving="Deposit",
    )
    assert credit_params["mutSoort"] == "3"
    assert credit_params["mutBedrag"] == "250.00"


# --------------------------------------------------------------------------- #
# WebSessionClient error detection tests
# --------------------------------------------------------------------------- #


def test_client_handles_session_expired_status():
    """HTTP 401 must raise SessionExpiredError."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Unauthorized")

    mock_http = httpx.Client(transport=httpx.MockTransport(handler))
    client = WebSessionClient(auth_token="dummy-token", http_client=mock_http)

    with pytest.raises(SessionExpiredError):
        client.get_import_grid()


def test_client_handles_security_envelope():
    """HTTP 200 with SECURITY_010 / errorType='security' must raise SessionExpiredError."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"errorType": "security", "message": "Niet ingelogd", "code": "SECURITY_010"},
        )

    mock_http = httpx.Client(transport=httpx.MockTransport(handler))
    client = WebSessionClient(auth_token="dummy-token", http_client=mock_http)

    with pytest.raises(SessionExpiredError, match="SECURITY_010"):
        client.get_import_grid()


def test_client_handles_validation_error_envelope():
    """Application validation errors must raise WebSessionError with upstream message."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"errorType": "validation", "message": "Rekening bestaat niet", "code": "VAL_001"},
        )

    mock_http = httpx.Client(transport=httpx.MockTransport(handler))
    client = WebSessionClient(auth_token="dummy-token", http_client=mock_http)

    with pytest.raises(WebSessionError, match="Rekening bestaat niet"):
        client.get_import_grid()


# --------------------------------------------------------------------------- #
# Mock HTTP requests tests
# --------------------------------------------------------------------------- #


def test_client_sync_endpoints():
    """Verify get_import_grid, get_mutatie_by_afschrift, and link_file_to_mutation."""
    requests_made: list[tuple[str, str, dict[str, Any] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        requests_made.append((request.method, str(request.url), body))

        if "/v1/api/import/gridtable" in str(request.url):
            return httpx.Response(200, json={"rowCount": 1, "data": [[1, "2026-03-01", "IBAN1", "2026-03-01", -10.0, "Test", "", 1000, "", False, ""]]})
        if "/v1/api/mutatie/getmutatiebyafschrift" in str(request.url):
            return httpx.Response(200, json={"mutNr": 0, "suggestie": {"grootboekId": 201}})
        if "/v1/api/folderkoppel/koppel/item" in str(request.url):
            return httpx.Response(200, json={"success": True})

        return httpx.Response(404)

    mock_http = httpx.Client(transport=httpx.MockTransport(handler))
    client = WebSessionClient(auth_token="valid-token", http_client=mock_http)

    # 1. Grid
    grid = client.get_import_grid(offset=0, limit=10)
    assert grid["rowCount"] == 1
    assert len(grid["rows"]) == 1
    assert grid["rows"][0]["id"] == 1

    # 2. Suggestion
    sugg = client.get_mutatie_by_afschrift({"id": 1, "rekening": "IBAN1"})
    assert "suggestie" in sugg

    # 3. Link file
    link_res = client.link_file_to_mutation(mutation_id=456, file_id=789)
    assert link_res["success"] is True

    # Check payload sent to folderkoppel
    assert requests_made[2][0] == "POST"
    assert requests_made[2][2] == {
        "koppelId": 456,
        "folders": [{"id": 789, "soort": "F"}],
        "koppelType": "MUT",
    }


@pytest.mark.asyncio
async def test_client_async_endpoints():
    """Verify async versions of WebSessionClient methods."""
    def handler(request: httpx.Request) -> httpx.Response:
        if "/v1/api/import/gridtable" in str(request.url):
            return httpx.Response(200, json={"rowCount": 0, "data": []})
        if "/v1/api/mutatie/getmutatiebyafschrift" in str(request.url):
            return httpx.Response(200, json={"suggestie": {"rekening": "1000"}})
        if "/v1/api/folderkoppel/koppel/item" in str(request.url):
            return httpx.Response(200, json={"linked": True})
        return httpx.Response(404)

    async_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = WebSessionClient(auth_token="valid-token", async_http_client=async_http)

    grid = await client.async_get_import_grid()
    assert grid["rowCount"] == 0

    sugg = await client.async_get_mutatie_by_afschrift({"id": "1"})
    assert "suggestie" in sugg

    link = await client.async_link_file_to_mutation(mutation_id=10, file_id=20)
    assert link["linked"] is True


# --------------------------------------------------------------------------- #
# Web Session MCP Tools tests
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_get_unprocessed_bank_transactions_tool():
    """Test get_unprocessed_bank_transactions MCP tool."""
    mock_client = MagicMock(spec=WebSessionClient)
    mock_client.async_get_import_grid.return_value = {
        "rowCount": 1,
        "rows": [{"id": 1, "mutOmschrijving": "Test"}],
    }

    tool = GetUnprocessedBankTransactionsTool(client=mock_client)
    dummy_sdk_client = MagicMock()

    result = await tool.execute(dummy_sdk_client, {"offset": 0, "limit": 25})
    assert result["rowCount"] == 1
    mock_client.async_get_import_grid.assert_called_once_with(offset=0, limit=25)


@pytest.mark.asyncio
async def test_suggest_mutation_for_bank_statement_tool():
    """Test suggest_mutation_for_bank_statement MCP tool."""
    mock_client = MagicMock(spec=WebSessionClient)
    mock_client.async_get_mutatie_by_afschrift.return_value = {"tegenRekening": 4100}

    tool = SuggestMutationForBankStatementTool(client=mock_client)
    dummy_sdk_client = MagicMock()

    result = await tool.execute(
        dummy_sdk_client,
        {
            "id": 12,
            "rekening": "NL01BANK123",
            "mut_datum": "2026-03-20",
            "mut_bedrag": -33.50,
            "mut_omschrijving": "SaaS monthly",
        },
    )
    assert result["tegenRekening"] == 4100
    mock_client.async_get_mutatie_by_afschrift.assert_called_once()


@pytest.mark.asyncio
async def test_attach_file_to_mutation_tool():
    """Test attach_file_to_mutation MCP tool."""
    mock_client = MagicMock(spec=WebSessionClient)
    mock_client.async_link_file_to_mutation.return_value = {"status": "ok"}

    tool = AttachFileToMutationTool(client=mock_client)
    dummy_sdk_client = MagicMock()

    result = await tool.execute(
        dummy_sdk_client,
        {"mutation_id": 500, "file_id": 600},
    )
    assert result["success"] is True
    assert result["mutation_id"] == 500
    assert result["file_id"] == 600
    mock_client.async_link_file_to_mutation.assert_called_once_with(
        mutation_id=500, file_id=600
    )


# --------------------------------------------------------------------------- #
# Configuration & Tool Enablement Policy tests
# --------------------------------------------------------------------------- #


def test_server_web_session_policy():
    """Verify tool_is_enabled reflects web session and write configurations."""
    # 1. Web session disabled by default
    config_default = MCPConfig(secret_token="test-token", _env_file=None)
    server_default = EBoekhoudenMCPServer(config_default)
    assert server_default.tool_is_enabled("get_unprocessed_bank_transactions") is False
    assert server_default.tool_is_enabled("suggest_mutation_for_bank_statement") is False
    assert server_default.tool_is_enabled("attach_file_to_mutation") is False

    # 2. Web session enabled, read-only
    config_web = MCPConfig(
        secret_token="test-token",
        enable_web_session=True,
        web_session_token=SecretStr("web-session-token"),
        _env_file=None,
    )
    server_web = EBoekhoudenMCPServer(config_web)
    assert server_web.tool_is_enabled("get_unprocessed_bank_transactions") is True
    assert server_web.tool_is_enabled("suggest_mutation_for_bank_statement") is True
    # attach_file_to_mutation is a write tool, so disabled without enable_write_tools
    assert server_web.tool_is_enabled("attach_file_to_mutation") is False

    # 3. Web session enabled with write tools
    config_web_write = MCPConfig(
        secret_token="test-token",
        enable_web_session=True,
        enable_write_tools=True,
        web_session_token=SecretStr("web-session-token"),
        _env_file=None,
    )
    server_web_write = EBoekhoudenMCPServer(config_web_write)
    assert server_web_write.tool_is_enabled("get_unprocessed_bank_transactions") is True
    assert server_web_write.tool_is_enabled("suggest_mutation_for_bank_statement") is True
    assert server_web_write.tool_is_enabled("attach_file_to_mutation") is True
