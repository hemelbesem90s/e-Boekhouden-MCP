"""MCP tools for AI bookkeeping inbox, Dutch OCR rules, and booking proposals."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field

from eboekhouden.filters import StringFilter
from eboekhouden_mcp.inbox.models import ExtractedInvoiceData
from eboekhouden_mcp.inbox.prompts import (
    DUTCH_INVOICE_OCR_SYSTEM_PROMPT,
    DUTCH_TRANSACTION_CLASSIFICATION_PROMPT,
    build_invoice_ocr_prompt,
    build_transaction_classify_prompt,
)
from eboekhouden_mcp.inbox.proposal import (
    generate_booking_proposal,
    suggest_bank_transaction_classification,
)
from eboekhouden_mcp.tools.base import BaseTool, ToolSchema

if TYPE_CHECKING:
    from eboekhouden import EBoekhoudenClient


class GetDutchBookkeepingRulesInput(ToolSchema):
    """Input schema for get_dutch_bookkeeping_rules."""

    category: Literal["all", "vat", "receipts", "tax_tips", "prompts"] = Field(
        default="all",
        description="Filter rules category: 'all', 'vat', 'receipts', 'tax_tips', or 'prompts'",
    )
    include_prompts: bool = Field(
        default=True,
        description="Whether to include complete LLM system prompt templates in the response",
    )


class GetDutchBookkeepingRulesTool(BaseTool):
    """Retrieve authoritative Dutch bookkeeping logic, VAT decision trees, and OCR prompt templates."""

    name = "get_dutch_bookkeeping_rules"
    description = (
        "Return authoritative Dutch accounting rules: VAT decision tree (HOOG_INK_21, LAAG_INK_9, GEEN, "
        "VERL_INK, BU_EU_INK, BI_EU_INK), EU Directive Art 226 reverse-charge validation, Art 38 EV charging, "
        "bonnetje vs invoice detection, Dutch tax tips (KIA, Vaste Activa, 80% representatie), and LLM prompts."
    )
    input_schema = GetDutchBookkeepingRulesInput

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        category = arguments.get("category", "all")
        include_prompts = bool(arguments.get("include_prompts", True))

        rules: dict[str, Any] = {}

        if category in ("all", "vat"):
            rules["vat_codes"] = {
                "HOOG_INK_21": "Standard Dutch 21% purchase VAT for domestic supplies.",
                "LAAG_INK_9": "Reduced Dutch 9% purchase VAT for food, books, pharmaceuticals, public transport.",
                "GEEN": "0% VAT or exempt: insurance (assurantiebelasting), bank fees, government duties, foreign VAT not deductible.",
                "VERL_INK": "Reverse-charge 21% for intra-EU services or cross-border B2B supplies with explicit reverse-charge note.",
                "VERL_INK_L9": "Reverse-charge 9% for intra-EU reduced rate supplies.",
                "BU_EU_INK": "Purchases from outside EU with explicit reverse charge / customs document.",
                "BI_EU_INK": "Intra-community acquisition of physical goods shipped from EU member states.",
            }
            rules["vat_decision_tree"] = [
                "1. Domestic Dutch supplier with NL VAT number charging 21% or 9% -> HOOG_INK_21 or LAAG_INK_9.",
                "1a. EU EV charging (Tesla, Fastned, etc.) with valid NL VAT number on recipient -> book as VERL_INK under Art 38 (net 0 BTW, foreign VAT absorbed in cost).",
                "1b. Foreign supplier charging local VAT without reverse-charge text -> GEEN (not deductible via NL BTW aangifte; full amount is expense).",
                "2. Explicit reverse charge wording (EU Art 226) -> VERL_INK (services/EU) or BI_EU_INK (goods/EU) or BU_EU_INK (outside EU).",
                "3. 0% VAT without reverse charge clause -> GEEN with review warning.",
                "4. Insurance with assurantiebelasting -> GEEN (assurantiebelasting is non-deductible expense, book total amount).",
            ]

        if category in ("all", "receipts"):
            rules["receipt_detection"] = {
                "is_receipt_criteria": [
                    "Supplier is restaurant, cafe, bar, supermarket, gas station, parking, or kiosk.",
                    "Document is a thermal till slip without formal addressee or payment terms.",
                    "Total amount < 100 EUR and lacks formal company invoice metadata.",
                ],
                "accounting_treatment": "Booked directly as Geld uitgegeven (mutation type 6) without requiring a creditor relation.",
            }

        if category in ("all", "tax_tips"):
            rules["tax_advice_categories"] = {
                "kia": "Investeringsaftrek (KIA): durable business assets >= €450 should be evaluated for KIA.",
                "vaste_activa": "Durable assets > €450 must be activated on the balance sheet and depreciated.",
                "gemengd_gebruik": "Assets with mixed business/personal use (phones, cars, computers) must have personal percentage adjusted.",
                "representatie": "Food, drinks, and entertainment for business relations: 80% tax-deductible (20% non-deductible).",
                "geen_btw": "Advice when VAT is non-deductible or foreign VAT procedure applies.",
            }

        if include_prompts and category in ("all", "prompts"):
            rules["prompts"] = {
                "invoice_ocr_system_prompt": DUTCH_INVOICE_OCR_SYSTEM_PROMPT,
                "transaction_classify_prompt": DUTCH_TRANSACTION_CLASSIFICATION_PROMPT,
            }

        return rules


class PrepareBookingProposalInput(ToolSchema):
    """Input schema for prepare_booking_proposal."""

    invoice_data: dict[str, Any] | str | None = Field(
        default=None,
        description="Structured extracted invoice data object (or JSON string).",
    )
    invoice_text: str | None = Field(
        default=None,
        description="Alternative raw invoice text or JSON string to parse if invoice_data is omitted.",
    )
    proposal_type: Literal["mutation", "invoice"] = Field(
        default="mutation",
        description="Proposal target: 'mutation' (create_mutation) or 'invoice' (create_invoice).",
    )
    default_bank_ledger_id: int | None = Field(
        default=None,
        description="Override bank/cash ledger account ID for receipts.",
    )
    default_creditor_ledger_id: int | None = Field(
        default=None,
        description="Override creditor ledger account ID for formal invoices.",
    )
    relations: list[dict[str, Any]] | None = Field(
        default=None,
        description="Optional list of relations to match against. If omitted, queries live relations from e-Boekhouden.",
    )
    ledgers: list[dict[str, Any]] | None = Field(
        default=None,
        description="Optional list of ledgers to validate against. If omitted, queries live ledgers from e-Boekhouden.",
    )
    cost_centers: list[dict[str, Any]] | None = Field(
        default=None,
        description="Optional list of cost centers. If omitted, queries live cost centers from e-Boekhouden.",
    )


class PrepareBookingProposalTool(BaseTool):
    """Cross-reference extracted invoice/receipt data with e-Boekhouden and produce a validated booking proposal."""

    name = "prepare_booking_proposal"
    description = (
        "Ingests structured invoice/receipt metadata, cross-references live relations, ledger accounts "
        "(grootboekrekeningen), and VAT codes from e-Boekhouden, and produces a validated booking proposal payload "
        "ready for create_mutation or create_invoice. Flags receipts (bonnetjes) vs formal invoices."
    )
    input_schema = PrepareBookingProposalInput

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        raw_data = arguments.get("invoice_data")
        raw_text = arguments.get("invoice_text")

        if isinstance(raw_data, str):
            try:
                raw_data = json.loads(raw_data)
            except json.JSONDecodeError:
                pass

        if raw_data is None and raw_text:
            try:
                raw_data = json.loads(raw_text)
            except json.JSONDecodeError:
                raw_data = {"leverancier": "Onbekend", "datum": "2026-01-01", "bedragExclBtw": 0.0, "bedragInclBtw": 0.0}

        if not isinstance(raw_data, dict):
            raise ValueError(
                "Either invoice_data (dict/JSON) or invoice_text (JSON) must be provided."
            )

        invoice_model = ExtractedInvoiceData.model_validate(raw_data)

        # 1. Fetch ledgers if not supplied
        ledgers = arguments.get("ledgers")
        if ledgers is None:
            ledger_items = client.ledgers.list(limit=2000).items
            ledgers = [item.model_dump() for item in ledger_items]

        # 2. Fetch relations if not supplied
        relations = arguments.get("relations")
        if relations is None:
            supplier = invoice_model.leverancier.strip()
            candidates = []
            if supplier:
                try:
                    candidates = client.relations.list(
                        limit=50, name=StringFilter.contains(supplier)
                    ).items
                except Exception:
                    candidates = []
            if not candidates:
                try:
                    candidates = client.relations.list(limit=200).items
                except Exception:
                    candidates = []

            # Hydrate top candidates for full names
            relations = []
            for c in candidates[:30]:
                try:
                    rel = client.relations.get(c.id)
                    relations.append(rel.model_dump())
                except Exception:
                    relations.append(c.model_dump())

        # 3. Fetch cost centers if not supplied
        cost_centers = arguments.get("cost_centers")
        if cost_centers is None:
            try:
                cost_centers = [cc.model_dump() for cc in client.cost_centers.list().items]
            except Exception:
                cost_centers = []

        proposal = generate_booking_proposal(
            invoice_data=invoice_model,
            relations=relations,
            ledgers=ledgers,
            cost_centers=cost_centers,
            default_bank_ledger_id=arguments.get("default_bank_ledger_id"),
            default_creditor_ledger_id=arguments.get("default_creditor_ledger_id"),
            proposal_type=arguments.get("proposal_type", "mutation"),
        )
        return proposal.model_dump()


class SuggestTransactionClassificationInput(ToolSchema):
    """Input schema for suggest_transaction_classification."""

    description: str = Field(description="Bank transaction description text")
    amount: float = Field(
        description="Transaction amount (negative for money spent, positive for money received)"
    )
    counter_account: str | None = Field(
        default=None,
        description="Counter account / IBAN if available from the bank statement",
    )
    date: str | None = Field(
        default=None,
        description="Transaction date (YYYY-MM-DD)",
    )
    ledgers: list[dict[str, Any]] | None = Field(
        default=None,
        description="Optional list of ledgers. If omitted, queries live ledgers from e-Boekhouden.",
    )


class SuggestTransactionClassificationTool(BaseTool):
    """Suggest ledger account, VAT code, and mutation type for a bank statement line."""

    name = "suggest_transaction_classification"
    description = (
        "Analyze a bank statement line description and amount, cross-reference against "
        "e-Boekhouden ledger accounts, and suggest the appropriate grootboekrekening, VAT code, "
        "and mutation type (Geld uitgegeven, Factuurbetaling, etc.)."
    )
    input_schema = SuggestTransactionClassificationInput

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        ledgers = arguments.get("ledgers")
        if ledgers is None:
            try:
                ledger_items = client.ledgers.list(limit=2000).items
                ledgers = [item.model_dump() for item in ledger_items]
            except Exception:
                ledgers = []

        suggestion = suggest_bank_transaction_classification(
            description=arguments["description"],
            amount=float(arguments["amount"]),
            counter_account=arguments.get("counter_account"),
            date=arguments.get("date"),
            ledgers=ledgers,
        )
        return suggestion.model_dump()
