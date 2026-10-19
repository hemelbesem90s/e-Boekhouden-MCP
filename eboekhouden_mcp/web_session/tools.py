"""MCP tools for the e-Boekhouden web session bridge."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from pydantic import Field

from eboekhouden_mcp.tools.base import BaseTool, ToolSchema
from eboekhouden_mcp.web_session.client import (
    WebSessionClient,
    build_afschrift_params,
)

if TYPE_CHECKING:
    from eboekhouden import EBoekhoudenClient


def _resolve_web_client(client_override: WebSessionClient | None = None) -> WebSessionClient:
    """Resolve an active WebSessionClient using injection or environment token."""
    if client_override is not None:
        return client_override

    token = os.environ.get("EBOEKHOUDEN_MCP_WEB_SESSION_TOKEN") or os.environ.get(
        "EBOEKHOUDEN_WEB_SESSION_TOKEN", ""
    )
    base_url = os.environ.get(
        "EBOEKHOUDEN_MCP_WEB_SESSION_BASE_URL", "https://secure20.e-boekhouden.nl"
    )
    if not token:
        raise ValueError(
            "Web session token is not configured. Set EBOEKHOUDEN_MCP_WEB_SESSION_TOKEN "
            "or pass an authenticated WebSessionClient."
        )
    return WebSessionClient(auth_token=token, base_url=base_url)


class GetUnprocessedBankTransactionsInput(ToolSchema):
    """Input schema for get_unprocessed_bank_transactions."""

    offset: int = Field(default=0, ge=0, description="Offset for pagination (0 or greater)")
    limit: int = Field(default=50, ge=1, le=500, description="Maximum items to return (1-500)")


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
    mut_factuur: str | None = Field(default="", description="Invoice number reference if known")
    grootboek_id: int | None = Field(default=0, description="Bank ledger account ID if known")


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

    mutation_id: int = Field(ge=1, description="ID (mutatie nummer) of the mutation to link")
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
