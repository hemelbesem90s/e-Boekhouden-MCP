"""Report / aggregate tools built from client-side rollups.

The API exposes only per-ledger balance and outstanding-invoice endpoints, so
trial balance, P&L, balance sheet, VAT summary and AR/AP aging are all computed
here by iterating the primitives. Each of these replaces many individual tool
calls with a single one.
"""

from __future__ import annotations

import datetime
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from pydantic import Field

from eboekhouden_mcp.tools.base import BaseTool, ToolSchema
from eboekhouden_mcp.tools.power._helpers import (
    compact,
    parse_date,
    range_date_filter,
    round2,
    to_markdown_table,
)

if TYPE_CHECKING:
    from eboekhouden import EBoekhoudenClient

_SCAN_LIMIT_DEFAULT = 500
_MAX_DETAILS_DEFAULT = 50


# Local date parser (kept here so _helpers stays filter-focused).
def _pdate(value: Any) -> datetime.date | None:
    return parse_date(value)


# --------------------------------------------------------------------------- #
# ledger balance rollups: trial balance, P&L, balance sheet
# --------------------------------------------------------------------------- #


def _ledger_balances(
    client: "EBoekhoudenClient",
    *,
    from_date: datetime.date | None,
    to_date: datetime.date | None,
    cost_center_id: int | None,
    categories: set[str] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ledger in client.ledgers.iter_all(limit=2000):
        if categories is not None and ledger.category not in categories:
            continue
        balance = client.ledgers.get_balance(
            ledger.id,
            cost_center_id=cost_center_id,
            from_date=from_date,
            to_date=to_date,
        )
        rows.append(
            {
                "ledger_id": ledger.id,
                "code": ledger.code,
                "description": ledger.description,
                "category": ledger.category,
                "balance": round2(balance.balance),
            }
        )
    return rows


class _PeriodInput(ToolSchema):
    from_date: str | None = Field(default=None, description="Period start (YYYY-MM-DD)")
    to_date: str | None = Field(default=None, description="Period end (YYYY-MM-DD)")
    cost_center_id: int | None = Field(
        default=None, description="Restrict to a single cost center"
    )
    format: str | None = Field(
        default="json", description="'json' (default) or 'markdown'"
    )


class TrialBalanceInput(_PeriodInput):
    category: str | None = Field(
        default=None,
        description="Restrict to one ledger category (e.g. BAL, VW, FIN, DEB, CRED)",
    )


class TrialBalanceTool(BaseTool):
    """Trial balance across all ledgers, grouped by category."""

    name = "get_trial_balance"
    description = (
        "Trial balance: the balance of every ledger over an optional period, grouped by category "
        "(BAL/VW/etc.) with subtotals and a grand total. One call instead of get_ledger_balance per account."
    )
    input_schema = TrialBalanceInput

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        category = arguments.get("category")
        categories = {category} if category else None
        rows = _ledger_balances(
            client,
            from_date=_pdate(arguments.get("from_date")),
            to_date=_pdate(arguments.get("to_date")),
            cost_center_id=arguments.get("cost_center_id"),
            categories=categories,
        )

        by_category: dict[str, Any] = {}
        for row in rows:
            bucket = by_category.setdefault(
                row["category"], {"total": 0.0, "accounts": []}
            )
            bucket["accounts"].append(row)
            bucket["total"] = round2(bucket["total"] + row["balance"])

        result: dict[str, Any] = {
            "from_date": arguments.get("from_date"),
            "to_date": arguments.get("to_date"),
            "account_count": len(rows),
            "by_category": by_category,
            "grand_total": round2(sum(row["balance"] for row in rows)),
        }
        if arguments.get("format") == "markdown":
            result["markdown"] = to_markdown_table(
                rows, ["code", "description", "category", "balance"]
            )
        return result


class ProfitLossTool(BaseTool):
    """Profit & loss over the VW (result) ledgers."""

    name = "get_profit_loss"
    description = (
        "Profit & loss over the period: revenue vs expense across VW ledgers and the net result. "
        "Convention: positive balance = debit (expense), negative = credit (revenue)."
    )
    input_schema = _PeriodInput

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        rows = _ledger_balances(
            client,
            from_date=_pdate(arguments.get("from_date")),
            to_date=_pdate(arguments.get("to_date")),
            cost_center_id=arguments.get("cost_center_id"),
            categories={"VW"},
        )
        expenses = [r for r in rows if r["balance"] > 0]
        revenue = [r for r in rows if r["balance"] < 0]
        expense_total = round2(sum(r["balance"] for r in expenses))
        revenue_total = round2(-sum(r["balance"] for r in revenue))

        result: dict[str, Any] = {
            "from_date": arguments.get("from_date"),
            "to_date": arguments.get("to_date"),
            "revenue_total": revenue_total,
            "expense_total": expense_total,
            "net_result": round2(revenue_total - expense_total),
            "revenue": revenue,
            "expenses": expenses,
        }
        if arguments.get("format") == "markdown":
            result["markdown"] = to_markdown_table(
                rows, ["code", "description", "balance"]
            )
        return result


class BalanceSheetTool(BaseTool):
    """Balance sheet over the BAL ledgers."""

    name = "get_balance_sheet"
    description = (
        "Balance sheet: assets vs liabilities across BAL ledgers with totals. "
        "Convention: positive balance = debit (asset), negative = credit (liability/equity)."
    )
    input_schema = _PeriodInput

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        rows = _ledger_balances(
            client,
            from_date=_pdate(arguments.get("from_date")),
            to_date=_pdate(arguments.get("to_date")),
            cost_center_id=arguments.get("cost_center_id"),
            categories={"BAL"},
        )
        assets = [r for r in rows if r["balance"] > 0]
        liabilities = [r for r in rows if r["balance"] < 0]
        assets_total = round2(sum(r["balance"] for r in assets))
        liabilities_total = round2(-sum(r["balance"] for r in liabilities))

        result: dict[str, Any] = {
            "as_of": arguments.get("to_date"),
            "assets_total": assets_total,
            "liabilities_total": liabilities_total,
            "difference": round2(assets_total - liabilities_total),
            "rows": rows,
        }
        if arguments.get("format") == "markdown":
            result["markdown"] = to_markdown_table(
                rows, ["code", "description", "balance"]
            )
        return result


# --------------------------------------------------------------------------- #
# VAT summary
# --------------------------------------------------------------------------- #


class VatSummaryInput(ToolSchema):
    date_from: str | None = Field(default=None, description="Period start (YYYY-MM-DD)")
    date_to: str | None = Field(default=None, description="Period end (YYYY-MM-DD)")
    max_details: int = Field(
        default=_MAX_DETAILS_DEFAULT,
        description="Max mutations to hydrate (accuracy is bounded by this; raise for full periods)",
    )
    scan_limit: int = Field(
        default=_SCAN_LIMIT_DEFAULT,
        description="Max candidate mutations pulled before hydration (max 2000)",
    )
    format: str | None = Field(
        default="json", description="'json' (default) or 'markdown'"
    )


class VatSummaryTool(BaseTool):
    """VAT totals per code over a period."""

    name = "get_vat_summary"
    description = (
        "VAT summary over a period: total VAT per VAT code across mutations, with an overall total. "
        "Hydrates mutation detail (bounded by max_details) since the VAT breakdown is detail-only."
    )
    input_schema = VatSummaryInput

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        scan_limit = min(int(arguments.get("scan_limit", _SCAN_LIMIT_DEFAULT)), 2000)
        max_details = int(arguments.get("max_details", _MAX_DETAILS_DEFAULT))

        candidates = client.mutations.list(
            limit=scan_limit,
            date_filter=range_date_filter(
                arguments.get("date_from"), arguments.get("date_to")
            ),
        ).items
        truncated = len(candidates) > max_details or len(candidates) >= scan_limit
        selected = candidates[:max_details]

        totals: dict[str, float] = defaultdict(float)
        for item in selected:
            detail = client.mutations.get(item.id)
            vat_entries = detail.vat or []
            if vat_entries:
                for entry in vat_entries:
                    code = str(entry.vat_code)
                    totals[code] += float(entry.amount)
            else:
                for row in detail.rows:
                    if row.vat_amount is not None and row.vat_code:
                        totals[str(row.vat_code)] += float(row.vat_amount)

        by_code = [
            {"vat_code": code, "amount": round2(amount)}
            for code, amount in sorted(totals.items())
        ]
        result: dict[str, Any] = {
            "date_from": arguments.get("date_from"),
            "date_to": arguments.get("date_to"),
            "mutations_scanned": len(selected),
            "truncated": truncated,
            "by_code": by_code,
            "total_vat": round2(sum(totals.values())),
        }
        if truncated:
            result["hint"] = (
                "Not all mutations in range were hydrated; raise max_details/scan_limit or narrow the period for exact figures."
            )
        if arguments.get("format") == "markdown":
            result["markdown"] = to_markdown_table(by_code, ["vat_code", "amount"])
        return result


# --------------------------------------------------------------------------- #
# AR/AP aging
# --------------------------------------------------------------------------- #

_BUCKETS = ("0-30", "31-60", "61-90", "90+")


def _bucket_for(age_days: int) -> str:
    if age_days <= 30:
        return "0-30"
    if age_days <= 60:
        return "31-60"
    if age_days <= 90:
        return "61-90"
    return "90+"


def _age_side(
    client: "EBoekhoudenClient", cred_deb: str, reference: datetime.date
) -> dict[str, Any]:
    per_relation: dict[int, dict[str, Any]] = {}
    bucket_totals: dict[str, float] = {bucket: 0.0 for bucket in _BUCKETS}
    grand_total = 0.0

    for invoice in client.mutations.iter_outstanding(cred_deb):
        amount = float(invoice.outstanding_amount)
        age = (reference - invoice.date).days
        bucket = _bucket_for(age)
        bucket_totals[bucket] = round2(bucket_totals[bucket] + amount)
        grand_total += amount

        relation = per_relation.setdefault(
            invoice.relation_id,
            {
                "relation_id": invoice.relation_id,
                "company": invoice.company,
                "total": 0.0,
                "buckets": {b: 0.0 for b in _BUCKETS},
            },
        )
        relation["total"] = round2(relation["total"] + amount)
        relation["buckets"][bucket] = round2(relation["buckets"][bucket] + amount)

    return {
        "total": round2(grand_total),
        "bucket_totals": bucket_totals,
        "relations": list(per_relation.values()),
    }


class ArApAgingInput(ToolSchema):
    cred_deb: str | None = Field(
        default=None,
        description="'D' debtors (AR) or 'C' creditors (AP). Omit for both.",
    )
    reference_date: str | None = Field(
        default=None,
        description="Age invoices relative to this date (YYYY-MM-DD). Defaults to today.",
    )
    format: str | None = Field(
        default="json", description="'json' (default) or 'markdown'"
    )


class ArApAgingTool(BaseTool):
    """Outstanding AR/AP aged into buckets, per relation."""

    name = "get_ar_ap_aging"
    description = (
        "Accounts receivable / payable aging: outstanding invoices bucketed by age "
        "(0-30/31-60/61-90/90+) per relation, with bucket and grand totals. 'D'=debtors, 'C'=creditors."
    )
    input_schema = ArApAgingInput

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        reference = _pdate(arguments.get("reference_date")) or datetime.date.today()
        requested = arguments.get("cred_deb")
        sides = [requested] if requested else ["D", "C"]

        result: dict[str, Any] = {"reference_date": reference.isoformat()}
        for side in sides:
            key = "debtors" if side == "D" else "creditors"
            result[key] = _age_side(client, side, reference)

        if arguments.get("format") == "markdown":
            markdowns = []
            for side in sides:
                key = "debtors" if side == "D" else "creditors"
                rows = [
                    {
                        "relation_id": r["relation_id"],
                        "company": r["company"],
                        "total": r["total"],
                    }
                    for r in result[key]["relations"]
                ]
                markdowns.append(
                    f"### {key}\n"
                    + to_markdown_table(rows, ["relation_id", "company", "total"])
                )
            result["markdown"] = "\n\n".join(markdowns)
        return result


# --------------------------------------------------------------------------- #
# ledger transactions
# --------------------------------------------------------------------------- #


class LedgerTransactionsInput(ToolSchema):
    ledger_id: int = Field(description="Ledger account id to report transactions for")
    date_from: str | None = Field(
        default=None, description="On/after date (YYYY-MM-DD)"
    )
    date_to: str | None = Field(default=None, description="On/before date (YYYY-MM-DD)")
    max_details: int = Field(
        default=_MAX_DETAILS_DEFAULT, description="Max matching mutations to return"
    )
    scan_limit: int = Field(
        default=_SCAN_LIMIT_DEFAULT,
        description="Max candidate mutations pulled before filtering (max 2000)",
    )
    verbose: bool = Field(
        default=False, description="Return raw JSON instead of compact output"
    )


class LedgerTransactionsTool(BaseTool):
    """All mutations touching a ledger over a period, with a running total."""

    name = "get_ledger_transactions"
    description = (
        "Every mutation that touches a given ledger (on the header or any line) over a period, "
        "with the per-mutation amount attributable to that ledger and a running total."
    )
    input_schema = LedgerTransactionsInput

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        ledger_id = arguments["ledger_id"]
        verbose = bool(arguments.get("verbose", False))
        scan_limit = min(int(arguments.get("scan_limit", _SCAN_LIMIT_DEFAULT)), 2000)
        max_details = int(arguments.get("max_details", _MAX_DETAILS_DEFAULT))

        candidates = client.mutations.list(
            limit=scan_limit,
            date_filter=range_date_filter(
                arguments.get("date_from"), arguments.get("date_to")
            ),
        ).items
        scan_truncated = len(candidates) >= scan_limit

        rows: list[dict[str, Any]] = []
        total = 0.0
        output_truncated = False
        for item in candidates:
            detail = client.mutations.get(item.id)
            row_amount = sum(
                float(row.amount) for row in detail.rows if row.ledger_id == ledger_id
            )
            header_match = detail.ledger_id == ledger_id
            if row_amount == 0.0 and not header_match:
                continue
            ledger_amount = round2(row_amount if row_amount else float(item.amount))
            total += ledger_amount
            entry = {
                "mutation_id": detail.id,
                "date": item.date,
                "type": detail.type,
                "invoice_number": detail.invoice_number,
                "relation_id": detail.relation_id,
                "ledger_amount": ledger_amount,
            }
            rows.append(entry if verbose else compact(entry))
            if len(rows) >= max_details:
                output_truncated = True
                break

        return {
            "ledger_id": ledger_id,
            "date_from": arguments.get("date_from"),
            "date_to": arguments.get("date_to"),
            "count": len(rows),
            "total": round2(total),
            "truncated": scan_truncated or output_truncated,
            "items": rows,
        }


# --------------------------------------------------------------------------- #
# cash flow, reserve adequacy & safe-to-spend
# --------------------------------------------------------------------------- #


def _classify_reserve_type(code: str, description: str) -> str | None:
    """Classify a FIN ledger as a specific reserve jar type, or None if operating."""
    desc_low = (description or "").lower()
    if any(k in desc_low for k in ("btw", "vat", "omzetbelasting")):
        return "vat"
    if any(k in desc_low for k in ("belasting", "tax", "inkomstenbelasting", "vpb")):
        return "tax"
    if any(k in desc_low for k in ("investering", "invest", "capex", "hardware")):
        return "investment"
    if any(k in desc_low for k in ("spaar", "reserve", "buffer", "potje", "jar")):
        return "general"
    return None


class CashFlowReportInput(ToolSchema):
    as_of: str | None = Field(
        default=None,
        description="Reference date (YYYY-MM-DD) for balances and aging. Defaults to today.",
    )
    from_date: str | None = Field(
        default=None,
        description="Start date (YYYY-MM-DD) for YTD net profit and tax accrual calculation. Defaults to Jan 1 of as_of year.",
    )
    operating_ledger_codes: list[str] | None = Field(
        default=None,
        description="Optional explicit ledger codes for main operating bank accounts (e.g. ['133000']).",
    )
    reserve_ledger_codes: list[str] | None = Field(
        default=None,
        description="Optional explicit ledger codes for reserve/jar accounts (e.g. ['133001', '133002', '133003']).",
    )
    effective_tax_rate: float = Field(
        default=0.20,
        ge=0.0,
        le=0.60,
        description="Estimated effective income/corporate tax rate on YTD net profit (default 0.20 = 20%).",
    )
    minimum_operating_buffer: float = Field(
        default=2500.0,
        ge=0.0,
        description="Minimum safety floor to keep untouched in the main operating account (default EUR 2,500).",
    )
    monthly_fixed_commitments: float = Field(
        default=0.0,
        ge=0.0,
        description="Next 30 days of recurring fixed commitments + owner draw to deduct when computing Safe-to-Spend.",
    )


class CashFlowReportTool(BaseTool):
    """Liquid cash split (operating vs reserve jars), tax/VAT reserve adequacy, and Safe-to-Spend."""

    name = "get_cash_flow"
    description = (
        "Cashflow & Safe-to-Spend report: separates main operating bank accounts from ring-fenced "
        "reserve jars (VAT/BTW, Income Tax, Investment), checks whether tax/VAT jars cover accrued "
        "liabilities, flags unallocated suspense balances (218200), and computes true Safe-to-Spend."
    )
    input_schema = CashFlowReportInput

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        as_of_date = _pdate(arguments.get("as_of")) or datetime.date.today()
        from_date = _pdate(arguments.get("from_date")) or datetime.date(
            as_of_date.year, 1, 1
        )
        op_codes = set(arguments.get("operating_ledger_codes") or [])
        res_codes = set(arguments.get("reserve_ledger_codes") or [])
        tax_rate = float(arguments.get("effective_tax_rate", 0.20))
        min_buffer = float(arguments.get("minimum_operating_buffer", 2500.0))
        fixed_commitments = float(arguments.get("monthly_fixed_commitments", 0.0))

        # 1. Fetch relevant liquid (FIN), VAT (BTWRC/AF*/VOOR), and suspense (218200) ledgers
        q_month = ((as_of_date.month - 1) // 3) * 3 + 1
        q_start = datetime.date(as_of_date.year, q_month, 1)
        q_end = datetime.date(as_of_date.year, 12, 31) if q_month == 10 else (
            datetime.date(as_of_date.year, q_month + 3, 1) - datetime.timedelta(days=1)
        )

        vat_categories = {"BTWRC", "AF6", "AF19", "AFOVERIG", "VOOR"}
        operating_accounts: list[dict[str, Any]] = []
        reserve_accounts: list[dict[str, Any]] = []
        suspense_accounts: list[dict[str, Any]] = []
        cum_vat_credit = 0.0
        qtr_vat_credit = 0.0
        vw_rows: list[dict[str, Any]] = []

        for ledger in client.ledgers.iter_all(limit=2000):
            cat = ledger.category
            code = str(ledger.code)
            desc = str(ledger.description or "")
            desc_low = desc.lower()

            if cat == "FIN":
                bal = round2(
                    client.ledgers.get_balance(ledger.id, to_date=as_of_date).balance
                )
                row = {
                    "ledger_id": ledger.id,
                    "code": code,
                    "description": desc,
                    "category": cat,
                    "balance": bal,
                }
                rtype = _classify_reserve_type(code, desc)
                if code in op_codes:
                    rtype = None
                elif code in res_codes and rtype is None:
                    rtype = "general"

                if rtype is None:
                    operating_accounts.append(row)
                else:
                    reserve_accounts.append({**row, "reserve_type": rtype})

            elif cat in vat_categories:
                bal_cum = float(
                    client.ledgers.get_balance(ledger.id, to_date=as_of_date).balance
                )
                cum_vat_credit += -bal_cum
                if cat != "BTWRC":
                    bal_qtr = float(
                        client.ledgers.get_balance(
                            ledger.id, from_date=q_start, to_date=q_end
                        ).balance
                    )
                    qtr_vat_credit += -bal_qtr

            elif cat == "BAL":
                is_suspense = code == "218200" or any(
                    k in desc_low
                    for k in ("rubriceren", "vraagpost", "suspense", "tussenrekening")
                )
                is_bal_vat = code in ("195700", "195010", "195020", "195300") or (
                    code.startswith("195") and "btw" in desc_low
                )
                if is_suspense or is_bal_vat:
                    bal = round2(
                        client.ledgers.get_balance(
                            ledger.id, to_date=as_of_date
                        ).balance
                    )
                    if is_suspense and abs(bal) > 0.004:
                        suspense_accounts.append(
                            {
                                "ledger_id": ledger.id,
                                "code": code,
                                "description": desc,
                                "category": cat,
                                "balance": bal,
                            }
                        )
                    elif is_bal_vat:
                        cum_vat_credit += -bal

            elif cat == "VW":
                bal = round2(
                    client.ledgers.get_balance(
                        ledger.id, from_date=from_date, to_date=as_of_date
                    ).balance
                )
                vw_rows.append(
                    {
                        "ledger_id": ledger.id,
                        "code": code,
                        "description": desc,
                        "category": cat,
                        "balance": bal,
                    }
                )

        accrued_vat_credit = max(cum_vat_credit, qtr_vat_credit)

        operating_total = round2(sum(a["balance"] for a in operating_accounts))
        reserve_total = round2(sum(a["balance"] for a in reserve_accounts))
        total_liquid = round2(operating_total + reserve_total)

        vat_jar_bal = round2(
            sum(
                a["balance"]
                for a in reserve_accounts
                if a.get("reserve_type") == "vat"
            )
        )
        tax_jar_bal = round2(
            sum(
                a["balance"]
                for a in reserve_accounts
                if a.get("reserve_type") == "tax"
            )
        )
        inv_jar_bal = round2(
            sum(
                a["balance"]
                for a in reserve_accounts
                if a.get("reserve_type") == "investment"
            )
        )

        accrued_vat_payable = round2(max(0.0, accrued_vat_credit))
        vat_shortfall = round2(max(0.0, accrued_vat_payable - vat_jar_bal))

        ytd_revenue = round2(-sum(r["balance"] for r in vw_rows if r["balance"] < 0))
        ytd_expense = round2(sum(r["balance"] for r in vw_rows if r["balance"] > 0))
        ytd_net_profit = round2(ytd_revenue - ytd_expense)
        estimated_tax = round2(max(0.0, ytd_net_profit * tax_rate))
        tax_shortfall = round2(max(0.0, estimated_tax - tax_jar_bal))

        open_ar = round2(
            sum(
                float(inv.outstanding_amount)
                for inv in client.mutations.iter_outstanding("D")
            )
        )
        open_ap = round2(
            sum(
                float(inv.outstanding_amount)
                for inv in client.mutations.iter_outstanding("C")
            )
        )

        immediate_safe = round2(
            operating_total
            - open_ap
            - vat_shortfall
            - fixed_commitments
            - min_buffer
        )
        strict_safe = round2(immediate_safe - tax_shortfall)
        projected_30d_safe = round2(strict_safe + open_ar)

        return {
            "as_of": as_of_date.isoformat(),
            "from_date": from_date.isoformat(),
            "liquid_accounts": {
                "operating_accounts": operating_accounts,
                "operating_total": operating_total,
                "reserve_accounts": reserve_accounts,
                "reserve_total": reserve_total,
                "total_liquid_cash": total_liquid,
            },
            "suspense_accounts": suspense_accounts,
            "reconciliation_clean": len(suspense_accounts) == 0,
            "reserve_adequacy": {
                "accrued_vat_payable": accrued_vat_payable,
                "vat_reserve_balance": vat_jar_bal,
                "vat_reserve_shortfall": vat_shortfall,
                "ytd_net_profit": ytd_net_profit,
                "effective_tax_rate": tax_rate,
                "estimated_tax_liability": estimated_tax,
                "tax_reserve_balance": tax_jar_bal,
                "tax_reserve_shortfall": tax_shortfall,
                "investment_reserve_balance": inv_jar_bal,
            },
            "open_ar_ap": {
                "open_debtors_total": open_ar,
                "open_creditors_total": open_ap,
                "net_ar_ap": round2(open_ar - open_ap),
            },
            "safe_to_spend": {
                "operating_cash_now": operating_total,
                "less_open_creditors": open_ap,
                "less_vat_reserve_shortfall": vat_shortfall,
                "less_monthly_fixed_commitments": fixed_commitments,
                "less_minimum_operating_buffer": min_buffer,
                "immediate_safe_to_spend_from_operating": immediate_safe,
                "less_tax_reserve_shortfall": tax_shortfall,
                "strict_safe_to_spend_after_all_accruals": strict_safe,
                "projected_30d_safe_to_spend_with_ar": projected_30d_safe,
                "investment_jar_available": inv_jar_bal,
            },
        }


# --------------------------------------------------------------------------- #
# budget vs actuals & spend gate
# --------------------------------------------------------------------------- #


class BudgetEnvelopeSpec(ToolSchema):
    name: str = Field(description="Budget envelope name (e.g. 'Software & AI')")
    ledger_codes: list[str] = Field(
        min_length=1,
        description="Ledger codes mapped to this envelope (e.g. ['431400', '430400'])",
    )
    monthly_budget: float = Field(
        ge=0.0, description="Target monthly budget in EUR (excl. VAT)"
    )


class BudgetVsActualsInput(ToolSchema):
    from_date: str | None = Field(
        default=None,
        description="Budget period start (YYYY-MM-DD). Defaults to Jan 1 of current year.",
    )
    to_date: str | None = Field(
        default=None,
        description="Budget period end (YYYY-MM-DD). Defaults to today.",
    )
    envelopes: list[BudgetEnvelopeSpec] | None = Field(
        default=None,
        description="Optional list of budget envelopes mapping ledger_codes to monthly_budget.",
    )
    proposed_spend_amount: float | None = Field(
        default=None,
        ge=0.0,
        description="Optional EUR amount (excl. VAT) of a purchase being evaluated against budget.",
    )
    proposed_spend_envelope: str | None = Field(
        default=None,
        description="Envelope name or ledger code for the proposed purchase.",
    )


class BudgetVsActualsTool(BaseTool):
    """Compare actual P&L expenses against monthly/YTD budget envelopes and evaluate proposed spend."""

    name = "get_budget_vs_actuals"
    description = (
        "Budget vs. Actuals & Spend Gate: compares YTD/period expense ledgers against target "
        "monthly budget envelopes, reports remaining prorated and annual budget per category, "
        "and evaluates whether a proposed purchase fits within budget."
    )
    input_schema = BudgetVsActualsInput

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        to_date = _pdate(arguments.get("to_date")) or datetime.date.today()
        from_date = _pdate(arguments.get("from_date")) or datetime.date(
            to_date.year, 1, 1
        )
        days_elapsed = max(1, (to_date - from_date).days + 1)
        months_elapsed = round2(max(1.0, days_elapsed / 30.4375))

        vw_rows = _ledger_balances(
            client,
            from_date=from_date,
            to_date=to_date,
            cost_center_id=None,
            categories={"VW"},
        )
        revenue_total = round2(-sum(r["balance"] for r in vw_rows if r["balance"] < 0))
        expense_rows = [r for r in vw_rows if r["balance"] > 0]
        expense_by_code = {str(r["code"]): r for r in expense_rows}
        expense_total = round2(sum(r["balance"] for r in expense_rows))

        raw_envelopes = arguments.get("envelopes") or []
        covered_codes: set[str] = set()
        envelope_results: list[dict[str, Any]] = []

        for env in raw_envelopes:
            env_name = str(env["name"])
            codes = [str(c) for c in env["ledger_codes"]]
            covered_codes.update(codes)
            m_budget = float(env["monthly_budget"])
            prorated_budget = round2(m_budget * months_elapsed)
            annual_budget = round2(m_budget * 12.0)
            actual = round2(
                sum(
                    float(expense_by_code[c]["balance"])
                    for c in codes
                    if c in expense_by_code
                )
            )
            rem_prorated = round2(prorated_budget - actual)
            rem_annual = round2(annual_budget - actual)
            util_pct = (
                round2((actual / prorated_budget) * 100.0)
                if prorated_budget > 0
                else (100.0 if actual > 0 else 0.0)
            )
            if actual > prorated_budget:
                status = "OVER_BUDGET"
            elif util_pct >= 85.0:
                status = "WATCH"
            else:
                status = "SAFE"

            envelope_results.append(
                {
                    "name": env_name,
                    "ledger_codes": codes,
                    "monthly_budget": m_budget,
                    "prorated_budget": prorated_budget,
                    "annual_budget": annual_budget,
                    "actual_spent": actual,
                    "monthly_actual_avg": round2(actual / months_elapsed),
                    "remaining_prorated": rem_prorated,
                    "remaining_annual": rem_annual,
                    "utilization_pct": util_pct,
                    "status": status,
                }
            )

        unbudgeted = [
            {
                **r,
                "monthly_actual_avg": round2(float(r["balance"]) / months_elapsed),
            }
            for r in expense_rows
            if str(r["code"]) not in covered_codes
        ]

        evaluation = None
        prop_amt = arguments.get("proposed_spend_amount")
        prop_target = arguments.get("proposed_spend_envelope")
        if prop_amt is not None:
            amt = float(prop_amt)
            matched_env = None
            if prop_target:
                t_low = str(prop_target).lower()
                for er in envelope_results:
                    if (
                        er["name"].lower() == t_low
                        or t_low in er["name"].lower()
                        or str(prop_target) in er["ledger_codes"]
                    ):
                        matched_env = er
                        break
            if matched_env is not None:
                rem_p = round2(matched_env["remaining_prorated"] - amt)
                rem_a = round2(matched_env["remaining_annual"] - amt)
                evaluation = {
                    "proposed_amount": amt,
                    "envelope": matched_env["name"],
                    "fits_prorated_budget": rem_p >= 0.0,
                    "fits_annual_budget": rem_a >= 0.0,
                    "remaining_prorated_after_spend": rem_p,
                    "remaining_annual_after_spend": rem_a,
                    "verdict": (
                        "APPROVED_WITHIN_PRORATED_BUDGET"
                        if rem_p >= 0.0
                        else (
                            "FITS_ANNUAL_BUT_EXCEEDS_YTD_PACE"
                            if rem_a >= 0.0
                            else "EXCEEDS_ANNUAL_BUDGET"
                        )
                    ),
                }
            else:
                evaluation = {
                    "proposed_amount": amt,
                    "envelope": prop_target or "Unassigned",
                    "verdict": "NO_MATCHING_ENVELOPE",
                }

        return {
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
            "months_elapsed": months_elapsed,
            "revenue_total": revenue_total,
            "expense_total": expense_total,
            "net_result": round2(revenue_total - expense_total),
            "envelopes": envelope_results,
            "unbudgeted_expenses": unbudgeted,
            "proposed_spend_evaluation": evaluation,
        }
