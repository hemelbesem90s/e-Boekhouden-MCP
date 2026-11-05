"""Tests for MCP tools."""

import pytest
from pydantic import ValidationError

from eboekhouden_mcp.tools import WRITE_TOOL_NAMES, ToolRegistry, register_all_tools
from eboekhouden_mcp.tools.base import BaseTool
from eboekhouden_mcp.tools.invoices import CreateInvoiceInput
from eboekhouden_mcp.tools.relations import ListRelationsInput


class TestToolRegistry:
    """Tests for the tool registry."""

    def test_register_and_get_tool(self):
        """Test registering and retrieving a tool."""
        registry = ToolRegistry()

        class TestTool(BaseTool):
            name = "test_tool"
            description = "A test tool"

            async def execute(self, client, arguments):
                return {"result": "ok"}

        tool = TestTool()
        registry.register(tool)

        assert registry.get("test_tool") == tool
        assert registry.get("nonexistent") is None

    def test_list_tools(self):
        """Test listing all registered tools."""
        registry = ToolRegistry()

        class Tool1(BaseTool):
            name = "tool1"
            description = "Tool 1"

            async def execute(self, client, arguments):
                return {}

        class Tool2(BaseTool):
            name = "tool2"
            description = "Tool 2"

            async def execute(self, client, arguments):
                return {}

        registry.register(Tool1())
        registry.register(Tool2())

        tools = registry.list_tools()
        assert len(tools) == 2

    def test_len_and_contains(self):
        """Test __len__ and __contains__ methods."""
        registry = ToolRegistry()

        class TestTool(BaseTool):
            name = "test"
            description = "Test"

            async def execute(self, client, arguments):
                return {}

        registry.register(TestTool())

        assert len(registry) == 1
        assert "test" in registry
        assert "other" not in registry


class TestRegisterAllTools:
    """Tests for registering all tools."""

    def test_registers_all_tools(self):
        """Test that all tools are registered (37 base + 14 power + 3 inbox + 29 web = 83)."""
        registry = ToolRegistry()
        register_all_tools(registry)

        # 37 base tools + 14 power tools + 3 inbox tools + 29 web session tools
        assert len(registry) == 83

    def test_tool_categories(self):
        """Test that tools from all categories are registered."""
        registry = ToolRegistry()
        register_all_tools(registry)

        # Check for at least one tool from each category
        assert "list_relations" in registry
        assert "list_invoices" in registry
        assert "list_mutations" in registry
        assert "list_products" in registry
        assert "list_ledgers" in registry
        assert "list_cost_centers" in registry
        assert "list_members" in registry
        assert "list_administrations" in registry
        assert "list_invoice_templates" in registry
        assert "list_units" in registry
        assert "send_file_to_digital_archive" in registry

    def test_all_side_effect_named_tools_are_guarded(self):
        """Conventional write verbs must never bypass the server write policy."""
        registry = ToolRegistry()
        register_all_tools(registry)

        side_effect_tools = {
            tool.name
            for tool in registry.list_tools()
            if tool.name.startswith(
                (
                    "create_",
                    "update_",
                    "delete_",
                    "send_",
                    "attach_",
                    "save_",
                    "move_",
                    "upload_",
                    "invoice_",
                )
            )
        }

        assert side_effect_tools == WRITE_TOOL_NAMES


