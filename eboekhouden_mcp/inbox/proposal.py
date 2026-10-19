"""Booking proposal generator and transaction classification logic."""

from __future__ import annotations

import difflib
import re
from typing import Any, Literal

from eboekhouden_mcp.inbox.models import (
    BankTransactionSuggestion,
    BookingLine,
    BookingProposal,
    ExtractedInvoiceData,
    TaxAdviceTip,
)

VALID_PURCHASE_VAT_CODES = {
    "HOOG_INK_21",
    "LAAG_INK_9",
    "GEEN",
    "VERL_INK",
    "VERL_INK_L9",
    "BU_EU_INK",
    "BI_EU_INK",
    "AFW",
}

LEGAL_SUFFIXES_REGEX = re.compile(
    r"\b(b\.?v\.?|n\.?v\.?|v\.?o\.?f\.?|gmbh|inc\.?|llc|ltd\.?|holding|beheer|nederland|nl)\b",
    re.IGNORECASE,
)


def _clean_name(name: str) -> str:
    """Normalize company name by stripping legal forms and punctuation for matching."""
    cleaned = LEGAL_SUFFIXES_REGEX.sub("", name)
    cleaned = re.sub(r"[^\w\s]", " ", cleaned)
    return " ".join(cleaned.lower().split())


def match_relation(
    supplier_name: str, relations: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, float]:
    """Match supplier name against existing relations.

    Returns:
        Tuple of (matched_relation_dict or None, confidence_score 0.0-1.0)
    """
    if not supplier_name or not relations:
        return None, 0.0

    raw_supplier = supplier_name.strip().lower()
    clean_supplier = _clean_name(supplier_name)
    supplier_tokens = [t for t in clean_supplier.split() if len(t) >= 3]

    # 1. Exact match on raw name or code or company
    for r in relations:
        names = [
            str(r.get("name") or "").strip().lower(),
            str(r.get("company") or "").strip().lower(),
        ]
        r_code = str(r.get("code") or "").strip().lower()
        if raw_supplier in names or (r_code and raw_supplier == r_code):
            return r, 1.0

    # 2. Match on cleaned name without legal forms
    for r in relations:
        for val in (r.get("name"), r.get("company")):
            if not val:
                continue
            clean_r = _clean_name(str(val))
            if clean_supplier and clean_supplier == clean_r:
                return r, 0.95

    # 3. Substring & Token matching
    best_candidate: dict[str, Any] | None = None
    best_ratio: float = 0.0

    for r in relations:
        candidates_to_check = []
        if r.get("name"):
            candidates_to_check.append(str(r["name"]))
        if r.get("company"):
            candidates_to_check.append(str(r["company"]))

        for cand in candidates_to_check:
            clean_cand = _clean_name(cand)
            if not clean_cand:
                continue

            if len(clean_supplier) >= 3 and len(clean_cand) >= 3:
                if clean_supplier in clean_cand or clean_cand in clean_supplier:
                    ratio = 0.88
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_candidate = r

            # Check significant token overlap (e.g. "Apple" in "Apple Ireland" and "Apple Distribution")
            cand_tokens = [t for t in clean_cand.split() if len(t) >= 3]
            if supplier_tokens and cand_tokens:
                if supplier_tokens[0] == cand_tokens[0]:
                    ratio = 0.85
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_candidate = r

            # Fuzzy sequence matcher
            ratio = difflib.SequenceMatcher(None, clean_supplier, clean_cand).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_candidate = r

    if best_candidate and best_ratio >= 0.75:
        return best_candidate, round(best_ratio, 2)

    return None, 0.0


