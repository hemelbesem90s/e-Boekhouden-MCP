"""MCP tools for the e-Boekhouden web session bridge."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field, model_validator

from eboekhouden_mcp.tools.base import BaseTool, ToolSchema
from eboekhouden_mcp.web_session.client import (
    WebSessionClient,
    build_afschrift_params,
)

if TYPE_CHECKING:
    from eboekhouden import EBoekhoudenClient


def _lookup_live_web_token() -> str:
    """Read the freshest web session token from environment or OS keyring."""
    token = os.environ.get("EBOEKHOUDEN_MCP_WEB_SESSION_TOKEN") or os.environ.get(
        "EBOEKHOUDEN_WEB_SESSION_TOKEN", ""
    )
    if not token:
        service_name = os.environ.get(
            "EBOEKHOUDEN_MCP_KEYRING_SERVICE", "eboekhouden_mcp"
        )
        try:
            from eboekhouden_mcp.config import get_keyring_secret

            keyring_token = get_keyring_secret(service_name, "web_session_token")
            if keyring_token:
                token = keyring_token
        except Exception:  # nosec B110
            pass
    return token.strip() if token else ""


def _lookup_live_web_credentials() -> tuple[str, str, str]:
    """Read the freshest web login credentials (username, password, totp_secret) from env or keyring."""
    username = os.environ.get("EBOEKHOUDEN_MCP_WEB_USERNAME") or os.environ.get(
        "EBOEKHOUDEN_WEB_USERNAME", ""
    )
    password = os.environ.get("EBOEKHOUDEN_MCP_WEB_PASSWORD") or os.environ.get(
        "EBOEKHOUDEN_WEB_PASSWORD", ""
    )
    totp_secret = os.environ.get("EBOEKHOUDEN_MCP_WEB_TOTP_SECRET") or os.environ.get(
        "EBOEKHOUDEN_WEB_TOTP_SECRET", ""
    )
    service_name = os.environ.get("EBOEKHOUDEN_MCP_KEYRING_SERVICE", "eboekhouden_mcp")
    try:
        from eboekhouden_mcp.config import get_keyring_secret

        if not username:
            username = get_keyring_secret(service_name, "web_username") or ""
        if not password:
            password = get_keyring_secret(service_name, "web_password") or ""
        if not totp_secret:
            totp_secret = get_keyring_secret(service_name, "web_totp_secret") or ""
    except Exception:  # nosec B110
        pass
    return username.strip(), password.strip(), totp_secret.strip()


def _resolve_web_client(
    client_override: WebSessionClient | None = None,
) -> WebSessionClient:
    """Resolve an active WebSessionClient using injection, environment, or keyring."""
    if client_override is not None:
        # If this is a live production client (no custom test transport injected),
        # refresh its token and credentials from keyring/env in case the user rotated them.
        if (
            isinstance(client_override, WebSessionClient)
            and getattr(client_override, "_http", True) is None
            and getattr(client_override, "_async_http", True) is None
        ):
            live_token = _lookup_live_web_token()
            if live_token and live_token != client_override.auth_token:
                client_override.set_token(live_token)
            live_user, live_pass, live_totp = _lookup_live_web_credentials()
            if live_user or live_pass or live_totp:
                client_override.set_credentials(
                    username=live_user or None,
                    password=live_pass or None,
                    totp_secret=live_totp or None,
                )
        return client_override

    token = _lookup_live_web_token()
    username, password, totp_secret = _lookup_live_web_credentials()
    service_name = os.environ.get("EBOEKHOUDEN_MCP_KEYRING_SERVICE", "eboekhouden_mcp")
    base_url = os.environ.get(
        "EBOEKHOUDEN_MCP_WEB_SESSION_BASE_URL", "https://secure20.e-boekhouden.nl"
    )
    if not token and not (username and password):
        raise ValueError(
            "Web session token or login credentials are not configured. "
            "Set EBOEKHOUDEN_MCP_WEB_SESSION_TOKEN or store web_username and web_password "
            "in keyring (service 'eboekhouden_mcp')."
        )
    return WebSessionClient(
        auth_token=token,
        base_url=base_url,
        username=username,
        password=password,
        totp_secret=totp_secret,
        keyring_service_name=service_name,
        persist_to_keyring=bool(username and password),
    )


class GetUnprocessedBankTransactionsInput(ToolSchema):
    """Input schema for get_unprocessed_bank_transactions."""

    offset: int = Field(
        default=0, ge=0, description="Offset for pagination (0 or greater)"
    )
    limit: int = Field(
        default=50, ge=1, le=500, description="Maximum items to return (1-500)"
    )


class GetUnprocessedBankTransactionsTool(BaseTool):
    """Inspect unprocessed bank statement lines from the e-Boekhouden bank import grid."""

    name = "get_unprocessed_bank_transactions"
    description = (
        "Inspect unprocessed bank statement transactions directly from the e-Boekhouden import grid. "
        "Returns raw and parsed bank statement rows awaiting booking or reconciliation."
    )
    input_schema = GetUnprocessedBankTransactionsInput

    def __init__(self, client: WebSessionClient | None = None) -> None:
        self._web_client = client

    def set_client(self, client: WebSessionClient) -> None:
        """Inject web session client."""
        self._web_client = client

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        web_client = _resolve_web_client(self._web_client)
        offset = int(arguments.get("offset", 0))
        limit = int(arguments.get("limit", 50))
        return await web_client.async_get_import_grid(offset=offset, limit=limit)


class SuggestMutationForBankStatementInput(ToolSchema):
    """Input schema for suggest_mutation_for_bank_statement."""

    id: int = Field(description="Bank statement row ID from import grid")
    rekening: str = Field(description="Bank account number / IBAN")
    mut_datum: str = Field(description="Transaction date (YYYY-MM-DD)")
    mut_bedrag: float = Field(
        description="Transaction amount (negative for money sent/debit, positive for money received/credit)"
    )
    mut_omschrijving: str = Field(description="Transaction description text")
    mut_factuur: str | None = Field(
        default="", description="Invoice number reference if known"
    )
    grootboek_id: int | None = Field(
        default=0, description="Bank ledger account ID if known"
    )


class SuggestMutationForBankStatementTool(BaseTool):
    """Get e-Boekhouden mutation prefill suggestion for a bank statement line."""

    name = "suggest_mutation_for_bank_statement"
    description = (
        "Retrieve pre-fill mutation suggestion from e-Boekhouden's internal matching engine "
        "for a specific bank statement row (matching invoices, counter-accounts, and ledger codes)."
    )
    input_schema = SuggestMutationForBankStatementInput

    def __init__(self, client: WebSessionClient | None = None) -> None:
        self._web_client = client

    def set_client(self, client: WebSessionClient) -> None:
        """Inject web session client."""
        self._web_client = client

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        web_client = _resolve_web_client(self._web_client)
        params = build_afschrift_params(
            id=int(arguments["id"]),
            rekening=str(arguments["rekening"]),
            mut_datum=str(arguments["mut_datum"]),
            mut_bedrag=float(arguments["mut_bedrag"]),
            mut_omschrijving=str(arguments["mut_omschrijving"]),
            mut_factuur=str(arguments.get("mut_factuur") or ""),
            grootboek_id=int(arguments.get("grootboek_id") or 0),
        )
        return await web_client.async_get_mutatie_by_afschrift(params)


class AttachFileToMutationInput(ToolSchema):
    """Input schema for attach_file_to_mutation."""

    mutation_id: int = Field(
        ge=1, description="ID (mutatie nummer) of the mutation to link"
    )
    file_id: int = Field(ge=1, description="Archive file ID in the digital archive")


class AttachFileToMutationTool(BaseTool):
    """Attach an uploaded archive file to a mutation."""

    name = "attach_file_to_mutation"
    description = (
        "Attach a digital archive file to an existing mutation in e-Boekhouden "
        "via /v1/api/folderkoppel/koppel/item. Links invoices/receipts directly to journal entries."
    )
    input_schema = AttachFileToMutationInput

    def __init__(self, client: WebSessionClient | None = None) -> None:
        self._web_client = client

    def set_client(self, client: WebSessionClient) -> None:
        """Inject web session client."""
        self._web_client = client

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        web_client = _resolve_web_client(self._web_client)
        mutation_id = int(arguments["mutation_id"])
        file_id = int(arguments["file_id"])
        result = await web_client.async_link_file_to_mutation(
            mutation_id=mutation_id, file_id=file_id
        )
        return {
            "success": True,
            "mutation_id": mutation_id,
            "file_id": file_id,
            "response": result,
        }


class ListArchiveFoldersInput(ToolSchema):
    """Input schema for list_archive_folders."""


class ListArchiveFoldersTool(BaseTool):
    """List Digital Archive folders (including Mail & Herken inbox folders)."""

    name = "list_archive_folders"
    description = (
        "List Digital Archive folders (including Mail & Herken inbox folders) "
        "from /v1/api/folder/directory."
    )
    input_schema = ListArchiveFoldersInput

    def __init__(self, client: WebSessionClient | None = None) -> None:
        self._web_client = client

    def set_client(self, client: WebSessionClient) -> None:
        """Inject web session client."""
        self._web_client = client

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        web_client = _resolve_web_client(self._web_client)
        res = await web_client.async_get_archive_folders()
        if isinstance(res, list):
            return {"count": len(res), "folders": res}
        return res


class ListArchiveFilesInput(ToolSchema):
    """Input schema for list_archive_files."""

    folder_id: int = Field(ge=1, description="Digital Archive folder ID")
    include_deleted: bool = Field(
        default=False, description="Whether to include deleted files in the listing"
    )


class ListArchiveFilesTool(BaseTool):
    """List files and recognition status inside a Digital Archive folder."""

    name = "list_archive_files"
    description = (
        "List files and Mail & Herken recognition status (menHStatus, koppelCount) "
        "inside a Digital Archive folder via /v1/api/folder/{folder_id}/filelist."
    )
    input_schema = ListArchiveFilesInput

    def __init__(self, client: WebSessionClient | None = None) -> None:
        self._web_client = client

    def set_client(self, client: WebSessionClient) -> None:
        """Inject web session client."""
        self._web_client = client

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        web_client = _resolve_web_client(self._web_client)
        folder_id = int(arguments["folder_id"])
        include_deleted = bool(arguments.get("include_deleted", False))
        res = await web_client.async_get_archive_files(
            folder_id=folder_id, include_deleted=include_deleted
        )
        if isinstance(res, list):
            return {"folder_id": folder_id, "count": len(res), "files": res}
        return res


class GetRecognizedInvoiceModelInput(ToolSchema):
    """Input schema for get_recognized_invoice_model."""

    file_id: int = Field(ge=1, description="Digital Archive file ID")


class GetRecognizedInvoiceModelTool(BaseTool):
    """Retrieve the internal AI OCR recognition model for a Digital Archive file."""

    name = "get_recognized_invoice_model"
    description = (
        "Retrieve the internal AI OCR recognition model (mutatie, mutatieRegels, "
        "suggestedAiIban, suggestedAiBtwNr) for a file in the Digital Archive "
        "via /v1/api/mutatie/folder/{file_id}/model."
    )
    input_schema = GetRecognizedInvoiceModelInput

    def __init__(self, client: WebSessionClient | None = None) -> None:
        self._web_client = client

    def set_client(self, client: WebSessionClient) -> None:
        """Inject web session client."""
        self._web_client = client

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        web_client = _resolve_web_client(self._web_client)
        file_id = int(arguments["file_id"])
        return await web_client.async_get_recognized_invoice_model(file_id=file_id)


class GetMutationModelByNumberInput(ToolSchema):
    """Input schema for get_mutation_model_by_number."""

    mutation_number: int = Field(
        ge=1, description="Existing mutation number (mutatienummer / nr)"
    )


class GetMutationModelByNumberTool(BaseTool):
    """Retrieve the editable web mutation model by mutation number (nr)."""

    name = "get_mutation_model_by_number"
    description = (
        "Retrieve the editable web mutation model (mutatie, mutatieRegels, folderKoppelId) "
        "by mutation number (nr) via /v1/api/mutatie/nr/{mutation_number}/model."
    )
    input_schema = GetMutationModelByNumberInput

    def __init__(self, client: WebSessionClient | None = None) -> None:
        self._web_client = client

    def set_client(self, client: WebSessionClient) -> None:
        """Inject web session client."""
        self._web_client = client

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        web_client = _resolve_web_client(self._web_client)
        mutation_number = int(arguments["mutation_number"])
        return await web_client.async_get_mutation_model_by_number(
            mutation_number=mutation_number
        )


class SaveWebMutationInput(ToolSchema):
    """Input schema for save_web_mutation."""

    payload: dict[str, Any] = Field(
        description="Web mutation model payload (containing mutatie, mutatieRegels, and optional folderKoppelId)"
    )
    confirm_write: bool | None = Field(
        default=None,
        description="Explicit confirmation flag for creating or updating the mutation",
    )


class SaveWebMutationTool(BaseTool):
    """Create or update a mutation via the web session API."""

    name = "save_web_mutation"
    description = (
        "Create or update a mutation via the web session API (POST /v1/api/mutatie) "
        "using the web mutation model format (mutatie, mutatieRegels, folderKoppelId)."
    )
    input_schema = SaveWebMutationInput

    def __init__(self, client: WebSessionClient | None = None) -> None:
        self._web_client = client

    def set_client(self, client: WebSessionClient) -> None:
        """Inject web session client."""
        self._web_client = client

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        web_client = _resolve_web_client(self._web_client)
        payload = dict(arguments["payload"])
        result = await web_client.async_save_web_mutation(payload=payload)
        return {
            "success": True,
            "response": result,
        }


class MoveArchiveFilesInput(ToolSchema):
    """Input schema for move_archive_files."""

    file_ids: list[int] = Field(
        min_length=1, description="List of Digital Archive file IDs to move"
    )
    target_folder_id: int = Field(
        ge=0, description="Target Digital Archive folder ID (0 for root)"
    )
    confirm_write: bool | None = Field(
        default=None,
        description="Explicit confirmation flag for moving Digital Archive files",
    )


class MoveArchiveFilesTool(BaseTool):
    """Move files between Digital Archive folders."""

    name = "move_archive_files"
    description = (
        "Move one or more Digital Archive files into a target folder "
        "via POST /v1/api/folder/bulk/update/{target_folder_id}."
    )
    input_schema = MoveArchiveFilesInput

    def __init__(self, client: WebSessionClient | None = None) -> None:
        self._web_client = client

    def set_client(self, client: WebSessionClient) -> None:
        """Inject web session client."""
        self._web_client = client

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        web_client = _resolve_web_client(self._web_client)
        file_ids = [int(fid) for fid in arguments["file_ids"]]
        target_folder_id = int(arguments["target_folder_id"])
        result = await web_client.async_move_archive_files(
            file_ids=file_ids, target_folder_id=target_folder_id
        )
        return {
            "success": True,
            "target_folder_id": target_folder_id,
            "file_ids": file_ids,
            "response": result,
        }


class CreateArchiveFolderInput(ToolSchema):
    """Input schema for create_archive_folder."""

    name: str = Field(
        min_length=1, description="Name of the new Digital Archive folder"
    )
    parent_folder_id: int = Field(
        default=0, ge=0, description="Parent folder ID (default 0 for root)"
    )
    confirm_write: bool | None = Field(
        default=None,
        description="Explicit confirmation flag for creating a Digital Archive folder",
    )


class CreateArchiveFolderTool(BaseTool):
    """Create a new folder in the Digital Archive."""

    name = "create_archive_folder"
    description = (
        "Create a new folder in the Digital Archive via POST /v1/api/folder/directory."
    )
    input_schema = CreateArchiveFolderInput

    def __init__(self, client: WebSessionClient | None = None) -> None:
        self._web_client = client

    def set_client(self, client: WebSessionClient) -> None:
        """Inject web session client."""
        self._web_client = client

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        web_client = _resolve_web_client(self._web_client)
        name = str(arguments["name"])
        parent_folder_id = int(arguments.get("parent_folder_id", 0))
        result = await web_client.async_create_archive_folder(
            name=name, parent_folder_id=parent_folder_id
        )
        return {
            "success": True,
            "name": name,
            "parent_folder_id": parent_folder_id,
            "response": result,
        }


class UploadArchiveFileInput(ToolSchema):
    """Input schema for upload_archive_file."""

    file_path: str = Field(
        min_length=1,
        description="Local filesystem path to the file to upload (must not be on a cloud/UNC drive)",
    )
    folder_id: int = Field(
        default=0,
        ge=0,
        description="Target Digital Archive folder ID (default 0 for root)",
    )
    file_name: str | None = Field(
        default=None,
        description="Optional override filename in the Digital Archive (defaults to the local file basename)",
    )
    overwrite: bool = Field(
        default=True,
        description="Whether to overwrite an existing file with the same name in the folder",
    )
    confirm_write: bool | None = Field(
        default=None,
        description="Explicit confirmation flag for uploading a Digital Archive file",
    )


class UploadArchiveFileTool(BaseTool):
    """Upload a local file to a Digital Archive folder without streaming Base64 through the LLM."""

    name = "upload_archive_file"
    description = (
        "Upload a local file directly from disk into a Digital Archive folder "
        "via POST /v1/api/folder/upload without streaming Base64 payloads through tool arguments."
    )
    input_schema = UploadArchiveFileInput

    def __init__(self, client: WebSessionClient | None = None) -> None:
        self._web_client = client

    def set_client(self, client: WebSessionClient) -> None:
        """Inject web session client."""
        self._web_client = client

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        web_client = _resolve_web_client(self._web_client)
        file_path = str(arguments["file_path"])
        folder_id = int(arguments.get("folder_id", 0))
        file_name = arguments.get("file_name")
        overwrite = bool(arguments.get("overwrite", True))
        result = await web_client.async_upload_archive_file(
            file_path=file_path,
            folder_id=folder_id,
            file_name=str(file_name) if file_name else None,
            overwrite=overwrite,
        )
        return {
            "success": True,
            "file_path": file_path,
            "folder_id": folder_id,
            "response": result,
        }


class DeleteWebMutationsInput(ToolSchema):
    """Input schema for delete_web_mutations."""

    mutation_numbers: list[int] = Field(
        min_length=1,
        description="List of mutation numbers (mutatienummer / nr) to delete",
    )
    confirm_write: bool | None = Field(
        default=None,
        description="Explicit confirmation flag for deleting mutations",
    )


class DeleteWebMutationsTool(BaseTool):
    """Delete one or more mutations by mutation number (nr) via the web session API."""

    name = "delete_web_mutations"
    description = (
        "Resolve mutation numbers (nr) to internal grid IDs via /v1/api/mutatie/gridtable "
        "and delete them via POST /v1/api/mutatie/bulk/delete."
    )
    input_schema = DeleteWebMutationsInput

    def __init__(self, client: WebSessionClient | None = None) -> None:
        self._web_client = client

    def set_client(self, client: WebSessionClient) -> None:
        """Inject web session client."""
        self._web_client = client

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        web_client = _resolve_web_client(self._web_client)
        mutation_numbers = [int(nr) for nr in arguments["mutation_numbers"]]
        result = await web_client.async_delete_web_mutations(
            mutation_numbers=mutation_numbers
        )
        return {
            "success": True,
            **result,
        }


class ListTimeEntriesInput(ToolSchema):
    """Input schema for list_time_entries."""

    status: str = Field(
        default="uninvoiced",
        description=(
            "Filter by invoicing status: 'uninvoiced' (default, unbilled WIP hours in Uren -> Factureren), "
            "'invoiced' (already billed hours), or 'all' (all logged hours)."
        ),
    )
    project_id: int | None = Field(
        default=None,
        description="Optional e-Boekhouden time-tracking project ID filter",
    )
    activity_id: int | None = Field(
        default=None,
        description="Optional e-Boekhouden activity ID filter",
    )
    period_start: str | None = Field(
        default=None,
        description="Optional start date filter (YYYY-MM-DD)",
    )
    period_end: str | None = Field(
        default=None,
        description="Optional end date filter (YYYY-MM-DD)",
    )
    include_non_billable: bool = Field(
        default=False,
        description="Whether to include non-billable time entries (nietFacturabel)",
    )
    include_notes: bool = Field(
        default=True,
        description="Whether to include detailed work notes (opmerkingen) per entry",
    )
    max_rows: int = Field(
        default=500,
        ge=1,
        le=2000,
        description="Maximum number of time entry rows to fetch across automatic 50-row pages",
    )


class ListTimeEntriesTool(BaseTool):
    """List logged hours (uninvoiced WIP, invoiced, or all) with per-project financial summary."""

    name = "list_time_entries"
    description = (
        "Fetch logged time entries and uninvoiced WIP hours from e-Boekhouden Urenregistratie "
        "(/v1/api/uur/factureren/gridtable) with automatic 50-row pagination and a per-project "
        "breakdown of hours, kilometers, and amounts (excl./incl. VAT)."
    )
    input_schema = ListTimeEntriesInput

    def __init__(self, client: WebSessionClient | None = None) -> None:
        self._web_client = client

    def set_client(self, client: WebSessionClient) -> None:
        """Inject web session client."""
        self._web_client = client

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        web_client = _resolve_web_client(self._web_client)
        return await web_client.async_get_time_entries(
            status=str(arguments.get("status", "uninvoiced")),
            project_id=arguments.get("project_id"),
            activity_id=arguments.get("activity_id"),
            period_start=arguments.get("period_start"),
            period_end=arguments.get("period_end"),
            include_non_billable=bool(arguments.get("include_non_billable", False)),
            include_notes=bool(arguments.get("include_notes", True)),
            max_rows=int(arguments.get("max_rows", 500)),
        )


class ListTimeProjectsInput(ToolSchema):
    """Input schema for list_time_projects."""

    active_only: bool = Field(
        default=False,
        description="If true, return only active time-tracking projects",
    )


class ListTimeProjectsTool(BaseTool):
    """List time-tracking projects in e-Boekhouden Urenregistratie."""

    name = "list_time_projects"
    description = (
        "List time-tracking projects and their linked client relations from e-Boekhouden "
        "Urenregistratie (/v1/api/project/getall and /v1/api/project/gridtable)."
    )
    input_schema = ListTimeProjectsInput

    def __init__(self, client: WebSessionClient | None = None) -> None:
        self._web_client = client

    def set_client(self, client: WebSessionClient) -> None:
        """Inject web session client."""
        self._web_client = client

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        web_client = _resolve_web_client(self._web_client)
        active_only = bool(arguments.get("active_only", False))
        projects = await web_client.async_get_time_projects(active_only=active_only)
        return {
            "count": len(projects),
            "projects": projects,
        }


class ListTimeActivitiesInput(ToolSchema):
    """Input schema for list_time_activities."""

    active_only: bool = Field(
        default=False,
        description="If true, return only active time-tracking activities",
    )


class ListTimeActivitiesTool(BaseTool):
    """List time-tracking activities and their hourly/kilometer rates."""

    name = "list_time_activities"
    description = (
        "List time-tracking activities, hourly rates, kilometer rates, VAT codes, and revenue ledgers "
        "from e-Boekhouden Urenregistratie (/v1/api/activiteit/getall)."
    )
    input_schema = ListTimeActivitiesInput

    def __init__(self, client: WebSessionClient | None = None) -> None:
        self._web_client = client

    def set_client(self, client: WebSessionClient) -> None:
        """Inject web session client."""
        self._web_client = client

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        web_client = _resolve_web_client(self._web_client)
        active_only = bool(arguments.get("active_only", False))
        activities = await web_client.async_get_time_activities(active_only=active_only)
        return {
            "count": len(activities),
            "activities": activities,
        }


class ListQuotesInput(ToolSchema):
    """Input schema for list_quotes."""

    offset: int = Field(
        default=0, ge=0, description="Offset for pagination (0 or greater)"
    )
    limit: int = Field(
        default=50, ge=1, le=500, description="Maximum items to return (1-500)"
    )
    quote_number: str | None = Field(
        default=None, description="Filter by quote number (e.g. 'OF-00001')"
    )
    relation_id: int | None = Field(
        default=None, ge=1, description="Filter by client relation ID"
    )
    status_id: int | None = Field(
        default=None, ge=1, description="Filter by quote status ID"
    )
    assigned_user_id: int | None = Field(
        default=None, ge=1, description="Filter by assigned user ID (inBehandelingBij)"
    )
    date_from: str | None = Field(
        default=None, description="Filter quote date from (YYYY-MM-DD)"
    )
    date_to: str | None = Field(
        default=None, description="Filter quote date to (YYYY-MM-DD)"
    )
    expiration_from: str | None = Field(
        default=None, description="Filter expiration date from (YYYY-MM-DD)"
    )
    expiration_to: str | None = Field(
        default=None, description="Filter expiration date to (YYYY-MM-DD)"
    )
    include_metadata: bool = Field(
        default=True,
        description="Include available quote statuses, templates, users, and next quote number",
    )


class ListQuotesTool(BaseTool):
    """List quotes (offertes) in e-Boekhouden with optional filters and metadata."""

    name = "list_quotes"
    description = (
        "List quotes (offertes) from e-Boekhouden (/v1/api/offerte/gridtable) with optional "
        "filters for quote number, client relation, status, assigned user, and dates, plus "
        "available quote statuses, templates, and next quote number."
    )
    input_schema = ListQuotesInput

    def __init__(self, client: WebSessionClient | None = None) -> None:
        self._web_client = client

    def set_client(self, client: WebSessionClient) -> None:
        """Inject web session client."""
        self._web_client = client

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        web_client = _resolve_web_client(self._web_client)
        return await web_client.async_list_quotes(
            offset=int(arguments.get("offset", 0)),
            limit=int(arguments.get("limit", 50)),
            quote_number=arguments.get("quote_number"),
            relation_id=arguments.get("relation_id"),
            status_id=arguments.get("status_id"),
            assigned_user_id=arguments.get("assigned_user_id"),
            date_from=arguments.get("date_from"),
            date_to=arguments.get("date_to"),
            expiration_from=arguments.get("expiration_from"),
            expiration_to=arguments.get("expiration_to"),
            include_metadata=bool(arguments.get("include_metadata", True)),
        )


class GetQuoteInput(ToolSchema):
    """Input schema for get_quote."""

    quote_id: int | None = Field(
        default=None, ge=1, description="Internal quote (offerte) ID"
    )
    quote_number: str | None = Field(
        default=None,
        min_length=1,
        description="Quote number (e.g. 'OF-00001') if quote_id is not known",
    )

    @model_validator(mode="after")
    def _require_id_or_number(self) -> "GetQuoteInput":
        if self.quote_id is None and not (
            self.quote_number and self.quote_number.strip()
        ):
            raise ValueError("Either quote_id or quote_number must be provided.")
        return self


class GetQuoteTool(BaseTool):
    """Get full details of a quote (offerte) by ID or quote number."""

    name = "get_quote"
    description = (
        "Get full details of an e-Boekhouden quote (offerte) by quote_id or quote_number, "
        "including header metadata, line items (offerteregel), financial totals, and attached Digital Archive files."
    )
    input_schema = GetQuoteInput

    def __init__(self, client: WebSessionClient | None = None) -> None:
        self._web_client = client

    def set_client(self, client: WebSessionClient) -> None:
        """Inject web session client."""
        self._web_client = client

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        web_client = _resolve_web_client(self._web_client)
        return await web_client.async_get_quote(
            quote_id=arguments.get("quote_id"),
            quote_number=arguments.get("quote_number"),
        )


class QuoteItemInput(ToolSchema):
    """Input schema for a quote (offerte) line item."""

    description: str = Field(
        min_length=1,
        max_length=8000,
        description="Line item description (omschrijving)",
    )
    price_per_unit: float = Field(
        description="Price per unit (excl. VAT when in_ex_vat='EX', incl. VAT when in_ex_vat='IN')"
    )
    quantity: float = Field(default=1.0, description="Quantity (aantal, default 1.0)")
    vat_code: str = Field(
        default="HOOG_VERK_21",
        description="VAT code (e.g. 'HOOG_VERK_21', 'BI_EU_VERK_D', 'BU_EU_VERK', 'LAAG_VERK_9', 'VERL_VERK', 'GEEN')",
    )
    ledger_id: int | None = Field(
        default=None,
        ge=1,
        description="Revenue ledger ID or code (auto-resolved from vat_code if omitted, e.g. 810200 / 810300)",
    )
    cost_center_id: int | None = Field(
        default=None, ge=1, description="Optional cost center ID (kostenplaatsId)"
    )
    unit_id: int | None = Field(
        default=None,
        ge=1,
        description="Optional unit ID (eenheidId, e.g. uur/stuk/km)",
    )
    code: str | None = Field(
        default=None,
        max_length=20,
        description="Optional item/article code (max 20 chars)",
    )
    product_id: int | None = Field(
        default=None,
        ge=1,
        description="Optional standard product/service ID (artikelId)",
    )
    discount: float | None = Field(
        default=None, description="Optional discount (korting) percentage or amount"
    )
    discount_is_percentage: bool = Field(
        default=True,
        description="Whether discount is a percentage (true) or fixed amount (false)",
    )


class CreateQuoteInput(ToolSchema):
    """Input schema for create_quote."""

    relation_id: int = Field(ge=1, description="Relation (customer) ID")
    items: list[QuoteItemInput] | None = Field(
        default=None,
        max_length=500,
        description="Quote line items (optional; each item has description, price_per_unit, quantity, vat_code, etc.)",
    )
    quote_number: str | None = Field(
        default=None,
        max_length=50,
        description="Custom quote number (auto-generated via /v1/api/offerte/nummer/next if omitted)",
    )
    date: str | None = Field(
        default=None, description="Quote date (YYYY-MM-DD, defaults to today)"
    )
    expiration_date: str | None = Field(
        default=None,
        description="Expiration date (YYYY-MM-DD, defaults to quote date + expiration_days)",
    )
    expiration_days: int = Field(
        default=30,
        ge=0,
        le=3650,
        description="Default validity period in days if expiration_date is omitted (default 30)",
    )
    status_id: int | None = Field(
        default=None, ge=1, description="Optional quote status ID (offertestatus)"
    )
    status: str | None = Field(
        default=None,
        description=(
            "Optional status name/substring (e.g. 'Ingevoerd', 'Verzonden', 'Wacht op antwoord', "
            "'Geaccepteerd', 'Gefactureerd'). Defaults to '01. Ingevoerd'."
        ),
    )
    template_id: int | None = Field(
        default=None,
        ge=1,
        description="Optional quote template ID (offertesjabloon, defaults to first active template)",
    )
    assigned_user_id: int | None = Field(
        default=None,
        ge=1,
        description="Optional assigned user ID (behandelGebruikerId, defaults to primary user)",
    )
    in_ex_vat: Literal["EX", "IN"] = Field(
        default="EX",
        description="'EX' for VAT exclusive (default) or 'IN' for VAT inclusive",
    )
    reference: str | None = Field(
        default=None,
        max_length=50,
        description="Customer reference / project UPID (kenmerk, max 50 chars)",
    )
    public_note: str | None = Field(
        default=None,
        max_length=15000,
        description="Customer-facing note (notitiePubliek, max 15000 chars)",
    )
    internal_note: str | None = Field(
        default=None,
        max_length=15000,
        description="Internal accounting/commercial note (notitieIntern, max 15000 chars)",
    )
    archive_file_id: int | None = Field(
        default=None,
        ge=1,
        description="Optional Digital Archive file ID (e.g. uploaded branded PDF offer) to link to this quote",
    )
    confirm_write: bool | None = Field(
        default=None,
        description="Explicit confirmation flag for creating a quote",
    )


class CreateQuoteTool(BaseTool):
    """Create a commercial quote (offerte) in e-Boekhouden with line items and optional PDF attachment."""

    name = "create_quote"
    description = (
        "Create a new quote (offerte) in e-Boekhouden via POST /v1/api/offerte, insert line items "
        "via POST /v1/api/offerteregel, and optionally attach a branded PDF from the Digital Archive."
    )
    input_schema = CreateQuoteInput

    def __init__(self, client: WebSessionClient | None = None) -> None:
        self._web_client = client

    def set_client(self, client: WebSessionClient) -> None:
        """Inject web session client."""
        self._web_client = client

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        web_client = _resolve_web_client(self._web_client)
        quote = await web_client.async_create_quote(
            relation_id=int(arguments["relation_id"]),
            items=arguments.get("items"),
            quote_number=arguments.get("quote_number"),
            date=arguments.get("date"),
            expiration_date=arguments.get("expiration_date"),
            expiration_days=int(arguments.get("expiration_days", 30)),
            status_id=arguments.get("status_id"),
            status=arguments.get("status"),
            template_id=arguments.get("template_id"),
            assigned_user_id=arguments.get("assigned_user_id"),
            in_ex_vat=str(arguments.get("in_ex_vat", "EX")),
            reference=str(arguments.get("reference") or ""),
            public_note=str(arguments.get("public_note") or ""),
            internal_note=str(arguments.get("internal_note") or ""),
            archive_file_id=arguments.get("archive_file_id"),
        )
        return {
            "success": True,
            "quote": quote,
        }


class UpdateQuoteInput(ToolSchema):
    """Input schema for update_quote."""

    quote_id: int = Field(ge=1, description="Internal quote (offerte) ID to update")
    relation_id: int | None = Field(
        default=None, ge=1, description="Updated customer relation ID"
    )
    quote_number: str | None = Field(
        default=None, max_length=50, description="Updated quote number"
    )
    date: str | None = Field(
        default=None, description="Updated quote date (YYYY-MM-DD)"
    )
    expiration_date: str | None = Field(
        default=None, description="Updated expiration date (YYYY-MM-DD)"
    )
    status_id: int | None = Field(
        default=None, ge=1, description="Updated quote status ID"
    )
    status: str | None = Field(
        default=None,
        description=(
            "Updated status name/substring (e.g. 'Ingevoerd', 'Verzonden', 'Wacht op antwoord', "
            "'Geaccepteerd', 'Gefactureerd')"
        ),
    )
    template_id: int | None = Field(
        default=None, ge=1, description="Updated quote template ID"
    )
    assigned_user_id: int | None = Field(
        default=None, ge=1, description="Updated assigned user ID"
    )
    in_ex_vat: Literal["EX", "IN"] | None = Field(
        default=None, description="'EX' for VAT exclusive or 'IN' for VAT inclusive"
    )
    reference: str | None = Field(
        default=None, max_length=50, description="Updated customer reference (kenmerk)"
    )
    public_note: str | None = Field(
        default=None,
        max_length=15000,
        description="Updated customer-facing note (notitiePubliek)",
    )
    internal_note: str | None = Field(
        default=None,
        max_length=15000,
        description="Updated internal note (notitieIntern)",
    )
    items: list[QuoteItemInput] | None = Field(
        default=None,
        max_length=500,
        description="Optional quote line items to replace (or append to) existing line items",
    )
    replace_items: bool = Field(
        default=True,
        description="If true (default) and items is provided, replaces existing line items; if false, appends items",
    )
    archive_file_id: int | None = Field(
        default=None,
        ge=1,
        description="Optional Digital Archive file ID to link to this quote",
    )
    confirm_write: bool | None = Field(
        default=None,
        description="Explicit confirmation flag for updating a quote",
    )


class UpdateQuoteTool(BaseTool):
    """Update an existing quote (offerte), its status, line items, or linked archive PDF."""

    name = "update_quote"
    description = (
        "Update an existing quote (offerte) in e-Boekhouden via PUT /v1/api/offerte (e.g. update "
        "status to '02. Verzonden' or '04. Geaccepteerd', update notes/dates, replace or append "
        "line items, or attach a Digital Archive PDF)."
    )
    input_schema = UpdateQuoteInput

    def __init__(self, client: WebSessionClient | None = None) -> None:
        self._web_client = client

    def set_client(self, client: WebSessionClient) -> None:
        """Inject web session client."""
        self._web_client = client

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        web_client = _resolve_web_client(self._web_client)
        quote = await web_client.async_update_quote(
            quote_id=int(arguments["quote_id"]),
            relation_id=arguments.get("relation_id"),
            quote_number=arguments.get("quote_number"),
            date=arguments.get("date"),
            expiration_date=arguments.get("expiration_date"),
            status_id=arguments.get("status_id"),
            status=arguments.get("status"),
            template_id=arguments.get("template_id"),
            assigned_user_id=arguments.get("assigned_user_id"),
            in_ex_vat=arguments.get("in_ex_vat"),
            reference=arguments.get("reference"),
            public_note=arguments.get("public_note"),
            internal_note=arguments.get("internal_note"),
            items=arguments.get("items"),
            replace_items=bool(arguments.get("replace_items", True)),
            archive_file_id=arguments.get("archive_file_id"),
        )
        return {
            "success": True,
            "quote": quote,
        }


class AttachFileToQuoteInput(ToolSchema):
    """Input schema for attach_file_to_quote."""

    quote_id: int = Field(ge=1, description="Internal quote (offerte) ID")
    file_id: int = Field(
        ge=1, description="Digital Archive file ID (folder/file ID) to link"
    )
    confirm_write: bool | None = Field(
        default=None,
        description="Explicit confirmation flag for linking a file to a quote",
    )


class AttachFileToQuoteTool(BaseTool):
    """Attach an uploaded Digital Archive file (e.g. branded PDF offer) to a quote (offerte)."""

    name = "attach_file_to_quote"
    description = (
        "Attach a Digital Archive file (such as an uploaded branded PDF offer) to an existing "
        "quote (offerte) via POST /v1/api/folderkoppel/koppel/item (koppelType='OF')."
    )
    input_schema = AttachFileToQuoteInput

    def __init__(self, client: WebSessionClient | None = None) -> None:
        self._web_client = client

    def set_client(self, client: WebSessionClient) -> None:
        """Inject web session client."""
        self._web_client = client

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        web_client = _resolve_web_client(self._web_client)
        quote_id = int(arguments["quote_id"])
        file_id = int(arguments["file_id"])
        result = await web_client.async_link_file_to_quote(
            quote_id=quote_id, file_id=file_id
        )
        return {
            "success": True,
            "quote_id": quote_id,
            "file_id": file_id,
            "response": result,
        }


class DeleteQuoteInput(ToolSchema):
    """Input schema for delete_quote."""

    quote_id: int = Field(ge=1, description="Internal quote (offerte) ID to delete")
    confirm_write: bool | None = Field(
        default=None,
        description="Explicit confirmation flag for deleting a quote",
    )


class DeleteQuoteTool(BaseTool):
    """Delete a quote (offerte) by ID via the web session API."""

    name = "delete_quote"
    description = (
        "Delete a quote (offerte) by ID via DELETE /v1/api/offerte/{quote_id}."
    )
    input_schema = DeleteQuoteInput

    def __init__(self, client: WebSessionClient | None = None) -> None:
        self._web_client = client

    def set_client(self, client: WebSessionClient) -> None:
        """Inject web session client."""
        self._web_client = client

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        web_client = _resolve_web_client(self._web_client)
        quote_id = int(arguments["quote_id"])
        result = await web_client.async_delete_quote(quote_id=quote_id)
        return {
            "success": True,
            **result,
        }


class SendInvoiceInput(ToolSchema):
    """Input schema for send_invoice."""

    invoice_id: int = Field(ge=1, description="Internal invoice ID (e.g. 73143509)")
    email_template_id: int | None = Field(
        default=None,
        ge=1,
        description="Email template ID to format subject and body (e.g. 1332610 for 'Normaal Factuur')",
    )
    recipient_email: str | None = Field(
        default=None,
        description="Recipient email address (defaults to customer email configured on invoice/template)",
    )
    subject: str | None = Field(
        default=None,
        max_length=200,
        description="Custom email subject (overrides template subject)",
    )
    body: str | None = Field(
        default=None,
        max_length=100000,
        description="Custom email body HTML or text (overrides template body)",
    )
    sender_email: str | None = Field(
        default=None,
        max_length=150,
        description="Sender email address (overrides default administration email)",
    )
    sender_name: str | None = Field(
        default=None,
        max_length=100,
        description="Sender display name (overrides default sender name)",
    )
    cc_email: str | None = Field(
        default=None,
        max_length=150,
        description="CC email address (optional)",
    )
    bcc_email: str | None = Field(
        default=None,
        max_length=150,
        description="BCC email address (optional)",
    )
    attach_ubl: bool = Field(
        default=True,
        description="Whether to attach the UBL e-invoice XML file (defaults to true)",
    )


class SendInvoiceTool(BaseTool):
    """Send an existing invoice by email via the web session API."""

    name = "send_invoice"
    description = (
        "Send an already-created invoice to a customer by email via POST /v1/api/factuur/mail. "
        "Supports formatting with an email template (e.g. template 1332610 'Normaal Factuur'), "
        "custom subjects/bodies, and attaching the UBL e-invoice XML."
    )
    input_schema = SendInvoiceInput

    def __init__(self, client: WebSessionClient | None = None) -> None:
        self._web_client = client

    def set_client(self, client: WebSessionClient) -> None:
        """Inject web session client."""
        self._web_client = client

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        web_client = _resolve_web_client(self._web_client)
        invoice_id = int(arguments["invoice_id"])
        recipient_email = arguments.get("recipient_email")

        # Fallback to REST client relation email if recipient_email is not specified
        if not recipient_email and client is not None:
            try:
                inv = client.invoices.get(invoice_id)
                if inv.relation_id:
                    rel = client.relations.get(inv.relation_id)
                    recipient_email = (
                        rel.email_address_invoice
                        or rel.email_address
                        or getattr(rel, "email", None)
                    )
            except Exception:  # nosec B110
                pass

        result = await web_client.async_send_invoice_email(
            invoice_id=invoice_id,
            recipient_email=recipient_email,
            sender_email=arguments.get("sender_email"),
            sender_name=arguments.get("sender_name"),
            subject=arguments.get("subject"),
            body=arguments.get("body"),
            cc_email=arguments.get("cc_email"),
            bcc_email=arguments.get("bcc_email"),
            attach_ubl=arguments.get("attach_ubl", True),
            email_template_id=arguments.get("email_template_id"),
        )
        return {
            "success": True,
            **result,
        }
