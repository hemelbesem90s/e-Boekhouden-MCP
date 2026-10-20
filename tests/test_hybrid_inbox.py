"""Unit tests for AI Bookkeeping Inbox, Dutch OCR classification, and proposal generation."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from eboekhouden_mcp.inbox.models import (
    BookingProposal,
    ExtractedInvoiceData,
)
from eboekhouden_mcp.inbox.prompts import (
    DUTCH_INVOICE_OCR_SYSTEM_PROMPT,
    build_invoice_ocr_prompt,
    build_transaction_classify_prompt,
)
from eboekhouden_mcp.inbox.proposal import (
    find_ledger_account,
    generate_booking_proposal,
    match_relation,
    suggest_bank_transaction_classification,
)
from eboekhouden_mcp.tools.inbox import (
    GetDutchBookkeepingRulesTool,
    PrepareBookingProposalTool,
    SuggestTransactionClassificationTool,
)


# --------------------------------------------------------------------------- #
# Prompt tests
# --------------------------------------------------------------------------- #


def test_dutch_invoice_ocr_prompt_contains_vat_rules():
    """Verify all Dutch VAT codes and authoritative rules are in the prompt."""
    prompt = DUTCH_INVOICE_OCR_SYSTEM_PROMPT

    # VAT codes
    assert "HOOG_INK_21" in prompt
    assert "LAAG_INK_9" in prompt
    assert "GEEN" in prompt
    assert "VERL_INK" in prompt
    assert "VERL_INK_L9" in prompt
    assert "BU_EU_INK" in prompt
    assert "BI_EU_INK" in prompt

    # Directive rules & EV charging
    assert "Article 226" in prompt
    assert "BTW verlegd" in prompt
    assert "Reverse charge" in prompt
    assert "Art 38" in prompt or "art. 38" in prompt or "art 38" in prompt.lower()
    assert "assurantiebelasting" in prompt

    # Receipt classification
    assert "isReceipt" in prompt
    assert "receiptReason" in prompt
    assert "Geld uitgegeven" in prompt

    # Tax advice tips
    assert "kia" in prompt
    assert "vaste_activa" in prompt
    assert "gemengd_gebruik" in prompt
    assert "representatie" in prompt
    assert "geen_btw" in prompt


def test_build_invoice_ocr_prompt_dynamic_accounts():
    """Custom ledger accounts should be injected into the OCR prompt."""
    default_prompt = build_invoice_ocr_prompt()
    assert "4100 = Kantoorkosten" in default_prompt

    custom_accounts = [
        {"code": "4110", "description": "Software licenties"},
        {"code": "4310", "description": "Brandstof zakelijk"},
    ]
    custom_prompt = build_invoice_ocr_prompt(custom_accounts)
    assert "4110 = Software licenties" in custom_prompt
    assert "4310 = Brandstof zakelijk" in custom_prompt


def test_build_transaction_classify_prompt():
    """Bank statement classification prompt should support injection."""
    base_prompt = build_transaction_classify_prompt()
    assert "GeldUitgegeven" in base_prompt
    assert "FactuurbetalingVerstuurd" in base_prompt

    accounts = [{"code": "4510", "description": "Bankkosten bunq"}]
    injected = build_transaction_classify_prompt(accounts)
    assert "4510 Bankkosten bunq" in injected


# --------------------------------------------------------------------------- #
# Models & Extraction tests
# --------------------------------------------------------------------------- #


def test_extracted_invoice_data_model_deserialization():
    """Test camelCase and snake_case parsing with tax tips."""
    raw = {
        "leverancier": "SuperCloud B.V.",
        "factuurnummer": "INV-2026-001",
        "datum": "2026-03-15",
        "bedragExclBtw": 100.0,
        "bedragInclBtw": 121.0,
        "btwBedrag": 21.0,
        "btwPercentage": 21,
        "currency": "EUR",
        "omschrijving": "Cloud hosting services",
        "grootboekcode": "4100",
        "btwCode": "HOOG_INK_21",
        "isReverseCharge": False,
        "isReceipt": False,
        "confidence": 0.98,
        "redenering": "Software & hosting → Kantoorkosten",
        "belastingAdvies": [
            {
                "type": "kia",
                "tekst": "Investeringsaftrek controleren bij aanschaf boven €450.",
            }
        ],
    }

    model = ExtractedInvoiceData.model_validate(raw)
    assert model.leverancier == "SuperCloud B.V."
    assert model.factuurnummer == "INV-2026-001"
    assert model.bedrag_excl_btw == 100.0
    assert model.bedrag_incl_btw == 121.0
    assert model.btw_bedrag == 21.0
    assert model.is_receipt is False
    assert len(model.belasting_advies) == 1
    assert model.belasting_advies[0].type == "kia"


# --------------------------------------------------------------------------- #
# Proposal & Matching tests
# --------------------------------------------------------------------------- #


SAMPLE_RELATIONS = [
    {"id": 10, "code": "REL-001", "name": "Bol.com B.V.", "company": "Bol.com B.V."},
    {
        "id": 20,
        "code": "REL-002",
        "name": "Apple Distribution International",
        "company": "Apple",
    },
    {"id": 30, "code": "REL-003", "name": "TransIP B.V.", "company": "TransIP"},
    {
        "id": 40,
        "code": "REL-004",
        "name": "Shell Nederland Verkoopmaatschappij",
        "company": "Shell",
    },
]

SAMPLE_LEDGERS = [
    {"id": 101, "code": "1000", "description": "Kas / Bank", "category": "FIN"},
    {"id": 102, "code": "1600", "description": "Crediteuren", "category": "CRED"},
    {"id": 103, "code": "1300", "description": "Debiteuren", "category": "DEB"},
    {
        "id": 201,
        "code": "4100",
        "description": "Kantoorkosten / Software",
        "category": "VW",
    },
    {"id": 202, "code": "4200", "description": "Huisvestingskosten", "category": "VW"},
    {"id": 203, "code": "4300", "description": "Vervoerskosten", "category": "VW"},
    {"id": 204, "code": "4400", "description": "Verkoopkosten", "category": "VW"},
    {"id": 205, "code": "4500", "description": "Algemene kosten", "category": "VW"},
]


def test_match_relation_exact_and_fuzzy():
    """Verify exact, suffix-stripped, and fuzzy matching."""
    # Exact match
    matched, conf = match_relation("TransIP B.V.", SAMPLE_RELATIONS)
    assert matched is not None
    assert matched["id"] == 30
    assert conf == 1.0

    # Normalized match (without B.V.)
    matched, conf = match_relation("Bol.com", SAMPLE_RELATIONS)
    assert matched is not None
    assert matched["id"] == 10
    assert conf >= 0.90

    # Substring match
    matched, conf = match_relation("Apple Ireland", SAMPLE_RELATIONS)
    assert matched is not None
    assert matched["id"] == 20

    # No match
    unmatched, conf = match_relation("Completely Unknown Vendor XYZ", SAMPLE_RELATIONS)
    assert unmatched is None
    assert conf == 0.0


def test_find_ledger_account_and_fallback():
    """Verify code match and fallback to general expense account."""
    ledger, exact = find_ledger_account("4100", SAMPLE_LEDGERS)
    assert ledger is not None
    assert ledger["code"] == "4100"
    assert exact is True

    # Unknown code falls back to 4500 or VW
    fallback, exact = find_ledger_account("9999", SAMPLE_LEDGERS)
    assert fallback is not None
    assert fallback["code"] in ("4500", "4100")
    assert exact is False


def test_generate_booking_proposal_formal_invoice():
    """A formal invoice should produce a mutation type 1 with matched creditor relation."""
    invoice_data = ExtractedInvoiceData(
        leverancier="TransIP",
        factuurnummer="F2026-987",
        datum="2026-04-01",
        bedragExclBtw=50.0,
        bedragInclBtw=60.5,
        btwBedrag=10.5,
        btwPercentage=21,
        omschrijving="Web hosting renewal",
        grootboekcode="4100",
        btwCode="HOOG_INK_21",
        isReceipt=False,
    )

    proposal = generate_booking_proposal(
        invoice_data=invoice_data,
        relations=SAMPLE_RELATIONS,
        ledgers=SAMPLE_LEDGERS,
    )

    assert isinstance(proposal, BookingProposal)
    assert proposal.mutation_type == "1"
    assert proposal.relation_id == 30
    assert proposal.relation_matched is True
    assert proposal.ledger_id == 102  # Crediteuren 1600
    assert len(proposal.lines) == 1
    assert proposal.lines[0].ledger_id == 201  # 4100
    assert proposal.lines[0].amount == 50.0
    assert proposal.lines[0].vat_code == "HOOG_INK_21"
    assert proposal.is_receipt is False

    # Check payload structure
    payload = proposal.payload
    assert payload["type"] == "1"
    assert payload["relation_id"] == 30
    assert payload["invoice_number"] == "F2026-987"
    assert len(payload["rows"]) == 1
    assert payload["rows"][0]["amount"] == 50.0
    assert payload["rows"][0]["vat_code"] == "HOOG_INK_21"


def test_generate_booking_proposal_receipt():
    """A receipt (bonnetje) should produce mutation type 6 without requiring creditor relation."""
    receipt_data = ExtractedInvoiceData(
        leverancier="Shell Express",
        factuurnummer="BON-5544",
        datum="2026-04-02",
        bedragExclBtw=41.32,
        bedragInclBtw=50.0,
        btwBedrag=8.68,
        btwPercentage=21,
        omschrijving="Brandstof Shell",
        grootboekcode="4300",
        btwCode="HOOG_INK_21",
        isReceipt=True,
        receiptReason="Tankstation kassabon",
    )

    proposal = generate_booking_proposal(
        invoice_data=receipt_data,
        relations=SAMPLE_RELATIONS,
        ledgers=SAMPLE_LEDGERS,
    )

    assert proposal.mutation_type == "6"  # Geld uitgegeven
    assert proposal.is_receipt is True
    assert proposal.receipt_reason == "Tankstation kassabon"
    assert proposal.ledger_id == 101  # Kas/Bank 1000

    payload = proposal.payload
    assert payload["type"] == "6"
    assert payload.get("relation_id") is None
    assert payload["rows"][0]["ledger_id"] == 203  # 4300


def test_generate_booking_proposal_geen_vat_rule():
    """For GEEN VAT (e.g. insurance/bank fee), full amount is booked and VAT is 0."""
    insurance_data = ExtractedInvoiceData(
        leverancier="Nationale Nederlanden",
        datum="2026-04-03",
        bedragExclBtw=100.0,
        bedragInclBtw=121.0,  # e.g. incl assurantiebelasting
        btwBedrag=21.0,  # mistakenly extracted as VAT
        grootboekcode="4500",
        btwCode="GEEN",
        isReceipt=False,
    )

    proposal = generate_booking_proposal(
        invoice_data=insurance_data,
        relations=[],
        ledgers=SAMPLE_LEDGERS,
    )

    # Line amount should equal bedragInclBtw and vat_amount should be 0
    assert proposal.lines[0].amount == 121.0
    assert proposal.lines[0].vat_amount is None or proposal.lines[0].vat_amount == 0.0
    assert any("Bij BTW-code GEEN" in w for w in proposal.validation_warnings)


def test_generate_booking_proposal_invoice_target():
    """Proposal targeting create_invoice format."""
    inv_data = ExtractedInvoiceData(
        leverancier="Bol.com",
        factuurnummer="BOL-123",
        datum="2026-04-05",
        bedragExclBtw=80.0,
        bedragInclBtw=96.8,
        btwBedrag=16.8,
        grootboekcode="4100",
        btwCode="HOOG_INK_21",
    )

    proposal = generate_booking_proposal(
        invoice_data=inv_data,
        relations=SAMPLE_RELATIONS,
        ledgers=SAMPLE_LEDGERS,
        proposal_type="invoice",
    )

    assert proposal.proposal_type == "invoice"
    assert "items" in proposal.payload
    assert proposal.payload["items"][0]["price_per_unit"] == 80.0


# --------------------------------------------------------------------------- #
# Transaction classification tests
# --------------------------------------------------------------------------- #


def test_suggest_bank_transaction_classification():
    """Verify bank description rule engine suggestions."""
    # Bank costs
    s1 = suggest_bank_transaction_classification(
        description="Kosten betalingsverkeer maart",
        amount=-14.50,
        ledgers=SAMPLE_LEDGERS,
    )
    assert s1.grootboekcode == "4500"
    assert s1.btw_code == "GEEN"
    assert s1.soort == "GeldUitgegeven"
    assert s1.ledger_id == 205

    # Fuel
    s2 = suggest_bank_transaction_classification(
        description="Betaalautomaat 14:22 Fastned B.V. Amsterdam",
        amount=-35.00,
        ledgers=SAMPLE_LEDGERS,
    )
    assert s2.grootboekcode == "4300"
    assert s2.btw_code == "HOOG_INK_21"
    assert s2.ledger_id == 203

    # SaaS Software
    s3 = suggest_bank_transaction_classification(
        description="GitHub Enterprise Monthly",
        amount=-42.00,
        ledgers=SAMPLE_LEDGERS,
    )
    assert s3.grootboekcode == "4100"
    assert s3.btw_code == "HOOG_INK_21"
    assert s3.ledger_id == 201

    # Revenue received
    s4 = suggest_bank_transaction_classification(
        description="Betaling van opdrachtgever Project Alpha",
        amount=1500.00,
        ledgers=SAMPLE_LEDGERS,
    )
    assert s4.soort in ("GeldOntvangen", "FactuurbetalingOntvangen")
    assert s4.mutation_type in ("3", "5")


# --------------------------------------------------------------------------- #
# Tool execution tests
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_get_dutch_bookkeeping_rules_tool():
    """Test get_dutch_bookkeeping_rules tool output."""
    tool = GetDutchBookkeepingRulesTool()
    client = MagicMock()

    res = await tool.execute(client, {"category": "all", "include_prompts": True})
    assert "vat_codes" in res
    assert "vat_decision_tree" in res
    assert "receipt_detection" in res
    assert "tax_advice_categories" in res
    assert "prompts" in res
    assert "HOOG_INK_21" in res["vat_codes"]


@pytest.mark.asyncio
async def test_prepare_booking_proposal_tool_with_supplied_data():
    """Test prepare_booking_proposal tool with direct invoice_data."""
    tool = PrepareBookingProposalTool()
    client = MagicMock()

    raw_invoice = {
        "leverancier": "TransIP B.V.",
        "factuurnummer": "INV-7788",
        "datum": "2026-04-10",
        "bedragExclBtw": 120.0,
        "bedragInclBtw": 145.2,
        "btwBedrag": 25.2,
        "grootboekcode": "4100",
        "btwCode": "HOOG_INK_21",
        "isReceipt": False,
    }

    result = await tool.execute(
        client,
        {
            "invoice_data": raw_invoice,
            "relations": SAMPLE_RELATIONS,
            "ledgers": SAMPLE_LEDGERS,
        },
    )

    assert result["mutation_type"] == "1"
    assert result["relation_id"] == 30
    assert result["payload"]["rows"][0]["amount"] == 120.0


@pytest.mark.asyncio
async def test_suggest_transaction_classification_tool():
    """Test suggest_transaction_classification tool execution."""
    tool = SuggestTransactionClassificationTool()
    client = MagicMock()

    result = await tool.execute(
        client,
        {
            "description": "Google Cloud Platform EMEA",
            "amount": -85.50,
            "ledgers": SAMPLE_LEDGERS,
        },
    )

    assert result["grootboekcode"] == "4100"
    assert result["btw_code"] == "HOOG_INK_21"
    assert result["soort"] == "GeldUitgegeven"