def find_ledger_account(
    grootboekcode: str | None, ledgers: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, bool]:
    """Locate a ledger account by code or fallback to standard expense account.

    Returns:
        Tuple of (ledger_dict or None, is_exact_match_bool)
    """
    if not ledgers:
        return None, False

    target_code = str(grootboekcode or "").strip()
    if target_code:
        for ledger in ledgers:
            code = str(ledger.get("code") or "").strip()
            if code == target_code or code.zfill(4) == target_code.zfill(4):
                return ledger, True

        # Try matching description
        target_lower = target_code.lower()
        for ledger in ledgers:
            desc = str(ledger.get("description") or "").lower()
            if target_lower in desc:
                return ledger, False

    # Fallback to general cost account (4500, or category VW / Profit & Loss)
    for ledger in ledgers:
        code = str(ledger.get("code") or "").strip()
        if code in ("4500", "4100"):
            return ledger, False

    for ledger in ledgers:
        cat = str(ledger.get("category") or "").upper()
        if cat in ("VW", "PROFIT_LOSS") or str(ledger.get("code") or "").startswith("4"):
            return ledger, False

    return ledgers[0], False


def find_header_account(
    ledgers: list[dict[str, Any]],
    is_receipt: bool,
    default_id: int | None = None,
) -> tuple[int, str]:
    """Find the header ledger account (Bank/Cash for receipts, Creditors for invoices)."""
    if default_id:
        for ledger in ledgers:
            if ledger.get("id") == default_id:
                return default_id, str(ledger.get("code") or "")
        return default_id, ""

    if is_receipt:
        # Liquid assets / Bank / Kas
        for ledger in ledgers:
            cat = str(ledger.get("category") or "").upper()
            code = str(ledger.get("code") or "").strip()
            if cat in ("FIN", "LIQUID_ASSETS") or code in ("1000", "1010", "1020"):
                return int(ledger["id"]), code
        return 1000, "1000"
    else:
        # Creditors
        for ledger in ledgers:
            cat = str(ledger.get("category") or "").upper()
            code = str(ledger.get("code") or "").strip()
            if cat in ("CRED", "CREDITORS") or code == "1600":
                return int(ledger["id"]), code
        return 1600, "1600"


