"""AI Inbox module for Dutch invoice extraction, proposal validation, and bank classification."""

from eboekhouden_mcp.inbox.models import (
    BankTransactionSuggestion,
    BookingLine,
    BookingProposal,
    ExtractedInvoiceData,
    TaxAdviceTip,
)
from eboekhouden_mcp.inbox.prompts import (
    DUTCH_INVOICE_OCR_SYSTEM_PROMPT,
    DUTCH_TRANSACTION_CLASSIFICATION_PROMPT,
    build_invoice_ocr_prompt,
    build_transaction_classify_prompt,
)
from eboekhouden_mcp.inbox.proposal import (
    generate_booking_proposal,
    match_relation,
    suggest_bank_transaction_classification,
)

__all__ = [
    "BankTransactionSuggestion",
    "BookingLine",
    "BookingProposal",
    "ExtractedInvoiceData",
    "TaxAdviceTip",
    "DUTCH_INVOICE_OCR_SYSTEM_PROMPT",
    "DUTCH_TRANSACTION_CLASSIFICATION_PROMPT",
    "build_invoice_ocr_prompt",
    "build_transaction_classify_prompt",
    "generate_booking_proposal",
    "match_relation",
    "suggest_bank_transaction_classification",
]