class TestToolSchemas:
    """Tests for tool input schemas."""

    def test_get_schema(self):
        """Test that tools return valid JSON schemas."""
        registry = ToolRegistry()
        register_all_tools(registry)

        for tool in registry.list_tools():
            schema = tool.get_schema()
            assert isinstance(schema, dict)
            # Should not have title (MCP expects clean schema)
            assert "title" not in schema

    def test_paginated_inputs_enforce_api_bounds(self):
        schema = ListRelationsInput.model_json_schema()

        assert schema["properties"]["limit"]["anyOf"][0]["minimum"] == 1
        assert schema["properties"]["limit"]["anyOf"][0]["maximum"] == 2000
        assert schema["properties"]["offset"]["anyOf"][0]["minimum"] == 0

        with pytest.raises(ValidationError):
            ListRelationsInput(limit=2001)
        with pytest.raises(ValidationError):
            ListRelationsInput(offset=-1)

    def test_invoice_items_have_a_strict_nested_schema(self):
        schema = CreateInvoiceInput.model_json_schema()

        assert schema["properties"]["items"]["minItems"] == 1
        assert schema["properties"]["items"]["maxItems"] == 500
        assert schema["$defs"]["InvoiceItemInput"]["additionalProperties"] is False
        assert (
            schema["$defs"]["CreateInvoiceEmailInput"]["additionalProperties"] is False
        )
        assert (
            schema["$defs"]["CreateInvoiceMutationInput"]["additionalProperties"]
            is False
        )

        with pytest.raises(ValidationError):
            CreateInvoiceInput(
                relation_id=1,
                term_of_payment=14,
                template_id=1,
                items=[{"description": "Missing required line fields"}],
            )

    @pytest.mark.asyncio
    async def test_create_invoice_tool_passes_all_fields(self):
        from unittest.mock import MagicMock
        from eboekhouden.models.invoice import CreatedInvoice
        from eboekhouden_mcp.tools.invoices import CreateInvoiceTool

        mock_client = MagicMock()
        mock_client.invoices.create.return_value = CreatedInvoice(
            id=73143509, invoice_number="F00061", in_ex_vat="EX"
        )

        tool = CreateInvoiceTool()
        args = {
            "relation_id": 70666663,
            "term_of_payment": 30,
            "template_id": 1532614,
            "invoice_number": "F00061",
            "date": "2026-11-03",
            "in_ex_vat": "EX",
            "email_template_id": 1332610,
            "reference": "26076D_XNDR",
            "text": "Invoice notes",
            "print": False,
            "items": [
                {
                    "description": "26076D XNDR - Regenpijpbeschermer",
                    "vat_code": "HOOG_VERK_21",
                    "ledger_id": 51798501,
                    "quantity": 3.0,
                    "price_per_unit": 75.0,
                    "cost_center_id": 492221,
                    "unit_id": 3192209,
                    "code": "UREN",
                    "product_id": 1001,
                    "vat_amount": 47.25,
                    "discount_amount": 0.0,
                    "discount_percentage": 0.0,
                }
            ],
            "email": {
                "from_email": "jacob@groblerengineering.nl",
                "from_name": "Jacob Grobler",
                "subject": "Factuur F00061",
                "body": "Beste Xander, hierbij de factuur.",
                "attach_ubl": True,
            },
            "mutation": {
                "description": "26076D XNDR - Factuur F00061",
                "check_payment_reference": True,
                "payment_reference": "26076D",
                "ledger_id": 51798176,
            },
        }

        result = await tool.execute(mock_client, args)
        assert result["id"] == 73143509
        assert result["invoice_number"] == "F00061"

        mock_client.invoices.create.assert_called_once()
        inv_arg = mock_client.invoices.create.call_args[0][0]
        assert inv_arg.relation_id == 70666663
        assert inv_arg.email_template_id == 1332610
        assert inv_arg.print is False
        assert inv_arg.email is not None
        assert inv_arg.email.from_email == "jacob@groblerengineering.nl"
        assert inv_arg.email.attach_ubl is True
        assert inv_arg.mutation is not None
        assert inv_arg.mutation.ledger_id == 51798176
        assert len(inv_arg.items) == 1
        item = inv_arg.items[0]
        assert item.unit_id == 3192209
        assert item.code == "UREN"
        assert item.product_id == 1001
        assert float(item.vat_amount) == 47.25

    def test_send_invoice_schema(self):
        from eboekhouden_mcp.web_session.tools import SendInvoiceInput

        schema = SendInvoiceInput.model_json_schema()
        assert schema["additionalProperties"] is False
        assert "invoice_id" in schema["required"]

        with pytest.raises(ValidationError):
            SendInvoiceInput(invoice_id=73143509, unexpected_param="bad")

    def test_tool_inputs_reject_unknown_fields(self):
        with pytest.raises(ValidationError):
            ListRelationsInput(unexpected=True)

    def test_unit_model_parses_singular_and_plural(self):
        from eboekhouden.models.unit import UnitList

        parsed = UnitList.model_validate(
            {
                "items": [{"id": 1, "singular": "uur", "plural": "uren"}],
                "count": 1,
            }
        )
        assert parsed.count == 1
        assert parsed.items[0].singular == "uur"
        assert parsed.items[0].plural == "uren"
        assert parsed.items[0].description == "uur"
