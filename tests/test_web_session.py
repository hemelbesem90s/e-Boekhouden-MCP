"""Unit and mock tests for e-Boekhouden Web Session Bridge client and tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from pydantic import SecretStr

from eboekhouden_mcp.config import MCPConfig
from eboekhouden_mcp.server import EBoekhoudenMCPServer
from eboekhouden_mcp.web_session.client import (
    IPVerificationRequiredError,
    SessionExpiredError,
    TwoFactorRequiredError,
    WebSessionClient,
    WebSessionError,
    _extract_v1_error_message,
    _validate_local_file_path,
    build_afschrift_params,
    normalize_web_mutation_payload,
    parse_import_grid,
)
from eboekhouden_mcp.web_session.tools import (
    AttachFileToMutationTool,
    DeleteWebMutationsTool,
    GetMutationModelByNumberTool,
    GetRecognizedInvoiceModelTool,
    GetUnprocessedBankTransactionsTool,
    ListArchiveFilesTool,
    ListArchiveFoldersTool,
    SaveWebMutationTool,
    SuggestMutationForBankStatementTool,
    UploadArchiveFileTool,
    WhitelistWebSessionIpTool,
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
            json={
                "errorType": "security",
                "message": "Niet ingelogd",
                "code": "SECURITY_010",
            },
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
            json={
                "errorType": "validation",
                "message": "Rekening bestaat niet",
                "code": "VAL_001",
            },
        )

    mock_http = httpx.Client(transport=httpx.MockTransport(handler))
    client = WebSessionClient(auth_token="dummy-token", http_client=mock_http)

    with pytest.raises(WebSessionError, match="Rekening bestaat niet"):
        client.get_import_grid()


# --------------------------------------------------------------------------- #
# Mock HTTP requests tests
# --------------------------------------------------------------------------- #


def test_client_sync_endpoints():
    """Verify sync methods on WebSessionClient including Digital Archive & Mutation models."""
    requests_made: list[tuple[str, str, dict[str, Any] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        requests_made.append((request.method, str(request.url), body))

        url = str(request.url)
        if "/v1/api/import/gridtable" in url:
            return httpx.Response(
                200,
                json={
                    "rowCount": 1,
                    "data": [
                        [
                            1,
                            "2026-03-01",
                            "IBAN1",
                            "2026-03-01",
                            -10.0,
                            "Test",
                            "",
                            1000,
                            "",
                            False,
                            "",
                        ]
                    ],
                },
            )
        if "/v1/api/mutatie/getmutatiebyafschrift" in url:
            return httpx.Response(
                200, json={"mutNr": 0, "suggestie": {"grootboekId": 201}}
            )
        if "/v1/api/folderkoppel/koppel/item" in url:
            return httpx.Response(200, json={"success": True})
        if "/v1/api/folder/directory" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 559261,
                        "naam": "Inkoopfacturen (Mail & Herken)",
                        "parentId": None,
                        "isDeleted": False,
                    }
                ],
            )
        if "/v1/api/folder/559261/filelist" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 1493990,
                        "naam": "factuur-2026.pdf",
                        "soort": "F",
                        "menHStatus": 2,
                        "koppelCount": 0,
                        "datum": "2026-03-15",
                        "size": 48120,
                    }
                ],
            )
        if "/v1/api/mutatie/folder/1493990/model" in url:
            return httpx.Response(
                200,
                json={
                    "mutatie": {"soort": 1, "factuurnummer": "INV-900"},
                    "mutatieRegels": [{"bedrag": 100.0, "btwCode": "HOOG_INK_21"}],
                    "suggestedAiIban": "NL91ABNA0417164300",
                    "suggestedAiBtwNr": "NL123456789B01",
                },
            )
        if "/v1/api/mutatie/nr/281/model" in url:
            return httpx.Response(
                200,
                json={
                    "mutatie": {"nr": 281, "soort": 1, "omschrijving": "Inkoop"},
                    "mutatieRegels": [{"bedrag": 250.0, "btwCode": "HOOG_INK_21"}],
                    "folderKoppelId": 1493990,
                },
            )
        if request.method == "POST" and url.endswith("/v1/api/mutatie"):
            return httpx.Response(200, json={"nr": 282, "saved": True})

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

    # 4. Archive folders
    folders = client.get_archive_folders()
    assert len(folders) == 1
    assert folders[0]["id"] == 559261

    # 5. Archive files
    files = client.get_archive_files(folder_id=559261, include_deleted=False)
    assert len(files) == 1
    assert files[0]["id"] == 1493990
    assert "includeDelete=false" in requests_made[4][1]

    # 6. Recognized invoice model
    rec_model = client.get_recognized_invoice_model(file_id=1493990)
    assert rec_model["suggestedAiIban"] == "NL91ABNA0417164300"

    # 7. Mutation model by number
    mut_model = client.get_mutation_model_by_number(mutation_number=281)
    assert mut_model["mutatie"]["nr"] == 281
    assert mut_model["folderKoppelId"] == 1493990

    # 8. Save web mutation
    save_res = client.save_web_mutation({"mutatie": {"soort": 1}, "mutatieRegels": []})
    assert save_res["nr"] == 282
    assert requests_made[7][0] == "POST"


@pytest.mark.asyncio
async def test_client_async_endpoints():
    """Verify async versions of WebSessionClient methods."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/v1/api/import/gridtable" in url:
            return httpx.Response(200, json={"rowCount": 0, "data": []})
        if "/v1/api/mutatie/getmutatiebyafschrift" in url:
            return httpx.Response(200, json={"suggestie": {"rekening": "1000"}})
        if "/v1/api/folderkoppel/koppel/item" in url:
            return httpx.Response(200, json={"linked": True})
        if "/v1/api/folder/directory" in url:
            return httpx.Response(200, json=[{"id": 10, "naam": "Inbox"}])
        if "/v1/api/folder/10/filelist" in url:
            return httpx.Response(
                200, json=[{"id": 99, "naam": "doc.pdf", "menHStatus": 1}]
            )
        if "/v1/api/mutatie/folder/99/model" in url:
            return httpx.Response(
                200, json={"mutatie": {"soort": 1}, "suggestedAiBtwNr": "NL001"}
            )
        if "/v1/api/mutatie/nr/281/model" in url:
            return httpx.Response(
                200, json={"mutatie": {"nr": 281}, "folderKoppelId": 99}
            )
        if request.method == "POST" and url.endswith("/v1/api/mutatie"):
            return httpx.Response(200, json={"nr": 300})
        return httpx.Response(404)

    async_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = WebSessionClient(auth_token="valid-token", async_http_client=async_http)

    grid = await client.async_get_import_grid()
    assert grid["rowCount"] == 0

    sugg = await client.async_get_mutatie_by_afschrift({"id": "1"})
    assert "suggestie" in sugg

    link = await client.async_link_file_to_mutation(mutation_id=10, file_id=20)
    assert link["linked"] is True

    folders = await client.async_get_archive_folders()
    assert folders[0]["id"] == 10

    files = await client.async_get_archive_files(folder_id=10, include_deleted=True)
    assert files[0]["id"] == 99

    rec = await client.async_get_recognized_invoice_model(file_id=99)
    assert rec["suggestedAiBtwNr"] == "NL001"

    mut = await client.async_get_mutation_model_by_number(mutation_number=281)
    assert mut["mutatie"]["nr"] == 281

    saved = await client.async_save_web_mutation({"mutatie": {"soort": 1}})
    assert saved["nr"] == 300


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


@pytest.mark.asyncio
async def test_list_archive_folders_tool():
    """Test list_archive_folders MCP tool."""
    mock_client = MagicMock(spec=WebSessionClient)
    mock_client.async_get_archive_folders.return_value = [
        {"id": 559261, "naam": "Inkoopfacturen", "parentId": None, "isDeleted": False}
    ]

    tool = ListArchiveFoldersTool(client=mock_client)
    dummy_sdk_client = MagicMock()

    result = await tool.execute(dummy_sdk_client, {})
    assert result["count"] == 1
    assert result["folders"][0]["id"] == 559261
    mock_client.async_get_archive_folders.assert_called_once_with()


@pytest.mark.asyncio
async def test_list_archive_files_tool():
    """Test list_archive_files MCP tool."""
    mock_client = MagicMock(spec=WebSessionClient)
    mock_client.async_get_archive_files.return_value = [
        {
            "id": 1493990,
            "naam": "inv.pdf",
            "soort": "F",
            "menHStatus": 2,
            "koppelCount": 0,
        }
    ]

    tool = ListArchiveFilesTool(client=mock_client)
    dummy_sdk_client = MagicMock()

    result = await tool.execute(
        dummy_sdk_client, {"folder_id": 559261, "include_deleted": False}
    )
    assert result["folder_id"] == 559261
    assert result["count"] == 1
    assert result["files"][0]["id"] == 1493990
    mock_client.async_get_archive_files.assert_called_once_with(
        folder_id=559261, include_deleted=False
    )


@pytest.mark.asyncio
async def test_get_recognized_invoice_model_tool():
    """Test get_recognized_invoice_model MCP tool."""
    mock_client = MagicMock(spec=WebSessionClient)
    mock_client.async_get_recognized_invoice_model.return_value = {
        "mutatie": {"soort": 1},
        "mutatieRegels": [],
        "suggestedAiIban": "NL91ABNA0417164300",
        "suggestedAiBtwNr": "NL123456789B01",
    }

    tool = GetRecognizedInvoiceModelTool(client=mock_client)
    dummy_sdk_client = MagicMock()

    result = await tool.execute(dummy_sdk_client, {"file_id": 1493990})
    assert result["suggestedAiIban"] == "NL91ABNA0417164300"
    mock_client.async_get_recognized_invoice_model.assert_called_once_with(
        file_id=1493990
    )


@pytest.mark.asyncio
async def test_get_mutation_model_by_number_tool():
    """Test get_mutation_model_by_number MCP tool."""
    mock_client = MagicMock(spec=WebSessionClient)
    mock_client.async_get_mutation_model_by_number.return_value = {
        "mutatie": {"nr": 281, "soort": 1},
        "mutatieRegels": [{"bedrag": 121.0}],
        "folderKoppelId": 1493990,
    }

    tool = GetMutationModelByNumberTool(client=mock_client)
    dummy_sdk_client = MagicMock()

    result = await tool.execute(dummy_sdk_client, {"mutation_number": 281})
    assert result["mutatie"]["nr"] == 281
    assert result["folderKoppelId"] == 1493990
    mock_client.async_get_mutation_model_by_number.assert_called_once_with(
        mutation_number=281
    )