def generate_booking_proposal(
    invoice_data: ExtractedInvoiceData,
    relations: list[dict[str, Any]],
    ledgers: list[dict[str, Any]],
    cost_centers: list[dict[str, Any]] | None = None,
    default_bank_ledger_id: int | None = None,
    default_creditor_ledger_id: int | None = None,
    proposal_type: Literal["mutation", "invoice"] = "mutation",
) -> BookingProposal:
    """Cross-references extracted invoice data with live accounting records and generates a validated proposal.

    Args:
        invoice_data: Structured extraction output from OCR.
        relations: Available relations from e-Boekhouden.
        ledgers: Available ledger accounts from e-Boekhouden.
        cost_centers: Optional available cost centers.
        default_bank_ledger_id: Optional default bank ledger ID for receipts.
        default_creditor_ledger_id: Optional default creditor ledger ID for formal invoices.
        proposal_type: 'mutation' (standard) or 'invoice'.

    Returns:
        Validated BookingProposal ready for create_mutation or create_invoice.
    """
    validation_warnings: list[str] = []
    validation_errors: list[str] = []

    is_receipt = invoice_data.is_receipt
    receipt_reason = invoice_data.receipt_reason or ""
    if is_receipt and not receipt_reason:
        receipt_reason = "Bonnetje / kasuitgave"

    # 1. Relation matching
    matched_relation, relation_confidence = match_relation(
        invoice_data.leverancier, relations
    )
    relation_id = int(matched_relation["id"]) if matched_relation else None
    relation_name = (
        str(matched_relation.get("name") or matched_relation.get("company") or "")
        if matched_relation
        else None
    )
    relation_matched = matched_relation is not None

    if not is_receipt and not relation_matched:
        validation_warnings.append(
            f"Leverancier '{invoice_data.leverancier}' niet gevonden in bestaande relaties. "
            "Maak eerst een crediteurrelatie aan of controleer de schrijfwijze."
        )

    # 2. Cost ledger matching
    matched_ledger, is_exact_code = find_ledger_account(
        invoice_data.grootboekcode, ledgers
    )
    if not matched_ledger:
        validation_errors.append("Geen geschikte grootboekrekening gevonden in de administratie.")
        cost_ledger_id = 4500
        cost_ledger_code = "4500"
    else:
        cost_ledger_id = int(matched_ledger["id"])
        cost_ledger_code = str(matched_ledger.get("code") or "")
        if not is_exact_code and invoice_data.grootboekcode:
            validation_warnings.append(
                f"Grootboekcode '{invoice_data.grootboekcode}' niet direct gevonden; "
                f"teruggevallen op {cost_ledger_code} ({matched_ledger.get('description', '')})."
            )

    # 3. Header ledger account
    header_ledger_id, header_ledger_code = find_header_account(
        ledgers=ledgers,
        is_receipt=is_receipt,
        default_id=default_bank_ledger_id if is_receipt else default_creditor_ledger_id,
    )

    # 4. VAT code validation and amount checking
    btw_code = invoice_data.btw_code or "HOOG_INK_21"
    if btw_code not in VALID_PURCHASE_VAT_CODES:
        validation_warnings.append(
            f"Onbekende of niet-standaard BTW-code '{btw_code}'. Controleer de BTW-aangifte specificatie."
        )

    excl_amount = round(float(invoice_data.bedrag_excl_btw), 2)
    incl_amount = round(float(invoice_data.bedrag_incl_btw), 2)
    vat_amount = round(float(invoice_data.btw_bedrag), 2)

    # Dutch tax rule enforcement
    if btw_code == "GEEN":
        if vat_amount != 0.0:
            validation_warnings.append(
                "Bij BTW-code GEEN is het BTW-bedrag €0.00; het volledige factuurbedrag inclusief BTW is als kosten opgenomen."
            )
            vat_amount = 0.0
        line_amount = incl_amount
    else:
        line_amount = excl_amount

        # Check math consistency for standard rates
        if btw_code == "HOOG_INK_21" and excl_amount > 0:
            expected_vat = round(excl_amount * 0.21, 2)
            if abs(expected_vat - vat_amount) > 0.05:
                validation_warnings.append(
                    f"Opgegeven BTW (€{vat_amount:.2f}) wijkt af van berekende 21% BTW (€{expected_vat:.2f})."
                )
        elif btw_code == "LAAG_INK_9" and excl_amount > 0:
            expected_vat = round(excl_amount * 0.09, 2)
            if abs(expected_vat - vat_amount) > 0.05:
                validation_warnings.append(
                    f"Opgegeven BTW (€{vat_amount:.2f}) wijkt af van berekende 9% BTW (€{expected_vat:.2f})."
                )

    # 5. Cost center matching (optional)
    cost_center_id: int | None = None
    if cost_centers:
        for cc in cost_centers:
            desc = str(cc.get("description") or "").lower()
            if desc and desc in invoice_data.omschrijving.lower():
                cost_center_id = int(cc["id"])
                break

    # 6. Description & Reference
    clean_desc = invoice_data.omschrijving.strip()
    if not clean_desc:
        clean_desc = f"{'Bon' if is_receipt else 'Factuur'} {invoice_data.leverancier}"
    clean_desc = clean_desc[:50]  # e-Boekhouden header description max 50 chars

    # 7. Construct BookingLine
    line = BookingLine(
        ledger_id=cost_ledger_id,
        ledger_code=cost_ledger_code,
        vat_code=btw_code,
        amount=line_amount,
        vat_amount=vat_amount if vat_amount > 0 else None,
        cost_center_id=cost_center_id,
        description=invoice_data.omschrijving[:200] if invoice_data.omschrijving else clean_desc,
    )

    # 8. Construct API Payload
    # Receipt -> Type 6 (Geld uitgegeven)
    # Formal purchase invoice -> Type 1 (Inkoopfactuur)
    mutation_type = "6" if is_receipt else "1"

    mutation_row_payload: dict[str, Any] = {
        "ledger_id": line.ledger_id,
        "vat_code": line.vat_code,
        "amount": line.amount,
    }
    if line.vat_amount is not None:
        mutation_row_payload["vat_amount"] = line.vat_amount
    if line.cost_center_id is not None:
        mutation_row_payload["cost_center_id"] = line.cost_center_id
    if line.description:
        mutation_row_payload["description"] = line.description

    payload: dict[str, Any] = {
        "type": mutation_type,
        "date": invoice_data.datum,
        "ledger_id": header_ledger_id,
        "description": clean_desc,
        "in_ex_vat": "EX",
        "rows": [mutation_row_payload],
    }

    if not is_receipt:
        if relation_id is not None:
            payload["relation_id"] = relation_id
        if invoice_data.factuurnummer:
            payload["invoice_number"] = str(invoice_data.factuurnummer)[:50]
    else:
        # Direct mutation for receipt
        if invoice_data.factuurnummer:
            payload["invoice_number"] = str(invoice_data.factuurnummer)[:50]

    if proposal_type == "invoice":
        invoice_payload: dict[str, Any] = {
            "relation_id": relation_id or 1,
            "term_of_payment": 30,
            "template_id": 1,
            "date": invoice_data.datum,
            "in_ex_vat": "EX",
            "reference": (invoice_data.factuurnummer or "")[:50],
            "items": [
                {
                    "description": line.description or clean_desc,
                    "vat_code": line.vat_code,
                    "ledger_id": line.ledger_id,
                    "quantity": 1.0,
                    "price_per_unit": line.amount,
                    "cost_center_id": line.cost_center_id,
                }
            ],
        }
        if invoice_data.factuurnummer:
            invoice_payload["invoice_number"] = str(invoice_data.factuurnummer)[:50]
        payload = invoice_payload

    # Overall proposal confidence
    confidence = invoice_data.confidence
    if not is_receipt and not relation_matched:
        confidence = min(confidence, 0.70)
    if validation_errors:
        confidence = min(confidence, 0.30)

    return BookingProposal(
        proposal_type=proposal_type,
        mutation_type=mutation_type,
        date=invoice_data.datum,
        ledger_id=header_ledger_id,
        relation_id=relation_id,
        relation_name=relation_name,
        relation_matched=relation_matched,
        invoice_number=invoice_data.factuurnummer,
        description=clean_desc,
        in_ex_vat="EX",
        lines=[line],
        is_receipt=is_receipt,
        receipt_reason=receipt_reason,
        confidence=round(confidence, 2),
        tax_advice=list(invoice_data.belasting_advies),
        payload=payload,
        validation_warnings=validation_warnings,
        validation_errors=validation_errors,
    )


