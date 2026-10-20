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
    """Verify sync methods on WebSessionClient including Digital Archive & Mutation models."""
    requests_made: list[tuple[str, str, dict[str, Any] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        requests_made.append((request.method, str(request.url), body))

        url = str(request.url)
        if "/v1/api/import/gridtable" in url:
            return httpx.Response(200, json={"rowCount": 1, "data": [[1, "2026-03-01", "IBAN1", "2026-03-01", -10.0, "Test", "", 1000, "", False, ""]]})
        if "/v1/api/mutatie/getmutatiebyafschrift" in url:
            return httpx.Response(200, json={"mutNr": 0, "suggestie": {"grootboekId": 201}})
        if "/v1/api/folderkoppel/koppel/item" in url:
            return httpx.Response(200, json={"success": True})
        if "/v1/api/folder/directory" in url:
            return httpx.Response(
                200,
                json=[
                    {"id": 559261, "naam": "Inkoopfacturen (Mail & Herken)", "parentId": None, "isDeleted": False}
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
            return httpx.Response(200, json=[{"id": 99, "naam": "doc.pdf", "menHStatus": 1}])
        if "/v1/api/mutatie/folder/99/model" in url:
            return httpx.Response(200, json={"mutatie": {"soort": 1}, "suggestedAiBtwNr": "NL001"})
        if "/v1/api/mutatie/nr/281/model" in url:
            return httpx.Response(200, json={"mutatie": {"nr": 281}, "folderKoppelId": 99})
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
        {"id": 1493990, "naam": "inv.pdf", "soort": "F", "menHStatus": 2, "koppelCount": 0}
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
    mock_client.async_get_recognized_invoice_model.assert_called_once_with(file_id=1493990)


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
    assert server_default.tool_is_enabled("suggest_mutation_for_bank_statement") is False
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
    assert server_web_write.tool_is_enabled("list_archive_folders") is True
    assert server_web_write.tool_is_enabled("list_archive_files") is True
    assert server_web_write.tool_is_enabled("get_recognized_invoice_model") is True
    assert server_web_write.tool_is_enabled("get_mutation_model_by_number") is True
    assert server_web_write.tool_is_enabled("attach_file_to_mutation") is True
    assert server_web_write.tool_is_enabled("save_web_mutation") is True
    assert server_web_write.tool_is_enabled("upload_archive_file") is True
    assert server_web_write.tool_is_enabled("delete_web_mutations") is True


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