@pytest.mark.asyncio
async def test_save_web_mutation_tool():
    """Test save_web_mutation MCP tool."""
    mock_client = MagicMock(spec=WebSessionClient)
    mock_client.async_save_web_mutation.return_value = {"nr": 285}

    tool = SaveWebMutationTool(client=mock_client)
    dummy_sdk_client = MagicMock()

    payload = {
        "mutatie": {"soort": 1, "datum": "2026-03-20"},
        "mutatieRegels": [{"bedrag": 100.0, "btwCode": "HOOG_INK_21"}],
        "folderKoppelId": 1493990,
    }
    result = await tool.execute(dummy_sdk_client, {"payload": payload})
    assert result["success"] is True
    assert result["response"]["nr"] == 285
    mock_client.async_save_web_mutation.assert_called_once_with(payload=payload)


# --------------------------------------------------------------------------- #
# Configuration & Tool Enablement Policy tests
# --------------------------------------------------------------------------- #


def test_server_web_session_policy():
    """Verify tool_is_enabled reflects web session and write configurations."""
    # 1. Web session disabled by default
    config_default = MCPConfig(secret_token="test-token", _env_file=None)
    server_default = EBoekhoudenMCPServer(config_default)
    assert server_default.tool_is_enabled("get_unprocessed_bank_transactions") is False
    assert (
        server_default.tool_is_enabled("suggest_mutation_for_bank_statement") is False
    )
    assert server_default.tool_is_enabled("attach_file_to_mutation") is False
    assert server_default.tool_is_enabled("list_archive_folders") is False
    assert server_default.tool_is_enabled("list_archive_files") is False
    assert server_default.tool_is_enabled("get_recognized_invoice_model") is False
    assert server_default.tool_is_enabled("get_mutation_model_by_number") is False
    assert server_default.tool_is_enabled("save_web_mutation") is False

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
    assert server_web.tool_is_enabled("list_archive_folders") is True
    assert server_web.tool_is_enabled("list_archive_files") is True
    assert server_web.tool_is_enabled("get_recognized_invoice_model") is True
    assert server_web.tool_is_enabled("get_mutation_model_by_number") is True
    # attach_file_to_mutation and save_web_mutation are write tools, so disabled without enable_write_tools
    assert server_web.tool_is_enabled("attach_file_to_mutation") is False
    assert server_web.tool_is_enabled("save_web_mutation") is False
    assert server_web.tool_is_enabled("upload_archive_file") is False
    assert server_web.tool_is_enabled("delete_web_mutations") is False
    assert server_web.tool_is_enabled("create_time_project") is False
    assert server_web.tool_is_enabled("update_time_project") is False
    assert server_web.tool_is_enabled("create_time_entry") is False
    assert server_web.tool_is_enabled("update_time_entry") is False
    assert server_web.tool_is_enabled("delete_time_entry") is False
    assert server_web.tool_is_enabled("invoice_time_entries") is False
    assert server_web.tool_is_enabled("update_invoice") is False

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
    assert (
        server_web_write.tool_is_enabled("suggest_mutation_for_bank_statement") is True
    )
    assert server_web_write.tool_is_enabled("list_archive_folders") is True
    assert server_web_write.tool_is_enabled("list_archive_files") is True
    assert server_web_write.tool_is_enabled("get_recognized_invoice_model") is True
    assert server_web_write.tool_is_enabled("get_mutation_model_by_number") is True
    assert server_web_write.tool_is_enabled("attach_file_to_mutation") is True
    assert server_web_write.tool_is_enabled("save_web_mutation") is True
    assert server_web_write.tool_is_enabled("upload_archive_file") is True
    assert server_web_write.tool_is_enabled("delete_web_mutations") is True
    assert server_web_write.tool_is_enabled("create_time_project") is True
    assert server_web_write.tool_is_enabled("update_time_project") is True
    assert server_web_write.tool_is_enabled("create_time_entry") is True
    assert server_web_write.tool_is_enabled("update_time_entry") is True
    assert server_web_write.tool_is_enabled("delete_time_entry") is True
    assert server_web_write.tool_is_enabled("invoice_time_entries") is True
    assert server_web_write.tool_is_enabled("update_invoice") is True


# --------------------------------------------------------------------------- #
# Normalization, Header, Empty Body, Upload & Delete tests
# --------------------------------------------------------------------------- #


def test_normalize_web_mutation_payload():
    """Verify MUT_026, MUT_112, MUT_117, and MutNr payload normalization."""
    # 1. Create payload with nr: 0, kostenplaats: 0, and reverse-charge btwCode
    create_raw = {
        "mutatie": {"nr": 0, "soort": 6, "inEx": "IN"},
        "mutatieRegels": [
            {"bedrag": 1.74, "btwCode": "BI_EU_INK", "kostenplaats": 0},
        ],
    }
    norm_create = normalize_web_mutation_payload(create_raw, is_update=False)
    assert "nr" not in norm_create["mutatie"]
    assert norm_create["mutatie"]["inEx"] == "EX"
    assert "kostenplaats" not in norm_create["mutatieRegels"][0]
    assert norm_create["folderKoppelId"] < 0

    # 2. Update payload with soort: 4 (FactuurbetalingVerstuurd)
    update_raw = {
        "mutatie": {"nr": 100, "soort": 4, "relatieId": 1001, "factuur": "INV-100"},
        "mutatieRegels": [
            {"index": 0, "bedrag": 25.00, "btwCode": "GEEN", "kostenplaats": 0},
        ],
    }
    norm_update = normalize_web_mutation_payload(update_raw, is_update=True)
    assert norm_update["mutatie"]["nr"] == 100
    assert norm_update["folderKoppelId"] == 100
    row = norm_update["mutatieRegels"][0]
    assert row["relatieId"] == 1001
    assert row["factuur"] == "INV-100"
    assert "kostenplaats" not in row


def test_client_headers_and_empty_200_response():
    """Verify Authorization header is sent and empty 200 responses return {'success': True}."""
    captured_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(dict(request.headers))
        return httpx.Response(200, text="")

    mock_http = httpx.Client(transport=httpx.MockTransport(handler))
    client = WebSessionClient(auth_token="jwt-eb-token", http_client=mock_http)

    res = client.move_archive_files(file_ids=[2001], target_folder_id=3001)
    assert res == {"success": True}
    assert captured_headers.get("authorization") == "jwt-eb-token"
    assert "auth-token=jwt-eb-token" in captured_headers.get("cookie", "")


def test_upload_archive_file_and_cloud_guard(tmp_path):
    """Verify local file upload Base64 encoding and fail-closed cloud path rejection."""
    pdf_file = tmp_path / "invoice_100.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 dummy content")

    captured_body: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_body.update(json.loads(request.content))
        return httpx.Response(200, json={"id": 2001, "fileName": "invoice_100.pdf"})

    mock_http = httpx.Client(transport=httpx.MockTransport(handler))
    client = WebSessionClient(auth_token="jwt-eb-token", http_client=mock_http)

    res = client.upload_archive_file(file_path=pdf_file, folder_id=3001)
    assert res["id"] == 2001
    assert captured_body["fileName"] == "invoice_100.pdf"
    assert captured_body["folderId"] == 3001
    assert captured_body["overwrite"] is True
    assert len(captured_body["data"]) > 10

    # Cloud path must be rejected fail-closed
    with pytest.raises(ValueError, match="Refusing to read binary file from cloud"):
        client.upload_archive_file(file_path=r"H:\Shared drives\Invoices\test.pdf")


@pytest.mark.parametrize(
    "cloud_path",
    [
        "/Users/jacob/Library/CloudStorage/GoogleDrive-jacob@grobler.nl/My Drive/invoice.pdf",
        "/Users/jacob/Library/Mobile Documents/com~apple~CloudDocs/receipt.pdf",
        "/Volumes/Google Drive/My Drive/invoice.pdf",
        r"H:\Shared drives\Invoices\test.pdf",
        "h:/shared drives/invoices/test.pdf",
        r"G:\My Drive\invoice.pdf",
        "g:/my drive/invoice.pdf",
        r"C:\Users\jacob\OneDrive\receipt.pdf",
        "/Users/jacob/OneDrive/receipt.pdf",
        r"C:\Users\jacob\Dropbox\receipt.pdf",
        "/Users/jacob/Dropbox/receipt.pdf",
        r"\\nas\share\invoice.pdf",
        "//nas/share/invoice.pdf",
    ],
)
def test_cloud_hydration_guard_rejects_virtual_mounts(cloud_path):
    """Verify fail-closed rejection for all macOS, Windows, and UNC cloud paths."""
    with pytest.raises(ValueError, match="Cloud Storage Hydration Protection"):
        _validate_local_file_path(cloud_path)


def test_cloud_hydration_guard_accepts_local_staging_paths(monkeypatch):
    """Verify macOS and staging paths outside cloud mounts are accepted."""
    monkeypatch.setattr(Path, "is_file", lambda self: True)

    path1 = _validate_local_file_path("/tmp/eboekhouden_inbox/invoice.pdf")
    assert path1 == Path("/tmp/eboekhouden_inbox/invoice.pdf")

    path2 = _validate_local_file_path(
        "/var/folders/zz/zyxvpxvq6csfxvn_n0000000000000/T/invoice.pdf"
    )
    assert path2 == Path("/var/folders/zz/zyxvpxvq6csfxvn_n0000000000000/T/invoice.pdf")

    path3 = _validate_local_file_path(r"C:\Temp\invoice.pdf")
    assert path3 == Path(r"C:\Temp\invoice.pdf")