def suggest_bank_transaction_classification(
    description: str,
    amount: float,
    counter_account: str | None = None,
    date: str | None = None,
    ledgers: list[dict[str, Any]] | None = None,
) -> BankTransactionSuggestion:
    """Suggest ledger account, VAT code, and mutation type for a bank statement row."""
    desc_clean = description.strip()
    desc_lower = desc_clean.lower()
    ledgers = ledgers or []

    # Default values
    grootboekcode = "4500"  # Algemene kosten
    btw_code = "HOOG_INK_21"
    reasoning = "Algemene bedrijfskosten"
    confidence = 0.75

    is_negative = amount < 0

    if not is_negative:
        # Money received (income / debtor / refund)
        if any(w in desc_lower for w in ("factuur", "inv-", "betaling van", "nota")):
            soort = "FactuurbetalingOntvangen"
            mutation_type = "3"
            grootboekcode = "1300"
            btw_code = "GEEN"
            reasoning = "Klantbetaling ontvangen op openstaande debiteur"
            confidence = 0.85
        else:
            soort = "GeldOntvangen"
            mutation_type = "5"
            grootboekcode = "8000"
            btw_code = "HOOG_VERK_21"
            reasoning = "Omzet / directe ontvangst"
            confidence = 0.80
    else:
        # Money spent (negative amount)
        # Bank costs
        if any(w in desc_lower for w in ("kosten betalingsverkeer", "bankkosten", "abonnementskosten abn", "ing bank", "rabobank pas", "bunq")):
            grootboekcode = "4500"
            btw_code = "GEEN"
            soort = "GeldUitgegeven"
            mutation_type = "6"
            reasoning = "Bankkosten zijn vrijgesteld van BTW"
            confidence = 0.95
        # Tax authorities / Belastingdienst
        elif any(w in desc_lower for w in ("belastingdienst", "omzetbelasting", "loonheffing", "motorrijtuigenbelasting")):
            grootboekcode = "4500"
            btw_code = "GEEN"
            soort = "GeldUitgegeven"
            mutation_type = "6"
            reasoning = "Belastingbetalingen hebben geen aftrekbare BTW"
            confidence = 0.95
        # Vehicle / Travel / Fuel / Public Transit
        elif any(w in desc_lower for w in ("shell", "esso", "bp ", "total ", "fastned", "tankstation", "brandstof", "q-park", "parkeren", "ns-reizigers", "ns reizigers", "uber")):
            grootboekcode = "4300"
            btw_code = "LAAG_INK_9" if "ns" in desc_lower else "HOOG_INK_21"
            soort = "GeldUitgegeven"
            mutation_type = "6"
            reasoning = "Vervoers- en brandstofkosten"
            confidence = 0.90
        # Software / SaaS
        elif any(w in desc_lower for w in ("github", "google", "aws", "microsoft", "openai", "slack", "notion", "adobe", "hosting", "transip")):
            grootboekcode = "4100"
            btw_code = "HOOG_INK_21"
            soort = "GeldUitgegeven"
            mutation_type = "6"
            reasoning = "Software / SaaS kantoorkosten"
            confidence = 0.88
        # Housing / Utilities
        elif any(w in desc_lower for w in ("kpn", "vodafone", "ziggo", "eneco", "vattenfall", "essent", "waternet", "huur ")):
            grootboekcode = "4200"
            btw_code = "HOOG_INK_21"
            soort = "GeldUitgegeven"
            mutation_type = "6"
            reasoning = "Huisvestings-, telecom- of energiekosten"
            confidence = 0.85
        # Horeca / Catering
        elif any(w in desc_lower for w in ("restaurant", "cafe", "lunch", "koffie", "snackbar", "bakker")):
            grootboekcode = "4400"
            btw_code = "LAAG_INK_9"
            soort = "GeldUitgegeven"
            mutation_type = "6"
            reasoning = "Representatie- en horecakosten"
            confidence = 0.85
        # Salaries
        elif any(w in desc_lower for w in ("salaris", "loon ", "personeel")):
            grootboekcode = "4600"
            btw_code = "GEEN"
            soort = "GeldUitgegeven"
            mutation_type = "6"
            reasoning = "Salarisbetaling (geen BTW)"
            confidence = 0.90
        # Creditor payment reference
        elif any(w in desc_lower for w in ("factuur", "inv-", "factuurnummer", "declaratie")):
            soort = "FactuurbetalingVerstuurd"
            mutation_type = "4"
            grootboekcode = "1600"
            btw_code = "GEEN"
            reasoning = "Betaling op openstaande inkoopfactuur"
            confidence = 0.80
        else:
            soort = "GeldUitgegeven"
            mutation_type = "6"
            grootboekcode = "4500"
            btw_code = "HOOG_INK_21"
            reasoning = "Algemene kosten"
            confidence = 0.70

    # Resolve ledger ID from ledgers if available
    ledger_id: int | None = None
    for l in ledgers:
        code = str(l.get("code") or "").strip()
        if code == grootboekcode or code.zfill(4) == grootboekcode.zfill(4):
            ledger_id = int(l["id"])
            break

    return BankTransactionSuggestion(
        grootboekcode=grootboekcode,
        ledger_id=ledger_id,
        btw_code=btw_code,
        soort=soort,
        mutation_type=mutation_type,
        omschrijving=desc_clean[:100],
        confidence=confidence,
        reasoning=reasoning,
    )
