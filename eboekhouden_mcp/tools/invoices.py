"""Invoice tools for the e-Boekhouden MCP server."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field

from eboekhouden.models.invoice import (
    CreateInvoice,
    CreateInvoiceEmail,
    CreateInvoiceItem,
    CreateInvoiceMutation,
)
from eboekhouden_mcp.tools.base import BaseTool, PaginatedInput, ToolSchema

if TYPE_CHECKING:
    from eboekhouden import EBoekhoudenClient


class ListInvoicesInput(PaginatedInput):
    """Input schema for list_invoices tool."""

    invoice_number: str | None = Field(
        default=None, description="Filter by invoice number"
    )
    relation_id: int | None = Field(default=None, description="Filter by relation ID")
    date: str | None = Field(
        default=None, description="Filter by invoice date (YYYY-MM-DD)"
    )


class ListInvoicesTool(BaseTool):
    """List all invoices."""

    name = "list_invoices"
    description = (
        "List all invoices with optional filters for invoice number, relation, and date"
    )
    input_schema = ListInvoicesInput

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        result = client.invoices.list(
            limit=arguments.get("limit"),
            offset=arguments.get("offset"),
            invoice_number=arguments.get("invoice_number"),
            relation_id=arguments.get("relation_id"),
            date_filter=arguments.get("date"),
        )
        return {
            "count": result.count,
            "items": [item.model_dump() for item in result.items],
        }


class GetInvoiceInput(ToolSchema):
    """Input schema for get_invoice tool."""

    id: int = Field(description="Invoice ID")


class GetInvoiceTool(BaseTool):
    """Get an invoice by ID."""

    name = "get_invoice"
    description = "Get full details of an invoice by ID, including all line items, totals, VAT amounts, and PDF URL"
    input_schema = GetInvoiceInput

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        invoice = client.invoices.get(arguments["id"])
        return invoice.model_dump()


class InvoiceItemInput(ToolSchema):
    """Input schema for an invoice line item."""

    description: str = Field(min_length=1, description="Line item description")
    vat_code: str = Field(
        description="VAT code (e.g., 'HOOG_VERK_21' for 21% Dutch VAT)"
    )
    ledger_id: int = Field(ge=1, description="Ledger account ID")
    quantity: float | None = Field(default=None, description="Quantity")
    price_per_unit: float | None = Field(default=None, description="Price per unit")
    cost_center_id: int | None = Field(default=None, ge=1, description="Cost center ID")
    unit_id: int | None = Field(
        default=None, ge=1, description="Unit ID (e.g. 3192209 for 'uren')"
    )
    code: str | None = Field(
        default=None, max_length=50, description="Product or item code (e.g. 'UREN')"
    )
    product_id: int | None = Field(
        default=None, ge=1, description="Product ID from product catalog"
    )
    vat_amount: float | None = Field(
        default=None, description="Explicit VAT amount (auto-calculated if omitted)"
    )
    discount_amount: float | None = Field(default=None, description="Discount amount")
    discount_percentage: float | None = Field(
        default=None, description="Discount percentage (0-100)"
    )


class CreateInvoiceEmailInput(ToolSchema):
    """Email options for sending invoice immediately upon creation."""

    from_email: str | None = Field(default=None, description="Sender email address")
    from_name: str | None = Field(default=None, description="Sender display name")
    subject: str | None = Field(
        default=None, description="Email subject (required if body is provided)"
    )
    body: str | None = Field(
        default=None, description="Email body in HTML or plain text"
    )
    attach_ubl: bool = Field(
        default=False, description="Whether to attach the UBL e-invoice XML file"
    )


class CreateInvoiceMutationInput(ToolSchema):
    """Options for automatically creating a debtor mutation from the invoice."""

    description: str | None = Field(
        default=None, max_length=200, description="Mutation description"
    )
    check_payment_reference: bool | None = Field(
        default=None, description="Whether to check payment reference uniqueness"
    )
    payment_reference: str | None = Field(
        default=None, max_length=50, description="Payment reference"
    )
    ledger_id: int | None = Field(
        default=None,
        ge=1,
        description="Debtors ledger ID (e.g. 51798176 for 191000 Debiteuren)",
    )


class CreateInvoiceInput(ToolSchema):
    """Input schema for create_invoice tool."""

    relation_id: int = Field(ge=1, description="Relation (customer) ID")
    term_of_payment: int = Field(ge=0, description="Payment term in days")
    template_id: int = Field(ge=1, description="Invoice template ID")
    items: list[InvoiceItemInput] = Field(
        min_length=1,
        max_length=500,
        description="Invoice line items (1-500)",
    )
    invoice_number: str | None = Field(
        default=None,
        description="Custom invoice number (auto-generated if not provided)",
    )
    date: str | None = Field(
        default=None, description="Invoice date (YYYY-MM-DD, defaults to today)"
    )
    in_ex_vat: Literal["IN", "EX"] | None = Field(
        default=None, description="'IN' for VAT inclusive, 'EX' for VAT exclusive"
    )
    email_template_id: int | None = Field(
        default=None,
        ge=1,
        description="Email template ID to format and send invoice email (e.g. 1332610 for 'Normaal Factuur')",
    )
    reference: str | None = Field(
        default=None, max_length=50, description="Reference text (max 50 chars)"
    )
    text: str | None = Field(
        default=None,
        max_length=35000,
        description="Invoice text/notes (max 35000 chars)",
    )
    print: bool | None = Field(
        default=None, description="Whether to mark the invoice as ready for printing"
    )
    email: CreateInvoiceEmailInput | None = Field(
        default=None,
        description="Email options to send the invoice to the customer immediately",
    )
    mutation: CreateInvoiceMutationInput | None = Field(
        default=None,
        description="Options to automatically create a debtors mutation from the invoice",
    )


class CreateInvoiceTool(BaseTool):
    """Create a new invoice."""

    name = "create_invoice"
    description = (
        "Create a new invoice for a customer with line items. Each item needs description, "
        "VAT code, ledger ID, quantity, and price. Optionally dispatches by email immediately "
        "via email_template_id or email options, and books the debtor mutation."
    )
    input_schema = CreateInvoiceInput

    async def execute(
        self, client: "EBoekhoudenClient", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        # Convert item dicts to CreateInvoiceItem models
        items = []
        for item_data in arguments["items"]:
            items.append(
                CreateInvoiceItem(
                    description=item_data["description"],
                    vat_code=item_data["vat_code"],
                    ledger_id=item_data["ledger_id"],
                    quantity=item_data.get("quantity"),
                    price_per_unit=item_data.get("price_per_unit"),
                    cost_center_id=item_data.get("cost_center_id"),
                    unit_id=item_data.get("unit_id"),
                    code=item_data.get("code"),
                    product_id=item_data.get("product_id"),
                    vat_amount=item_data.get("vat_amount"),
                    discount_amount=item_data.get("discount_amount"),
                    discount_percentage=item_data.get("discount_percentage"),
                )
            )

        email_model = None
        if arguments.get("email"):
            email_data = arguments["email"]
            email_model = CreateInvoiceEmail(
                from_email=email_data.get("from_email"),
                from_name=email_data.get("from_name"),
                subject=email_data.get("subject"),
                body=email_data.get("body"),
                attach_ubl=email_data.get("attach_ubl", False),
            )

        mutation_model = None
        if arguments.get("mutation"):
            mut_data = arguments["mutation"]
            mutation_model = CreateInvoiceMutation(
                description=mut_data.get("description"),
                check_payment_reference=mut_data.get("check_payment_reference"),
                payment_reference=mut_data.get("payment_reference"),
                ledger_id=mut_data.get("ledger_id"),
            )

        invoice_data = CreateInvoice(
            relation_id=arguments["relation_id"],
            term_of_payment=arguments["term_of_payment"],
            template_id=arguments["template_id"],
            items=items,
            invoice_number=arguments.get("invoice_number"),
            date=arguments.get("date"),
            in_ex_vat=arguments.get("in_ex_vat"),
            email_template_id=arguments.get("email_template_id"),
            reference=arguments.get("reference"),
            text=arguments.get("text"),
            print=arguments.get("print"),
            email=email_model,
            mutation=mutation_model,
        )
        result = client.invoices.create(invoice_data)
        return {
            "id": result.id,
            "invoice_number": result.invoice_number,
            "in_ex_vat": result.in_ex_vat,
        }


# Re-export SendInvoiceTool from web session tools for convenience
from eboekhouden_mcp.web_session.tools import (  # noqa: E402
    SendInvoiceInput,
    SendInvoiceTool,
)

__all__ = [
    "ListInvoicesInput",
    "ListInvoicesTool",
    "GetInvoiceInput",
    "GetInvoiceTool",
    "InvoiceItemInput",
    "CreateInvoiceEmailInput",
    "CreateInvoiceMutationInput",
    "CreateInvoiceInput",
    "CreateInvoiceTool",
    "SendInvoiceInput",
    "SendInvoiceTool",
]