def test_delete_web_mutations():
    """Verify mutation number resolution via gridtable and bulk deletion."""
    calls: list[tuple[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        url = str(request.url)
        calls.append((url, body))
        if "/v1/api/mutatie/gridtable" in url:
            return httpx.Response(200, json={"data": [[90001, 42, "2026-01-13"]]})
        if "/v1/api/mutatie/bulk/delete" in url:
            return httpx.Response(200, json={"removedIds": [90001]})
        return httpx.Response(404)

    mock_http = httpx.Client(transport=httpx.MockTransport(handler))
    client = WebSessionClient(auth_token="jwt-eb-token", http_client=mock_http)

    res = client.delete_web_mutations([42])
    assert res["resolved_ids"] == {42: 90001}
    assert res["response"] == {"removedIds": [90001]}
    assert calls[0][1] == {"filter": {"nr": "42"}}
    assert calls[1][1] == [90001]


@pytest.mark.asyncio
async def test_upload_and_delete_tools():
    """Verify UploadArchiveFileTool and DeleteWebMutationsTool execution."""
    mock_client = MagicMock(spec=WebSessionClient)
    mock_client.async_upload_archive_file.return_value = {"id": 2001}
    mock_client.async_delete_web_mutations.return_value = {
        "mutation_numbers": [42],
        "resolved_ids": {42: 90001},
        "response": {"removedIds": [90001]},
    }
    dummy_sdk_client = MagicMock()

    upload_tool = UploadArchiveFileTool(client=mock_client)
    up_res = await upload_tool.execute(
        dummy_sdk_client,
        {"file_path": r"C:\Temp\inv.pdf", "folder_id": 3001},
    )
    assert up_res["success"] is True
    assert up_res["response"]["id"] == 2001

    del_tool = DeleteWebMutationsTool(client=mock_client)
    del_res = await del_tool.execute(
        dummy_sdk_client,
        {"mutation_numbers": [42]},
    )
    assert del_res["success"] is True
    assert del_res["resolved_ids"] == {42: 90001}


def test_get_time_entries_autopagination_and_summary():
    """Verify 50-row auto-pagination and project/VAT summary calculation for time entries."""
    from eboekhouden_mcp.web_session.tools import (
        ListTimeActivitiesTool,
        ListTimeEntriesTool,
        ListTimeProjectsTool,
        _resolve_web_client,
    )

    col_meta = [
        {"name": "id", "index": 0},
        {"name": "datum", "index": 1},
        {"name": "medewerker", "index": 2},
        {"name": "projectId", "index": 3},
        {"name": "project", "index": 4},
        {"name": "activiteit", "index": 5},
        {"name": "aantalUren", "index": 6},
        {"name": "facturabel", "index": 7},
        {"name": "uurtarief", "index": 8},
        {"name": "factuur", "index": 9},
        {"name": "aantalKilometers", "index": 10},
        {"name": "kilometertarief", "index": 11},
        {"name": "factuurId", "index": 12},
        {"name": "opmerkingen", "index": 13},
    ]

    page1_rows = [
        [
            1000 + i,
            "2026-10-15T00:00:00",
            "JacobGrobler",
            851957,
            "25011E_BCKE_SSB_Opmeten",
            "Engineering_EU",
            2.0,
            "Ja",
            75.0,
            None,
            None,
            0.30,
            None,
            "CAD work",
        ]
        for i in range(50)
    ]
    page2_rows = [
        [
            2001,
            "2026-10-28T00:00:00",
            "JacobGrobler",
            842044,
            "26014S_MSTG_Kraker_Project_Support",
            "MSTG_Consult",
            3.0,
            "Ja",
            75.0,
            None,
            40.0,
            0.0,
            None,
            "Kraker consult",
        ]
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/v1/api/uur/factureren/gridtable" in url:
            if "offset=0" in url:
                return httpx.Response(
                    200,
                    json={
                        "rowCount": 51,
                        "colMetadata": col_meta,
                        "data": page1_rows,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "rowCount": 51,
                    "colMetadata": col_meta,
                    "data": page2_rows,
                },
            )
        if "/v1/api/project/getall/" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 851957,
                        "relatieId": 70666369,
                        "naam": "25011E_BCKE_SSB_Opmeten",
                        "actief": True,
                        "facturabel": True,
                    },
                    {
                        "id": 842044,
                        "relatieId": 70666523,
                        "naam": "26014S_MSTG_Kraker_Project_Support",
                        "actief": True,
                        "facturabel": True,
                    },
                ],
            )
        if "/v1/api/project/gridtable" in url:
            return httpx.Response(
                200,
                json={
                    "colMetadata": [
                        {"name": "id", "index": 0},
                        {"name": "project", "index": 1},
                        {"name": "relatie", "index": 2},
                    ],
                    "data": [
                        [851957, "25011E_BCKE_SSB_Opmeten", "Baecke BV"],
                        [
                            842044,
                            "26014S_MSTG_Kraker_Project_Support",
                            "MST Groep B.V.",
                        ],
                    ],
                },
            )
        if "/v1/api/activiteit/getall/" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 629937,
                        "naam": "Engineering_EU",
                        "uurtarief": 75.0,
                        "kilometertarief": 0.30,
                        "btwCode": "BI_EU_VERK_D",
                        "actief": True,
                    },
                    {
                        "id": 629916,
                        "naam": "MSTG_Consult",
                        "uurtarief": 75.0,
                        "kilometertarief": 0.0,
                        "btwCode": "HOOG_VERK_21",
                        "actief": True,
                    },
                ],
            )
        return httpx.Response(404)

    mock_http = httpx.Client(transport=httpx.MockTransport(handler))
    client = WebSessionClient(auth_token="jwt-eb-token", http_client=mock_http)

    res = client.get_time_entries(status="uninvoiced")
    assert res["rowCount"] == 51
    assert res["fetchedCount"] == 51
    summary = res["summary"]
    assert summary["entry_count"] == 51
    assert summary["total_hours"] == 103.0
    assert summary["total_kilometers"] == 40.0
    assert summary["total_excl_vat"] == 7725.0
    assert summary["total_vat"] == 47.25
    assert summary["total_incl_vat"] == 7772.25
    assert len(summary["by_project"]) == 2
    assert summary["by_project"][0]["project"] == "25011E_BCKE_SSB_Opmeten"
    assert summary["by_project"][0]["client"] == "Baecke BV"

    # Also test live token refresh on non-mock WebSessionClient
    live_client = WebSessionClient(auth_token="stale-token")
    import os

    old_env = os.environ.get("EBOEKHOUDEN_MCP_WEB_SESSION_TOKEN")
    try:
        os.environ["EBOEKHOUDEN_MCP_WEB_SESSION_TOKEN"] = "fresh-rotated-token"
        resolved = _resolve_web_client(live_client)
        assert resolved.auth_token == "fresh-rotated-token"
    finally:
        if old_env is None:
            os.environ.pop("EBOEKHOUDEN_MCP_WEB_SESSION_TOKEN", None)
        else:
            os.environ["EBOEKHOUDEN_MCP_WEB_SESSION_TOKEN"] = old_env
        assert ListTimeEntriesTool.name == "list_time_entries"
        assert ListTimeProjectsTool.name == "list_time_projects"
        assert ListTimeActivitiesTool.name == "list_time_activities"


@pytest.mark.asyncio
async def test_async_web_session_client_and_time_tools():
    """Verify async WebSessionClient methods via injected AsyncClient and time MCP tools."""
    from eboekhouden_mcp.web_session.tools import (
        CreateArchiveFolderTool,
        ListTimeActivitiesTool,
        ListTimeEntriesTool,
        ListTimeProjectsTool,
        MoveArchiveFilesTool,
    )

    col_meta = [
        {"name": "id", "index": 0},
        {"name": "datum", "index": 1},
        {"name": "medewerker", "index": 2},
        {"name": "projectId", "index": 3},
        {"name": "project", "index": 4},
        {"name": "activiteit", "index": 5},
        {"name": "aantalUren", "index": 6},
        {"name": "facturabel", "index": 7},
        {"name": "uurtarief", "index": 8},
        {"name": "factuur", "index": 9},
        {"name": "aantalKilometers", "index": 10},
        {"name": "kilometertarief", "index": 11},
        {"name": "factuurId", "index": 12},
        {"name": "opmerkingen", "index": 13},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/v1/api/import/gridtable" in url:
            return httpx.Response(
                200,
                json={
                    "rowCount": 1,
                    "colMetadata": [{"name": "id", "index": 0}],
                    "data": [[101]],
                },
            )
        if "/v1/api/mutatie/getmutatiebyafschrift" in url:
            return httpx.Response(200, json={"tegenRekening": 4100})
        if "/v1/api/folderkoppel/koppel/item" in url:
            return httpx.Response(200, json={"linked": True})
        if "/v1/api/folder/directory" in url:
            if request.method == "POST":
                return httpx.Response(200, json={"id": 999, "name": "NewFolder"})
            return httpx.Response(200, json=[{"id": 1, "naam": "Root"}])
        if "/filelist" in url:
            return httpx.Response(200, json=[{"id": 10, "naam": "doc.pdf"}])
        if "/v1/api/mutatie/folder/" in url:
            return httpx.Response(200, json={"mutatie": {"soort": 1}})
        if "/v1/api/mutatie/nr/" in url:
            return httpx.Response(200, json={"mutatie": {"nr": 500}})
        if "/v1/api/mutatie/gridtable" in url:
            return httpx.Response(200, json={"data": [[90001, 42, "2026-01-13"]]})
        if "/v1/api/mutatie/bulk/delete" in url:
            return httpx.Response(200, json={"removedIds": [90001]})
        if url.endswith("/v1/api/mutatie"):
            return httpx.Response(200, json={"nr": 501})
        if "/v1/api/folder/bulk/update/" in url:
            return httpx.Response(200, json={"moved": True})
        if "/v1/api/project/getall/" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 851957,
                        "relatieId": 70666369,
                        "naam": "25011E_BCKE_SSB_Opmeten",
                        "actief": True,
                        "facturabel": True,
                    }
                ],
            )
        if "/v1/api/project/gridtable" in url:
            return httpx.Response(
                200,
                json={
                    "colMetadata": [
                        {"name": "id", "index": 0},
                        {"name": "relatie", "index": 1},
                    ],
                    "data": [[851957, "Baecke BV"]],
                },
            )
        if "/v1/api/activiteit/getall/" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 629937,
                        "naam": "Engineering_EU",
                        "uurtarief": 75.0,
                        "kilometertarief": 0.30,
                        "btwCode": "BI_EU_VERK_D",
                        "actief": True,
                    }
                ],
            )
        if "/v1/api/uur/factureren/gridtable" in url:
            return httpx.Response(
                200,
                json={
                    "rowCount": 1,
                    "colMetadata": col_meta,
                    "data": [
                        [
                            1001,
                            "2026-10-15T00:00:00",
                            "JacobGrobler",
                            851957,
                            "25011E_BCKE_SSB_Opmeten",
                            "Engineering_EU",
                            4.0,
                            "Ja",
                            75.0,
                            None,
                            20.0,
                            0.30,
                            None,
                            "Test async entry",
                        ]
                    ],
                },
            )
        return httpx.Response(404)

    async_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = WebSessionClient(auth_token="jwt-eb-token", async_http_client=async_http)

    assert (await client.async_get_import_grid())["rowCount"] == 1
    assert (await client.async_get_mutatie_by_afschrift({"id": 1}))[
        "tegenRekening"
    ] == 4100
    assert (await client.async_link_file_to_mutation(1, 2))["linked"] is True
    assert len(await client.async_get_archive_folders()) == 1
    assert len(await client.async_get_archive_files(1)) == 1
    assert "mutatie" in await client.async_get_recognized_invoice_model(10)
    assert (await client.async_get_mutation_model_by_number(500))["mutatie"][
        "nr"
    ] == 500
    assert (
        await client.async_save_web_mutation(
            {"mutatie": {"soort": 6}, "mutatieRegels": []}
        )
    )["nr"] == 501
    assert (
        await client.async_save_web_mutation(
            {"mutatie": {"nr": 500, "soort": 6}, "mutatieRegels": []}
        )
    )["nr"] == 501
    assert (await client.async_move_archive_files([10], 1))["moved"] is True
    assert (await client.async_create_archive_folder("NewFolder"))["id"] == 999
    assert (await client.async_delete_web_mutations([42]))["resolved_ids"] == {
        42: 90001
    }

    # Test time tools via async client
    dummy_sdk = MagicMock()
    entries_tool = ListTimeEntriesTool(client=client)
    res_entries = await entries_tool.execute(
        dummy_sdk,
        {
            "status": "all",
            "project_id": 851957,
            "activity_id": 629937,
            "period_start": "2026-10-01",
            "period_end": "2026-10-31",
            "include_non_billable": True,
            "include_notes": True,
            "max_rows": 50,
        },
    )
    assert res_entries["summary"]["total_hours"] == 4.0
    assert res_entries["summary"]["total_kilometers"] == 20.0

    proj_tool = ListTimeProjectsTool(client=client)
    res_proj = await proj_tool.execute(dummy_sdk, {"active_only": True})
    assert res_proj["count"] == 1
    assert res_proj["projects"][0]["relatie"] == "Baecke BV"

    act_tool = ListTimeActivitiesTool(client=client)
    res_act = await act_tool.execute(dummy_sdk, {"active_only": True})
    assert res_act["count"] == 1
    assert res_act["activities"][0]["naam"] == "Engineering_EU"

    move_tool = MoveArchiveFilesTool(client=client)
    res_move = await move_tool.execute(
        dummy_sdk, {"file_ids": [10], "target_folder_id": 1}
    )
    assert res_move["success"] is True

    mkdir_tool = CreateArchiveFolderTool(client=client)
    res_mkdir = await mkdir_tool.execute(
        dummy_sdk, {"name": "NewFolder", "parent_folder_id": 0}
    )
    assert res_mkdir["success"] is True

    await async_http.aclose()


