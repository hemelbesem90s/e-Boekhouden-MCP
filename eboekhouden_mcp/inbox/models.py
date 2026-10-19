"""Pydantic models for AI inbox invoice processing, proposals, and bank suggestions."""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class TaxAdviceTip(BaseModel):
    """Tax advice tip extracted or derived for an invoice."""

    type: Literal["kia", "vaste_activa", "gemengd_gebruik", "representatie", "geen_btw", "other"] | str = Field(
        description="Tax tip type category: kia, vaste_activa, gemengd_gebruik, representatie, geen_btw"
    )
    tekst: str = Field(description="Dutch tax advice explanation")

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class ExtractedInvoiceData(BaseModel):
    """Structured extraction output from Dutch invoice/receipt OCR."""

    leverancier: str = Field(description="Supplier/company name")
    factuurnummer: str | None = Field(default=None, description="Invoice or receipt number")
    datum: str = Field(description="Invoice date in YYYY-MM-DD format")
    bedrag_excl_btw: float = Field(alias="bedragExclBtw", description="Amount excluding VAT")
    bedrag_incl_btw: float = Field(alias="bedragInclBtw", description="Amount including VAT")
    btw_bedrag: float = Field(default=0.0, alias="btwBedrag", description="VAT amount")
    btw_percentage: float | None = Field(default=None, alias="btwPercentage", description="VAT percentage (0, 9, 21)")
    currency: str = Field(default="EUR", description="ISO 4217 currency code (e.g. EUR, USD, CHF)")
    omschrijving: str = Field(default="", description="Description of the goods or services")
    grootboekcode: str | None = Field(default=None, description="Suggested ledger code (e.g. 4100, 4300)")
    btw_code: str | None = Field(
        default=None,
        alias="btwCode",
        description="Dutch VAT code (e.g. HOOG_INK_21, LAAG_INK_9, GEEN, VERL_INK)",
    )
    is_reverse_charge: bool = Field(
        default=False,
        alias="isReverseCharge",
        description="True if invoice has reverse-charge VAT",
    )
    is_receipt: bool = Field(
        default=False,
        alias="isReceipt",
        description="True if document is a receipt/till slip (bonnetje) rather than a formal invoice",
    )
    receipt_reason: str = Field(
        default="",
        alias="receiptReason",
        description="Explanation why document was classified as receipt",
    )
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Extraction confidence score")
    redenering: str = Field(default="", description="Reasoning for ledger/VAT classification")
    belasting_advies: list[TaxAdviceTip] = Field(
        default_factory=list,
        alias="belastingAdvies",
        description="List of applicable Dutch tax advice tips",
    )

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class BookingLine(BaseModel):
    """Validated booking line item for a mutation or invoice."""

    ledger_id: int = Field(description="Ledger account ID")
    ledger_code: str | None = Field(default=None, description="Ledger account code (e.g. 4100)")
    vat_code: str = Field(description="VAT code, e.g. HOOG_INK_21 or GEEN")
    amount: float = Field(description="Line amount")
    vat_amount: float | None = Field(default=None, description="VAT amount for this line")
    cost_center_id: int | None = Field(default=None, description="Cost center ID if assigned")
    description: str | None = Field(default=None, description="Line description")

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class BookingProposal(BaseModel):
    """Fully validated booking proposal ready for e-Boekhouden mutation or invoice creation."""

    proposal_type: Literal["mutation", "invoice"] = Field(
        default="mutation",
        description="Whether proposal targets create_mutation or create_invoice",
    )
    mutation_type: str = Field(
        default="1",
        description="Mutation type string: '1'=Invoice received, '6'=Money spent, etc.",
    )
    date: str = Field(description="Booking date (YYYY-MM-DD)")
    ledger_id: int = Field(
        description="Main header ledger ID (e.g. 1600 Creditors or 1000 Bank/Cash)"
    )
    relation_id: int | None = Field(
        default=None,
        description="Matched relation/supplier ID (None for receipts without relation)",
    )
    relation_name: str | None = Field(default=None, description="Name of matched relation")
    relation_matched: bool = Field(
        default=False,
        description="Whether supplier was matched against an existing relation in e-Boekhouden",
    )
    invoice_number: str | None = Field(default=None, description="Invoice number")
    description: str | None = Field(default=None, description="Proposal description")
    in_ex_vat: Literal["IN", "EX"] = Field(
        default="EX",
        description="'EX' for VAT exclusive or 'IN' for VAT inclusive",
    )
    lines: list[BookingLine] = Field(
        default_factory=list,
        description="Validated booking lines for this proposal",
    )
    is_receipt: bool = Field(
        default=False,
        description="True if document is a receipt (bonnetje) suitable for direct mutation without creditor relation",
    )
    receipt_reason: str = Field(default="", description="Reason document is classified as receipt")
    confidence: float = Field(default=1.0, description="Overall proposal confidence")
    tax_advice: list[TaxAdviceTip] = Field(
        default_factory=list,
        description="Applicable tax tips for user notification",
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="e-Boekhouden REST API payload ready for create_mutation or create_invoice",
    )
    validation_warnings: list[str] = Field(
        default_factory=list,
        description="Non-blocking validation warnings for operator review",
    )
    validation_errors: list[str] = Field(
        default_factory=list,
        description="Blocking validation errors",
    )

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class BankTransactionSuggestion(BaseModel):
    """AI suggestion for classifying a bank transaction line."""

    grootboekcode: str = Field(description="Suggested ledger account code")
    ledger_id: int | None = Field(default=None, description="Matched ledger account ID if resolved")
    btw_code: str = Field(description="Suggested VAT code (e.g. HOOG_INK_21, GEEN)")
    soort: str = Field(
        description="Transaction type name: GeldUitgegeven, GeldOntvangen, FactuurbetalingVerstuurd, FactuurbetalingOntvangen"
    )
    mutation_type: str = Field(
        description="e-Boekhouden mutation type code: '3', '4', '5', '6'"
    )
    omschrijving: str = Field(description="Suggested clean description")
    confidence: float = Field(default=0.85, ge=0.0, le=1.0, description="Suggestion confidence")
    reasoning: str | None = Field(default=None, description="Explanation for suggestion")

    model_config = ConfigDict(populate_by_name=True, extra="ignore")
