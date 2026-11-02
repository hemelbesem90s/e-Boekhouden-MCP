"""Web session bridge module for e-Boekhouden cookie-based session automation."""

from eboekhouden_mcp.web_session.client import (
    SessionExpiredError,
    WebSessionClient,
    WebSessionError,
    build_afschrift_params,
    parse_gridtable_rows,
    parse_import_grid,
    summarize_time_entries,
)
from eboekhouden_mcp.web_session.tools import (
    AttachFileToMutationTool,
    GetMutationModelByNumberTool,
    GetRecognizedInvoiceModelTool,
    GetUnprocessedBankTransactionsTool,
    ListArchiveFilesTool,
    ListArchiveFoldersTool,
    ListTimeActivitiesTool,
    ListTimeEntriesTool,
    ListTimeProjectsTool,
    SaveWebMutationTool,
    SuggestMutationForBankStatementTool,
)

__all__ = [
    "WebSessionClient",
    "WebSessionError",
    "SessionExpiredError",
    "build_afschrift_params",
    "parse_gridtable_rows",
    "parse_import_grid",
    "summarize_time_entries",
    "GetUnprocessedBankTransactionsTool",
    "SuggestMutationForBankStatementTool",
    "AttachFileToMutationTool",
    "ListArchiveFoldersTool",
    "ListArchiveFilesTool",
    "GetRecognizedInvoiceModelTool",
    "GetMutationModelByNumberTool",
    "SaveWebMutationTool",
    "ListTimeEntriesTool",
    "ListTimeProjectsTool",
    "ListTimeActivitiesTool",
]