# --------------------------------------------------------------------------- #
# Quote (Offerte) Web Session Client & Tools tests
# --------------------------------------------------------------------------- #


def test_quote_helpers_and_sync_lifecycle():
    """Verify date/status/ledger helpers and sync list/get/create/update/delete quote lifecycle."""
    from eboekhouden_mcp.web_session.client import (
        build_quote_line_payload,
        format_eb_date_dict,
        parse_eb_date_dict,
        resolve_quote_status_id,
        resolve_revenue_ledger_id,
    )

    assert parse_eb_date_dict("2026-11-02") == {"year": 2026, "month": 11, "day": 2}
    assert parse_eb_date_dict("02-11-2026") == {"year": 2026, "month": 11, "day": 2}
    assert parse_eb_date_dict({"year": 2026, "month": 9, "day": 4}) == {
        "year": 2026,
        "month": 9,
        "day": 4,
    }
    assert format_eb_date_dict({"year": 2026, "month": 9, "day": 4}) == "2026-09-04"
    assert format_eb_date_dict("2026-09-04T00:00:00") == "2026-09-04"
    assert format_eb_date_dict(None) is None

    statuses = [
        {"id": 4027466, "status": "01. Ingevoerd"},
        {"id": 4027467, "status": "02. Verzonden"},
        {"id": 4027469, "status": "04. Geaccepteerd"},
    ]
    assert resolve_quote_status_id(statuses) == 4027466
    assert resolve_quote_status_id(statuses, status="Verzonden") == 4027467
    assert resolve_quote_status_id(statuses, status_id=4027469) == 4027469
    with pytest.raises(ValueError, match="Unknown quote status"):
        resolve_quote_status_id(statuses, status="NonExistent")

    ledgers = [
        {"id": 51798501, "code": "810200", "omschrijving": "omzet (btw hoog)"},
        {
            "id": 51798502,
            "code": "810300",
            "omschrijving": "omzet (buitenland binnen EU)",
        },
        {"id": 51798500, "code": "810100", "omschrijving": "omzet (btw laag)"},
    ]
    assert resolve_revenue_ledger_id(ledgers, vat_code="HOOG_VERK_21") == 51798501
    assert resolve_revenue_ledger_id(ledgers, vat_code="BI_EU_VERK_D") == 51798502
    assert resolve_revenue_ledger_id(ledgers, vat_code="LAAG_VERK_9") == 51798500
    assert resolve_revenue_ledger_id(ledgers, ledger_id=810300) == 51798502
    assert resolve_revenue_ledger_id(ledgers, ledger_id=51798501) == 51798501

    line_ex = build_quote_line_payload(
        {
            "description": "FEM Structural Analysis",
            "price_per_unit": 1000.0,
            "quantity": 2.0,
            "vat_code": "HOOG_VERK_21",
            "cost_center_id": 486260,
            "unit_id": 3192209,
            "code": "ENG-01",
            "discount": 10.0,
        },
        quote_id=2490022,
        in_ex_vat="EX",
        volgorde=1,
        ledgers=ledgers,
    )
    assert line_ex["prijsExcl"] == 1000.0
    assert line_ex["prijsIncl"] == 1210.0
    assert line_ex["grootboekId"] == 51798501
    assert line_ex["kostenplaatsId"] == 486260
    assert line_ex["eenheidId"] == 3192209
    assert line_ex["korting"] == 10.0

    line_in = build_quote_line_payload(
        {
            "description": "Incl VAT item",
            "price_per_unit": 121.0,
            "vat_code": "HOOG_VERK_21",
            "ledger_id": 51798501,
        },
        quote_id=2490022,
        in_ex_vat="IN",
        volgorde=2,
    )
    assert line_in["prijsIncl"] == 121.0
    assert line_in["prijsExcl"] == 100.0

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/v1/api/offertestatus" in url:
            return httpx.Response(200, json=statuses)
        if "/v1/api/offertesjabloon/selectlist" in url:
            return httpx.Response(
                200, json=[{"id": 907356, "naam": "Voorbeeld", "inactief": False}]
            )
        if "/v1/api/user/selectlist" in url:
            return httpx.Response(200, json=[{"id": 1080456, "naam": "JacobGrobler"}])
        if "/v1/api/offerte/nummer/next" in url:
            return httpx.Response(200, json="OF-00002")
        if "/v1/api/grootboekrekening/selectlist/actief/VWBAL" in url:
            return httpx.Response(200, json=ledgers)
        if "/v1/api/offerte/gridtable" in url:
            return httpx.Response(
                200,
                json={
                    "rowCount": 1,
                    "colMetadata": [
                        {"name": "id", "index": 0},
                        {"name": "nummer", "index": 1},
                        {"name": "status", "index": 2},
                        {"name": "totaalExcl", "index": 3},
                        {"name": "totaalbtw", "index": 4},
                        {"name": "totaalIncl", "index": 5},
                    ],
                    "data": [
                        [2490022, "OF-00001", "01. Ingevoerd", 1000.0, 210.0, 1210.0]
                    ],
                },
            )
        if "/v1/api/offerte/dashboard/" in url:
            return httpx.Response(
                200,
                json={
                    "nummer": "OF-00002",
                    "status": "01. Ingevoerd",
                    "relatieId": 70666369,
                    "relatieCode": "BCKE",
                    "relatieBedrijf": "Baecke BV",
                    "inBehandelingBij": "JacobGrobler",
                },
            )
        if "/v1/api/offerteregel/gridtable/" in url:
            return httpx.Response(
                200,
                json={
                    "rowCount": 1,
                    "colMetadata": [
                        {"name": "id", "index": 0},
                        {"name": "aantal", "index": 1},
                        {"name": "omschrijving", "index": 2},
                        {"name": "ppe", "index": 3},
                        {"name": "regelTotaal", "index": 4},
                        {"name": "btwCode", "index": 5},
                    ],
                    "data": [
                        [5001, 10.0, "Engineering", 125.0, 1250.0, "BI_EU_VERK_D"]
                    ],
                },
            )
        if "/v1/api/offerteregel/" in url and request.method == "DELETE":
            return httpx.Response(204, text="")
        if url.endswith("/v1/api/offerteregel") and request.method == "POST":
            return httpx.Response(200, json={"id": 5002})
        if "/v1/api/folder/gekoppeldgridtable" in url:
            return httpx.Response(
                200,
                json={
                    "rowCount": 1,
                    "colMetadata": [
                        {"name": "id", "index": 0},
                        {"name": "naam", "index": 1},
                    ],
                    "data": [[1493990, "Quote_OF-00002.pdf"]],
                },
            )
        if "/v1/api/folderkoppel/koppel/item" in url:
            return httpx.Response(200, json={"linked": True})
        if "/v1/api/offerte/" in url and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": 2490023,
                    "nummer": "OF-00002",
                    "relatieId": 70666369,
                    "statusId": 4027466,
                    "sjabloonId": 907356,
                    "behandelGebruikerId": 1080456,
                    "datum": {"year": 2026, "month": 11, "day": 2},
                    "vervaldatum": {"year": 2026, "month": 12, "day": 2},
                    "inEx": "EX",
                    "kenmerk": "26077I",
                    "notitiePubliek": "Public note",
                    "notitieIntern": "Internal note",
                    "totaalExcl": 1250.0,
                    "totaalBtw": 0.0,
                    "totaalIncl": 1250.0,
                },
            )
        if url.endswith("/v1/api/offerte") and request.method == "POST":
            return httpx.Response(200, json={"id": 2490023})
        if url.endswith("/v1/api/offerte") and request.method == "PUT":
            return httpx.Response(204, text="")
        if "/v1/api/offerte/" in url and request.method == "DELETE":
            return httpx.Response(200, json=True)
        return httpx.Response(404)

    mock_http = httpx.Client(transport=httpx.MockTransport(handler))
    client = WebSessionClient(auth_token="jwt-eb-token", http_client=mock_http)

    listed = client.list_quotes(relation_id=70666369)
    assert listed["rowCount"] == 1
    assert listed["summary"]["total_excl_vat"] == 1000.0
    assert listed["metadata"]["next_quote_number"] == "OF-00002"

    by_num = client.get_quote(quote_number="OF-00001")
    assert by_num["quote_number"] == "OF-00002"
    assert len(by_num["items"]) == 1
    assert len(by_num["attached_files"]) == 1

    created = client.create_quote(
        relation_id=70666369,
        reference="26077I",
        items=[
            {
                "description": "Engineering",
                "price_per_unit": 125.0,
                "quantity": 10.0,
                "vat_code": "BI_EU_VERK_D",
            }
        ],
        archive_file_id=1493990,
    )
    assert created["id"] == 2490023
    assert created["total_excl_vat"] == 1250.0

    updated = client.update_quote(
        quote_id=2490023,
        status="Verzonden",
        items=[
            {
                "description": "Updated Engineering",
                "price_per_unit": 125.0,
                "quantity": 12.0,
                "vat_code": "BI_EU_VERK_D",
            }
        ],
        replace_items=True,
        archive_file_id=1493990,
    )
    assert updated["id"] == 2490023

    deleted = client.delete_quote(2490023)
    assert deleted["deleted"] is True


