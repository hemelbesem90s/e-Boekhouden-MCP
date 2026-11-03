"""Shared constants for MCP tool policy and categorization."""

from __future__ import annotations

WRITE_TOOL_NAMES = frozenset(
    {
        "create_relation",
        "update_relation",
        "delete_relation",
        "create_invoice",
        "create_mutation",
        "create_product",
        "update_product",
        "delete_product",
        "create_ledger",
        "update_ledger",
        "create_cost_center",
        "delete_cost_center",
        "create_member",
        "delete_member",
        "send_file_to_digital_archive",
        "attach_file_to_mutation",
        "save_web_mutation",
        "move_archive_files",
        "create_archive_folder",
        "upload_archive_file",
        "delete_web_mutations",
        "create_quote",
        "update_quote",
        "attach_file_to_quote",
        "delete_quote",
        "send_invoice",
    }
)

DESTRUCTIVE_TOOL_NAMES = frozenset(
    {
        "delete_relation",
        "delete_product",
        "delete_cost_center",
        "delete_member",
        "delete_web_mutations",
        "delete_quote",
    }
)

ARCHIVE_TOOL_NAME = "send_file_to_digital_archive"

WEB_SESSION_TOOL_NAMES = frozenset(
    {
        "get_unprocessed_bank_transactions",
        "suggest_mutation_for_bank_statement",
        "attach_file_to_mutation",
        "list_archive_folders",
        "list_archive_files",
        "get_recognized_invoice_model",
        "get_mutation_model_by_number",
        "save_web_mutation",
        "move_archive_files",
        "create_archive_folder",
        "upload_archive_file",
        "delete_web_mutations",
        "list_time_entries",
        "list_time_projects",
        "list_time_activities",
        "list_quotes",
        "get_quote",
        "create_quote",
        "update_quote",
        "attach_file_to_quote",
        "delete_quote",
        "send_invoice",
    }
)

INBOX_TOOL_NAMES = frozenset(
    {
        "get_dutch_bookkeeping_rules",
        "prepare_booking_proposal",
        "suggest_transaction_classification",
    }
)

__all__ = [
    "WRITE_TOOL_NAMES",
    "DESTRUCTIVE_TOOL_NAMES",
    "ARCHIVE_TOOL_NAME",
    "WEB_SESSION_TOOL_NAMES",
    "INBOX_TOOL_NAMES",
]