@pytest.mark.asyncio
async def test_async_quote_tools():
    """Verify async quote client methods and all 6 Quote MCP tools."""
    from eboekhouden_mcp.web_session.tools import (
        AttachFileToQuoteTool,
        CreateQuoteTool,
        DeleteQuoteTool,
        GetQuoteInput,
        GetQuoteTool,
        ListQuotesTool,
        UpdateQuoteTool,
    )

    with pytest.raises(ValueError, match="Either quote_id or quote_number"):
        GetQuoteInput()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/v1/api/offertestatus" in url:
            return httpx.Response(
                200,
                json=[
                    {"id": 4027466, "status": "01. Ingevoerd"},
                    {"id": 4027467, "status": "02. Verzonden"},
                ],
            )
        if "/v1/api/offertesjabloon/selectlist" in url:
            return httpx.Response(
                200, json=[{"id": 907356, "naam": "Voorbeeld", "inactief": False}]
            )
        if "/v1/api/user/selectlist" in url:
            return httpx.Response(200, json=[{"id": 1080456, "naam": "JacobGrobler"}])
        if "/v1/api/offerte/nummer/next" in url:
            return httpx.Response(200, json="OF-00003")
        if "/v1/api/grootboekrekening/selectlist/actief/VWBAL" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 51798501,
                        "code": "810200",
                        "omschrijving": "omzet (btw hoog)",
                    },
                    {"id": 51798502, "code": "810300", "omschrijving": "omzet (EU)"},
                ],
            )
        if "/v1/api/offerte/gridtable" in url:
            return httpx.Response(
                200,
                json={
                    "rowCount": 1,
                    "colMetadata": [
                        {"name": "id", "index": 0},
                        {"name": "nummer", "index": 1},
                        {"name": "totaalExcl", "index": 2},
                        {"name": "totaalbtw", "index": 3},
                        {"name": "totaalIncl", "index": 4},
                    ],
                    "data": [[2490025, "OF-00003", 2500.0, 525.0, 3025.0]],
                },
            )
        if "/v1/api/offerte/dashboard/" in url:
            return httpx.Response(
                200,
                json={
                    "nummer": "OF-00003",
                    "status": "02. Verzonden",
                    "relatieId": 70666369,
                    "relatieCode": "BCKE",
                    "relatieBedrijf": "Baecke BV",
                },
            )
        if "/v1/api/offerteregel/gridtable/" in url:
            return httpx.Response(
                200,
                json={
                    "rowCount": 1,
                    "colMetadata": [{"name": "id", "index": 0}],
                    "data": [[6001]],
                },
            )
        if "/v1/api/offerteregel/" in url and request.method == "DELETE":
            return httpx.Response(204, text="")
        if url.endswith("/v1/api/offerteregel") and request.method == "POST":
            return httpx.Response(200, json={"id": 6002})
        if "/v1/api/folder/gekoppeldgridtable" in url:
            return httpx.Response(
                200,
                json={"rowCount": 0, "colMetadata": [], "data": []},
            )
        if "/v1/api/folderkoppel/koppel/item" in url:
            return httpx.Response(200, json={"linked": True})
        if "/v1/api/offerte/" in url and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": 2490025,
                    "nummer": "OF-00003",
                    "relatieId": 70666369,
                    "statusId": 4027467,
                    "sjabloonId": 907356,
                    "behandelGebruikerId": 1080456,
                    "datum": {"year": 2026, "month": 11, "day": 2},
                    "vervaldatum": {"year": 2026, "month": 12, "day": 2},
                    "inEx": "EX",
                    "totaalExcl": 2500.0,
                    "totaalBtw": 525.0,
                    "totaalIncl": 3025.0,
                },
            )
        if url.endswith("/v1/api/offerte") and request.method == "POST":
            return httpx.Response(200, json={"id": 2490025})
        if url.endswith("/v1/api/offerte") and request.method == "PUT":
            return httpx.Response(204, text="")
        if "/v1/api/offerte/" in url and request.method == "DELETE":
            return httpx.Response(200, json=True)
        return httpx.Response(404)

    async_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = WebSessionClient(auth_token="jwt-eb-token", async_http_client=async_http)
    dummy_sdk = MagicMock()

    list_res = await ListQuotesTool(client=client).execute(
        dummy_sdk, {"relation_id": 70666369, "include_metadata": True}
    )
    assert list_res["rowCount"] == 1
    assert list_res["quotes"][0]["nummer"] == "OF-00003"

    get_res = await GetQuoteTool(client=client).execute(
        dummy_sdk, {"quote_number": "OF-00003"}
    )
    assert get_res["id"] == 2490025

    create_res = await CreateQuoteTool(client=client).execute(
        dummy_sdk,
        {
            "relation_id": 70666369,
            "status": "Verzonden",
            "reference": "26077I",
            "items": [
                {
                    "description": "Engineering package",
                    "price_per_unit": 2500.0,
                    "quantity": 1.0,
                    "vat_code": "HOOG_VERK_21",
                }
            ],
            "archive_file_id": 1493990,
        },
    )
    assert create_res["success"] is True
    assert create_res["quote"]["id"] == 2490025

    update_res = await UpdateQuoteTool(client=client).execute(
        dummy_sdk,
        {
            "quote_id": 2490025,
            "status": "Verzonden",
            "items": [
                {
                    "description": "Revised Engineering package",
                    "price_per_unit": 2500.0,
                    "quantity": 1.0,
                    "vat_code": "HOOG_VERK_21",
                }
            ],
            "replace_items": True,
            "archive_file_id": 1493990,
        },
    )
    assert update_res["success"] is True

    attach_res = await AttachFileToQuoteTool(client=client).execute(
        dummy_sdk, {"quote_id": 2490025, "file_id": 1493990}
    )
    assert attach_res["success"] is True

    del_res = await DeleteQuoteTool(client=client).execute(
        dummy_sdk, {"quote_id": 2490025}
    )
    assert del_res["success"] is True
    assert del_res["deleted"] is True

    await async_http.aclose()


@pytest.mark.asyncio
async def test_send_invoice_web_session_sync_and_async():
    """Verify sync and async invoice emailing via web session API."""
    from eboekhouden_mcp.web_session.tools import SendInvoiceTool

    captured_posts = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/v1/api/factuur/maildata/1332610/73143509" in url:
            return httpx.Response(
                200,
                json={
                    "subject": "Factuur F00061 - Las- en Constructiewerk Xander Verbunt B.V.",
                    "body": "<p>Beste Xander, hierbij uw factuur F00061.</p>",
                    "emailAan": "Xanderverbunt@gmail.com",
                    "naam": "Jacob Grobler",
                    "emailVan": "jacob@groblerengineering.nl",
                    "ccEmail": "",
                    "bccEmail": "",
                },
            )
        if "/v1/api/factuur/73143509/maildata" in url:
            return httpx.Response(
                200,
                json={
                    "subject": "Factuur F00061",
                    "body": "<p>Standaard factuur maildata</p>",
                    "emailAan": "Xanderverbunt@gmail.com",
                    "naam": "Jacob Grobler",
                    "emailVan": "jacob@groblerengineering.nl",
                },
            )
        if "/v1/api/factuur/mail" in url and request.method == "POST":
            body = json.loads(request.content.decode("utf-8"))
            captured_posts.append(body)
            if body.get("aanEmail") == "error@example.com":
                return httpx.Response(
                    200, json={"errorCount": 1, "errorMessage": "Invalid email host"}
                )
            return httpx.Response(200, json={"errorCount": 0, "errorMessage": None})
        return httpx.Response(404, json={"error": f"Not found: {url}"})

    sync_transport = httpx.MockTransport(handler)
    sync_http = httpx.Client(
        transport=sync_transport, base_url="https://secure20.e-boekhouden.nl"
    )
    client = WebSessionClient(auth_token="test-token")
    client._http = sync_http

    # 1. Sync get_invoice_mail_data
    mail_data = client.get_invoice_mail_data(invoice_id=73143509, template_id=1332610)
    assert mail_data["subject"].startswith("Factuur F00061")
    assert mail_data["emailAan"] == "Xanderverbunt@gmail.com"

    # 2. Sync send_invoice_email using template
    res_sync = client.send_invoice_email(
        invoice_id=73143509,
        email_template_id=1332610,
    )
    assert res_sync["success"] is True
    assert res_sync["recipient_email"] == "Xanderverbunt@gmail.com"
    assert captured_posts[-1]["factuurId"] == 73143509
    assert captured_posts[-1]["aanEmail"] == "Xanderverbunt@gmail.com"
    assert captured_posts[-1]["factuurUbl"] is True

    # 3. Async send_invoice_email with custom overrides
    async_transport = httpx.MockTransport(handler)
    async_http = httpx.AsyncClient(
        transport=async_transport, base_url="https://secure20.e-boekhouden.nl"
    )
    client._async_http = async_http

    res_async = await client.async_send_invoice_email(
        invoice_id=73143509,
        recipient_email="custom@verbunt.nl",
        subject="Aangepast onderwerp F00061",
        body="Aangepaste body",
        attach_ubl=False,
    )
    assert res_async["success"] is True
    assert res_async["recipient_email"] == "custom@verbunt.nl"
    assert captured_posts[-1]["aanEmail"] == "custom@verbunt.nl"
    assert captured_posts[-1]["subject"] == "Aangepast onderwerp F00061"
    assert captured_posts[-1]["factuurUbl"] is False

    # 4. Error handling
    with pytest.raises(WebSessionError, match="Invalid email host"):
        await client.async_send_invoice_email(
            invoice_id=73143509,
            recipient_email="error@example.com",
            subject="Test error",
            body="Test error body",
        )

    # 5. SendInvoiceTool execution with REST client relation fallback
    from unittest.mock import MagicMock

    mock_sdk = MagicMock()
    mock_sdk.invoices.get.return_value = MagicMock(relation_id=70666663)
    mock_sdk.relations.get.return_value = MagicMock(email="fallback@verbunt.nl")

    tool = SendInvoiceTool(client=client)
    tool_res = await tool.execute(
        mock_sdk,
        {
            "invoice_id": 73143509,
            "email_template_id": 1332610,
        },
    )
    assert tool_res["success"] is True
    assert tool_res["invoice_id"] == 73143509

    sync_http.close()
    await async_http.aclose()


@pytest.mark.asyncio
async def test_web_session_auto_login_and_silent_refresh(monkeypatch):
    """Verify cold-start auto-login, silent 401/SECURITY_010 token refresh, TOTP generation, and keyring persistence."""
    from eboekhouden_mcp.web_session.client import generate_totp_code

    # RFC 6238 test vector (Base32 of "12345678901234567890" = GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ)
    totp_secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    code_59 = generate_totp_code(totp_secret, timestamp=59.0)
    assert len(code_59) == 6
    assert code_59 == "287082"

    stored_secrets: dict[tuple[str, str], str] = {}

    def fake_set_keyring_secret(service: str, account: str, secret: str) -> None:
        stored_secrets[(service, account)] = secret

    monkeypatch.setattr(
        "eboekhouden_mcp.config.set_keyring_secret", fake_set_keyring_secret
    )

    login_calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/v1/api/auth/login" in url and request.method == "POST":
            body = json.loads(request.content.decode("utf-8"))
            login_calls.append(body)
            if body.get("password") == "wrong-pass":
                return httpx.Response(
                    401,
                    json={
                        "errorType": "validation",
                        "message": "Gebruikersnaam en/of wachtwoord is onjuist.",
                    },
                )
            if body.get("password") == "locked-pass":
                return httpx.Response(
                    400,
                    json={"errorType": "Lockout", "lockoutMinutes": 15},
                )
            if body.get("password") == "v1-fallback-pass":
                return httpx.Response(
                    500,
                    json={
                        "errorType": "server",
                        "message": "Er is iets misgegaan. Foutcode: WR2RCTZ0",
                    },
                )
            return httpx.Response(200, json={"token": "fresh-jwt-token-999"})

        if "/bh/inloggen.asp" in url and request.method == "GET":
            return httpx.Response(
                200,
                headers={"Set-Cookie": "SRVRID=10s12; path=/"},
                text='<input type="hidden" name="txtFormCheck" value="99887766">',
            )

        if "/bh/inloggen.asp?LOGIN=1" in url and request.method == "POST":
            return httpx.Response(
                200,
                headers={
                    "Set-Cookie": "auth-token=v1-cookie-jwt-token-777; Domain=.e-boekhouden.nl; Path=/; HttpOnly; Secure"
                },
                text="<html>Logged in</html>",
            )

        if "/v1/api/folder/directory" in url:
            auth_hdr = request.headers.get("Authorization", "")
            if auth_hdr != "fresh-jwt-token-999":
                return httpx.Response(
                    200,
                    json={
                        "errorType": "security",
                        "message": "Niet ingelogd",
                        "code": "SECURITY_010",
                    },
                )
            return httpx.Response(200, json=[{"id": 1, "naam": "Inbox"}])

        return httpx.Response(404)

    # 1. Sync cold-start auto-login (no initial auth_token)
    sync_http = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://secure20.e-boekhouden.nl",
    )
    client_cold = WebSessionClient(
        auth_token=None,
        username="jacob_user",
        password="secret_password",
        totp_secret=totp_secret,
        http_client=sync_http,
        persist_to_keyring=True,
    )
    folders = client_cold.get_archive_folders()
    assert len(folders) == 1
    assert client_cold.auth_token == "fresh-jwt-token-999"
    assert len(login_calls) == 1
    assert login_calls[0]["username"] == "jacob_user"
    assert len(login_calls[0]["twoFactorCode"]) == 6
    assert (
        stored_secrets[("eboekhouden_mcp", "web_session_token")]
        == "fresh-jwt-token-999"
    )

    # 2. Sync expired token silent refresh (starts with expired token -> hits SECURITY_010 -> logs in -> retries)
    client_expired = WebSessionClient(
        auth_token="expired-jwt-token",
        username="jacob_user",
        password="secret_password",
        http_client=sync_http,
        persist_to_keyring=True,
    )
    folders2 = client_expired.get_archive_folders()
    assert len(folders2) == 1
    assert client_expired.auth_token == "fresh-jwt-token-999"
    assert len(login_calls) == 2

    # 3. Async expired token silent refresh
    async_http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://secure20.e-boekhouden.nl",
    )
    client_async_expired = WebSessionClient(
        auth_token="expired-async-token",
        username="jacob_user",
        password="secret_password",
        async_http_client=async_http,
        persist_to_keyring=True,
    )
    folders_async = await client_async_expired.async_get_archive_folders()
    assert len(folders_async) == 1
    assert client_async_expired.auth_token == "fresh-jwt-token-999"
    assert len(login_calls) == 3

    # 4. Login error & lockout handling
    bad_client = WebSessionClient(
        username="jacob_user",
        password="wrong-pass",
        http_client=sync_http,
    )
    with pytest.raises(WebSessionError, match="onjuist"):
        bad_client.login()

    locked_client = WebSessionClient(
        username="jacob_user",
        password="locked-pass",
        http_client=sync_http,
    )
    with pytest.raises(WebSessionError, match="locked out for 15"):
        locked_client.login()

    # 4b. Classic V1 login flow (GET + POST /bh/inloggen.asp) when /v1/api/auth/login returns 500 WR2RCTZ0
    v1_client = WebSessionClient(
        username="jacob_user",
        password="v1-fallback-pass",
        http_client=sync_http,
        persist_to_keyring=True,
    )
    minted_tok = v1_client.login()
    assert minted_tok == "v1-cookie-jwt-token-777"
    assert v1_client.auth_token == "v1-cookie-jwt-token-777"
    assert (
        stored_secrets[("eboekhouden_mcp", "web_session_token")]
        == "v1-cookie-jwt-token-777"
    )

    v1_async_client = WebSessionClient(
        username="jacob_user",
        password="v1-fallback-pass",
        async_http_client=async_http,
        persist_to_keyring=True,
    )
    minted_async_tok = await v1_async_client.async_login()
    assert minted_async_tok == "v1-cookie-jwt-token-777"

    # 5. Server wires WebSessionClient when only web_username + web_password are provided
    cfg_creds_only = MCPConfig(
        secret_token="rest-api-token",
        enable_web_session=True,
        web_username="jacob_user",
        web_password="secret_password",
        _env_file=None,
    )
    srv = EBoekhoudenMCPServer(cfg_creds_only)
    folder_tool = srv.registry.get("list_archive_folders")
    assert folder_tool is not None
    assert folder_tool._web_client is not None
    assert folder_tool._web_client.has_login_credentials() is True

    sync_http.close()
    await async_http.aclose()


@pytest.mark.asyncio
async def test_time_tracking_and_invoice_update_write_tools():
    """Verify sync and async time project/entry write tools, invoice updates, and enriched send_invoice."""
    from eboekhouden_mcp.web_session.tools import (
        CreateTimeEntryTool,
        CreateTimeProjectTool,
        DeleteTimeEntryTool,
        InvoiceTimeEntriesInput,
        InvoiceTimeEntriesTool,
        UpdateInvoiceTool,
        UpdateTimeEntryTool,
        UpdateTimeProjectTool,
    )

    recorded_posts: list[dict[str, Any]] = []
    recorded_puts: list[dict[str, Any]] = []
    recorded_deletes: list[str] = []
    recorded_gets: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        method = request.method

        if method == "GET":
            recorded_gets.append(url)
            if "/v1/api/project/876343" in url:
                return httpx.Response(
                    200,
                    json={
                        "id": 876343,
                        "naam": "26071E_BCKE_Tank_Platforms",
                        "actief": True,
                        "relatieId": 70666369,
                        "factuurSjabloonId": 1363414,
                        "factuurWijze": 1,
                        "factuurGroeperenOp": 0,
                        "factuurOmschrijvingType": 1,
                        "uurtarief": 85.0,
                        "uurtariefInEx": "EX",
                        "uurtariefGrootboekrekeningId": 51798502,
                        "uurtariefBtwCode": "BI_EU_VERK_D",
                        "kmtarief": 0.35,
                        "kmtariefInEx": "EX",
                        "kmtariefGrootboekrekeningId": 51798502,
                        "kmtariefBtwCode": "BI_EU_VERK_D",
                        "kostenplaatsId": 492222,
                        "notities": "Platforms initial",
                        "stelLimietIn": False,
                        "limiet": 0,
                        "limietType": 0,
                        "stuurMeldingBijPercentage": 0,
                        "stuurMeldingGebruikerIds": [],
                    },
                )
            if "/v1/api/grootboekrekening/selectlist/actief/VWBAL" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": 51798501,
                            "code": "810200",
                            "omschrijving": "Omzet hoog NL",
                        },
                        {
                            "id": 51798502,
                            "code": "810300",
                            "omschrijving": "Omzet EU diensten",
                        },
                    ],
                )
            if "/v1/api/user/selectlist" in url:
                return httpx.Response(
                    200, json=[{"id": 841802, "naam": "Jacob Grobler"}]
                )
            if "/v1/api/uur/28047685" in url:
                return httpx.Response(
                    200,
                    json={
                        "id": 28047685,
                        "datum": {"year": 2026, "month": 10, "day": 6},
                        "medewerkerId": 841802,
                        "projectId": 876343,
                        "activiteitId": 925998,
                        "aantal": 2.0,
                        "opmerkingen": "Drawings for platform",
                        "declarabel": True,
                        "kilometers": 0.0,
                        "gefactureerd": 0,
                    },
                )
            if "/v1/api/factuur/73221294" in url:
                return httpx.Response(
                    200,
                    json={
                        "factuur": {
                            "id": 73221294,
                            "nummer": "F00062",
                            "kenmerk": "OLD_KENMERK",
                            "factuurtekst": "OLD_TEKST",
                            "datum": {"year": 2026, "month": 11, "day": 5},
                            "betalingstermijn": 30,
                            "sjabloonId": 1363414,
                        },
                        "factuurItems": [],
                    },
                )
            if "/v1/api/factuur/pdfinvoiceschedule" in url:
                return httpx.Response(200, json={"scheduled": True})

        elif method == "POST":
            body = (
                json.loads(request.content.decode("utf-8")) if request.content else {}
            )
            recorded_posts.append({"url": url, "body": body})
            if "/v1/api/project" in url:
                return httpx.Response(200, json={**body, "id": 876344})
            if "/v1/api/uur/factureren" in url:
                if 999999 in body.get("ids", []):
                    return httpx.Response(
                        200,
                        json={
                            "errorCount": 1,
                            "errorMessages": ["Uur al gefactureerd"],
                            "ids": [],
                        },
                    )
                return httpx.Response(
                    200, json={"errorCount": 0, "errorMessages": [], "ids": [73221294]}
                )
            if "/v1/api/uur" in url:
                return httpx.Response(200, json={**body, "id": 28047686})
            if "/v1/api/factuur/mail" in url:
                return httpx.Response(200, json={"errorCount": 0})

        elif method == "PUT":
            body = (
                json.loads(request.content.decode("utf-8")) if request.content else {}
            )
            recorded_puts.append({"url": url, "body": body})
            if "/v1/api/project" in url:
                return httpx.Response(200, json=body)
            if "/v1/api/uur" in url:
                return httpx.Response(200, json=body)
            if "/v1/api/factuur/model" in url:
                return httpx.Response(200, json={"saved": True})

        elif method == "DELETE":
            recorded_deletes.append(url)
            if "/v1/api/uur/" in url:
                return httpx.Response(200, json={"deleted": True})

        return httpx.Response(404, json={"error": f"Not found: {url}"})

    sync_http_test = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://secure20.e-boekhouden.nl",
    )
    async_http_test = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://secure20.e-boekhouden.nl",
    )
    client_sync = WebSessionClient(
        auth_token="test-session-token",
        http_client=sync_http_test,
    )
    client_async = WebSessionClient(
        auth_token="test-session-token",
        async_http_client=async_http_test,
    )

    try:
        # 1. Sync & Async get_time_project
        p_sync = client_sync.get_time_project(876343)
        assert p_sync["id"] == 876343
        assert p_sync["naam"] == "26071E_BCKE_Tank_Platforms"

        p_async = await client_async.async_get_time_project(876343)
        assert p_async["id"] == 876343

        # 2. Sync create_time_project (with 6-digit ledger code resolution)
        created_p = client_sync.create_time_project(
            name="26071E_BCKE_Tank_Platforms_New",
            relation_id=70666369,
            hourly_rate=85.0,
            hourly_rate_ledger_id=810300,  # 6-digit code -> resolves to 51798502
            hourly_rate_vat_code="BI_EU_VERK_D",
            notes="New platform build",
        )
        assert created_p["id"] == 876344
        post_proj = [p for p in recorded_posts if "/v1/api/project" in p["url"]][-1]
        assert post_proj["body"]["uurtariefGrootboekrekeningId"] == 51798502
        assert post_proj["body"]["naam"] == "26071E_BCKE_Tank_Platforms_New"

        # 2b. Async create_time_project
        created_p_async = await client_async.async_create_time_project(
            name="26071E_BCKE_Async",
            relation_id=70666369,
            hourly_rate=90.0,
            hourly_rate_ledger_id=810300,
        )
        assert created_p_async["id"] == 876344

        # 3. Sync & Async update_time_project (set active=False on completion)
        upd_p = client_sync.update_time_project(
            project_id=876343,
            active=False,
            notes="Completed platforms",
        )
        assert upd_p["actief"] is False
        assert upd_p["notities"] == "Completed platforms"

        upd_p_async = await client_async.async_update_time_project(
            project_id=876343,
            active=True,
            hourly_rate=95.0,
        )
        assert upd_p_async["actief"] is True
        assert upd_p_async["uurtarief"] == 95.0

        # 4. Sync & Async get_time_entry
        entry_sync = client_sync.get_time_entry(28047685)
        assert entry_sync["id"] == 28047685
        assert entry_sync["aantal"] == 2.0

        entry_async = await client_async.async_get_time_entry(28047685)
        assert entry_async["id"] == 28047685

        # 5. Sync & Async create_time_entry (auto-resolving employee_id)
        new_entry = client_sync.create_time_entry(
            project_id=876343,
            activity_id=925998,
            hours=3.5,
            date="2026-11-05",
            notes="Welding inspection",
        )
        assert new_entry["id"] == 28047686
        post_uur = [
            p
            for p in recorded_posts
            if "/v1/api/uur" in p["url"] and not p["url"].endswith("factureren")
        ][-1]
        assert post_uur["body"]["gebruikerId"] == 841802
        assert post_uur["body"]["aantalUren"] == 3.5
        assert post_uur["body"]["datum"] == {"year": 2026, "month": 11, "day": 5}

        new_entry_async = await client_async.async_create_time_entry(
            project_id=876343,
            activity_id=925998,
            hours=2.5,
            employee_id=841802,
        )
        assert new_entry_async["id"] == 28047686

        # 6. Sync & Async update_time_entry
        upd_entry = client_sync.update_time_entry(
            entry_id=28047685,
            hours=4.0,
            notes="Revised CAD models",
        )
        assert upd_entry["aantalUren"] == 4.0
        assert upd_entry["opmerkingen"] == "Revised CAD models"

        upd_entry_async = await client_async.async_update_time_entry(
            entry_id=28047685,
            hours=5.0,
        )
        assert upd_entry_async["aantalUren"] == 5.0

        # 7. Sync & Async delete_time_entry
        del_sync = client_sync.delete_time_entry(28047685)
        assert del_sync["success"] is True
        assert any("/v1/api/uur/28047685" in u for u in recorded_deletes)

        del_async = await client_async.async_delete_time_entry(28047685)
        assert del_async["success"] is True

        # 8. Sync & Async update_invoice (model + pdf schedule)
        upd_inv = client_sync.update_invoice(
            invoice_id=73221294,
            reference="26071E_BCKE",
            invoice_text="Detailing of Tank Platforms",
            payment_term_days=30,
        )
        assert upd_inv["success"] is True
        assert upd_inv["reference"] == "26071E_BCKE"
        assert upd_inv["invoice_text"] == "Detailing of Tank Platforms"
        put_model = [p for p in recorded_puts if "/v1/api/factuur/model" in p["url"]][
            -1
        ]
        assert put_model["body"]["factuur"]["kenmerk"] == "26071E_BCKE"
        assert any("pdfinvoiceschedule?factuurId=73221294" in u for u in recorded_gets)

        upd_inv_async = await client_async.async_update_invoice(
            invoice_id=73221294,
            reference="26071E_BCKE_V2",
        )
        assert upd_inv_async["reference"] == "26071E_BCKE_V2"

        # 9. Sync & Async invoice_time_entries (with atomic header post-update)
        inv_sync = client_sync.invoice_time_entries(
            entry_ids=[28047685],
            reference="26071E_AUTOREF",
            invoice_text="Auto-invoiced time",
        )
        assert inv_sync["success"] is True
        assert inv_sync["invoice_ids"] == [73221294]
        assert len(inv_sync["updated_invoices"]) == 1
        assert inv_sync["updated_invoices"][0]["reference"] == "26071E_AUTOREF"

        inv_async = await client_async.async_invoice_time_entries(
            entry_ids=[28047685],
            reference="26071E_ASYNC_REF",
            invoice_text="Async-invoiced time",
        )
        assert inv_async["success"] is True
        assert inv_async["invoice_ids"] == [73221294]
        assert len(inv_async["updated_invoices"]) == 1

        # 9b. Error handling on invoice_time_entries
        with pytest.raises(WebSessionError, match="Uur al gefactureerd"):
            client_sync.invoice_time_entries([999999])

        with pytest.raises(WebSessionError, match="Uur al gefactureerd"):
            await client_async.async_invoice_time_entries([999999])

        # 10. Sync & Async enriched send_invoice
        mail_sync = client_sync.send_invoice_email(
            invoice_id=73221294,
            recipient_email="finance@baecke.nl",
            sender_email="jacob@grobler.nl",
            sender_name="Jacob Grobler",
            subject="Factuur F00062",
            body="Geachte heer Baecke,\n\nHierbij de factuur.",
            email_template_id=123,
        )
        assert mail_sync["success"] is True
        assert mail_sync["from_email"] == "jacob@grobler.nl"
        assert mail_sync["from_name"] == "Jacob Grobler"
        assert mail_sync["email_template_id"] == 123
        assert mail_sync["body"] == "Geachte heer Baecke,\n\nHierbij de factuur."

        mail_async = await client_async.async_send_invoice_email(
            invoice_id=73221294,
            recipient_email="finance@baecke.nl",
            sender_email="jacob@grobler.nl",
            sender_name="Jacob Grobler",
            subject="Factuur F00062",
            body="Async body text",
        )
        assert mail_async["from_email"] == "jacob@grobler.nl"
        assert mail_async["body"] == "Async body text"

        # 11. MCP Tool Execution tests
        mock_eb_client = MagicMock()

        # CreateTimeProjectTool
        tool_cp = CreateTimeProjectTool(client_async)
        res_cp = await tool_cp.execute(
            mock_eb_client,
            {"name": "26071E_Tool_Test", "relation_id": 70666369, "hourly_rate": 85.0},
        )
        assert res_cp["success"] is True
        assert res_cp["project"]["id"] == 876344

        # UpdateTimeProjectTool
        tool_up = UpdateTimeProjectTool(client_async)
        res_up = await tool_up.execute(
            mock_eb_client,
            {"project_id": 876343, "active": False},
        )
        assert res_up["success"] is True
        assert res_up["project"]["actief"] is False

        # CreateTimeEntryTool
        tool_ce = CreateTimeEntryTool(client_async)
        res_ce = await tool_ce.execute(
            mock_eb_client,
            {"project_id": 876343, "activity_id": 925998, "hours": 4.5},
        )
        assert res_ce["success"] is True
        assert res_ce["entry"]["id"] == 28047686

        # UpdateTimeEntryTool
        tool_ue = UpdateTimeEntryTool(client_async)
        res_ue = await tool_ue.execute(
            mock_eb_client,
            {"entry_id": 28047685, "hours": 6.0},
        )
        assert res_ue["success"] is True
        assert res_ue["entry"]["aantalUren"] == 6.0

        # DeleteTimeEntryTool
        tool_de = DeleteTimeEntryTool(client_async)
        res_de = await tool_de.execute(
            mock_eb_client,
            {"entry_id": 28047685},
        )
        assert res_de["success"] is True

        # InvoiceTimeEntriesTool
        tool_inv = InvoiceTimeEntriesTool(client_async)
        res_inv = await tool_inv.execute(
            mock_eb_client,
            {"entry_ids": [28047685], "reference": "26071E_TOOL_REF"},
        )
        assert res_inv["success"] is True
        assert res_inv["invoice_ids"] == [73221294]

        # InvoiceTimeEntriesInput alias tests
        validated_input_ids = InvoiceTimeEntriesInput(ids=[101, 102])
        assert validated_input_ids.entry_ids == [101, 102]
        validated_input_aliases = InvoiceTimeEntriesInput(time_entry_ids=[201])
        assert validated_input_aliases.entry_ids == [201]
        with pytest.raises(ValueError, match="At least one time entry ID"):
            InvoiceTimeEntriesInput(entry_ids=[])

        # UpdateInvoiceTool
        tool_ui = UpdateInvoiceTool(client_async)
        res_ui = await tool_ui.execute(
            mock_eb_client,
            {
                "invoice_id": 73221294,
                "reference": "26071E_FINAL",
                "invoice_text": "Final Platforms",
            },
        )
        assert res_ui["success"] is True
        assert res_ui["reference"] == "26071E_FINAL"

    finally:
        sync_http_test.close()
        await async_http_test.aclose()


# --------------------------------------------------------------------------- #
# IP Verification Challenge & Whitelist Tests
# --------------------------------------------------------------------------- #


def test_extract_v1_error_message():
    """Test extracting error text from <span id="ERR_MSG">."""
    assert (
        _extract_v1_error_message(
            '<span id="ERR_MSG"><font color=red><center>Foutmelding</center></font></span>'
        )
        == "Foutmelding"
    )
    assert _extract_v1_error_message("<html>Geen fout hier</html>") is None


def test_login_ip_verification_html():
    """Verify that an HTML response indicating an IP challenge raises IPVerificationRequiredError."""
    html_ip_challenge = """
    <html>
        <table id="formWrapper">
            <span id="ERR_MSG"><font color=red><center>Inloggen vanaf een nieuw IP-adres. Bevestig uw IP-adres via de e-mail.</center></font></span>
        </table>
        <a id="resetPassword" href="/overig/vergeten">Gebruikersnaam en/of wachtwoord vergeten</a>
    </html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html_ip_challenge)

    client = WebSessionClient(
        username="user@example.com",
        password="secretpassword",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(IPVerificationRequiredError) as exc_info:
        client._parse_login_response(
            httpx.Response(200, text=html_ip_challenge), persist_to_keyring=False
        )
    assert exc_info.value.code == "IP_VERIFICATION_REQUIRED"
    assert "IP" in str(exc_info.value) or "ip" in str(exc_info.value)


def test_login_ip_verification_json():
    """Verify that a JSON response with IP verification signals raises IPVerificationRequiredError."""
    # Test errorType
    resp_type = httpx.Response(
        403,
        json={
            "errorType": "ip_verification",
            "message": "Nieuw IP-adres vereist verificatie",
        },
    )
    client = WebSessionClient()
    with pytest.raises(IPVerificationRequiredError) as exc_info:
        client._parse_login_response(resp_type, persist_to_keyring=False)
    assert exc_info.value.code == "IP_VERIFICATION_REQUIRED"

    # Test code
    resp_code = httpx.Response(
        401,
        json={"code": "IP_VERIFICATION_REQUIRED", "message": "Verify your IP address"},
    )
    with pytest.raises(IPVerificationRequiredError):
        client._parse_login_response(resp_code, persist_to_keyring=False)


def test_login_two_factor_detection():
    """Verify that 2FA prompts in HTML or JSON raise TwoFactorRequiredError."""
    html_2fa = """
    <html>
        <div>U heeft gekozen om gebruik te maken van tweestapsverificatie bij het inloggen.</div>
        <a id="resetPassword">Gebruikersnaam en/of wachtwoord vergeten</a>
    </html>
    """
    client = WebSessionClient()
    with pytest.raises(TwoFactorRequiredError) as exc_info:
        client._parse_login_response(
            httpx.Response(200, text=html_2fa), persist_to_keyring=False
        )
    assert exc_info.value.code == "TWO_FACTOR_REQUIRED"

    # JSON 2FA
    json_2fa = httpx.Response(
        400,
        json={"errorType": "two_factor", "message": "Two factor code required"},
    )
    with pytest.raises(TwoFactorRequiredError):
        client._parse_login_response(json_2fa, persist_to_keyring=False)


def test_login_does_not_false_positive_on_footer_reset_link():
    """V1 login pages with footer reset password link must not trigger invalid credentials error."""
    # HTML without ERR_MSG and without tokens (e.g. initial re-render without explicit error)
    html_benign = """
    <html>
        <form name="frm" Action="inloggen.asp?LOGIN=1" method="post">
            <input type="text" id="txtEmail" name="txtEmail">
            <input type="password" id="txtWachtwoord" name="txtWachtwoord">
        </form>
        <a id="resetPassword" href="/overig/vergeten">Gebruikersnaam en/of wachtwoord vergeten</a>
    </html>
    """
    client = WebSessionClient()
    with pytest.raises(WebSessionError) as exc_info:
        client._parse_login_response(
            httpx.Response(200, text=html_benign), persist_to_keyring=False
        )
    # It should not claim "Gebruikersnaam en/of wachtwoord is onjuist"
    assert "login form returned without an authentication token" in str(exc_info.value)
    # But it includes helpful note about new IP address blocks
    assert "new IP address" in str(exc_info.value)


def test_login_credentials_failure_includes_ip_guidance():
    """Real invalid credentials error in ERR_MSG must include guidance about potential new IP blocks."""
    html_err = """
    <html>
        <span id="ERR_MSG"><font color=red><center>Gebruikersnaam en/of wachtwoord is onjuist</center></font></span>
        <a id="resetPassword" href="/overig/vergeten">Gebruikersnaam en/of wachtwoord vergeten</a>
    </html>
    """
    client = WebSessionClient()
    with pytest.raises(WebSessionError) as exc_info:
        client._parse_login_response(
            httpx.Response(200, text=html_err), persist_to_keyring=False
        )
    err_str = str(exc_info.value)
    assert "Gebruikersnaam en/of wachtwoord is onjuist" in err_str
    assert "new IP address" in err_str


def test_extract_whitelist_code():
    """Test extracting code from raw code string and full confirmation URLs."""
    assert WebSessionClient._extract_whitelist_code("XYZ123ABC") == "XYZ123ABC"
    assert (
        WebSessionClient._extract_whitelist_code(
            "https://secure20.e-boekhouden.nl/overig/login-whitelist?code=ABC-9988-XYZ"
        )
        == "ABC-9988-XYZ"
    )
    assert (
        WebSessionClient._extract_whitelist_code(
            "https://secure20.e-boekhouden.nl/overig/login-whitelist?other=1&code=CODE77#section"
        )
        == "CODE77"
    )
    with pytest.raises(ValueError, match="must not be empty"):
        WebSessionClient._extract_whitelist_code("   ")


def test_whitelist_ip_sync_success_and_auto_login():
    """Test whitelist_ip with code and URL, triggering auto-login when credentials exist."""
    whitelist_calls: list[str] = []
    login_calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/v1/api/loginwhitelist/whitelist" in url and request.method == "POST":
            code = request.url.params.get("code", "")
            whitelist_calls.append(code)
            return httpx.Response(200, text='"Success"')
        if "/v1/api/auth/login" in url and request.method == "POST":
            login_calls.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"token": "fresh-whitelisted-jwt"})
        return httpx.Response(404)

    mock_http = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://secure20.e-boekhouden.nl",
    )
    client = WebSessionClient(
        username="jacob_user",
        password="secret_pass",
        http_client=mock_http,
    )
    result = client.whitelist_ip(
        "https://secure20.e-boekhouden.nl/overig/login-whitelist?code=TOKEN_555"
    )

    assert result["success"] is True
    assert result["status"] == "Success"
    assert result["code"] == "TOKEN_555"
    assert result["auto_login"] is True
    assert result["auth_token"] == "fresh-whitelisted-jwt"
    assert client.auth_token == "fresh-whitelisted-jwt"
    assert whitelist_calls == ["TOKEN_555"]
    assert len(login_calls) == 1


@pytest.mark.asyncio
async def test_whitelist_ip_async_already_whitelisted():
    """Test async_whitelist_ip returning 'Already whitelisted'."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='"Already whitelisted"')

    mock_async = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://secure20.e-boekhouden.nl",
    )
    client = WebSessionClient(async_http_client=mock_async)
    result = await client.async_whitelist_ip("TOKEN_ALREADY_OK")

    assert result["success"] is True
    assert result["status"] == "Already whitelisted"
    assert result["code"] == "TOKEN_ALREADY_OK"


def test_whitelist_ip_invalid_code():
    """Test whitelist_ip with an expired or invalid code."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='"Code invalid"')

    mock_http = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://secure20.e-boekhouden.nl",
    )
    client = WebSessionClient(http_client=mock_http)
    with pytest.raises(WebSessionError, match="invalid or has expired"):
        client.whitelist_ip("EXPIRED_CODE")


@pytest.mark.asyncio
async def test_whitelist_web_session_ip_tool_execute():
    """Test executing WhitelistWebSessionIpTool directly."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='"Success"')

    mock_async = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://secure20.e-boekhouden.nl",
    )
    client = WebSessionClient(async_http_client=mock_async)
    tool = WhitelistWebSessionIpTool(client)

    result = await tool.execute(
        None,
        {
            "code_or_url": "https://secure20.e-boekhouden.nl/overig/login-whitelist?code=TOOL_CODE_123"
        },
    )
    assert result["success"] is True
    assert result["status"] == "Success"
    assert result["code"] == "TOOL_CODE_123"
