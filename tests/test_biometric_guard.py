"""Unit tests for biometric verification guard and keyring integration."""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest
from mcp.types import CallToolRequest, CallToolRequestParams
from pydantic import ValidationError

from eboekhouden_mcp.biometric_guard import (
    BiometricHardwareUnavailable,
    BiometricVerificationDenied,
    format_write_prompt,
    verify_macos,
    verify_user_presence,
    verify_windows,
)
from eboekhouden_mcp.biometric_guard import (
    main as biometric_main,
)
from eboekhouden_mcp.config import MCPConfig
from eboekhouden_mcp.server import create_server
from eboekhouden_mcp.server import main as server_main

# ============================================================================
# 1. Prompt formatting tests
# ============================================================================


def test_format_write_prompt_invoice_with_explicit_amount():
    prompt = format_write_prompt(
        "create_invoice",
        {"amount": 1210.0, "relation_id": 42},
    )
    assert prompt == "e-Boekhouden MCP: Approve POST Invoice for EUR 1,210.00 to Relation 42?"


def test_format_write_prompt_invoice_computed_from_items():
    prompt = format_write_prompt(
        "create_invoice",
        {
            "relation_id": 10,
            "items": [
                {"description": "Consulting", "quantity": 10.0, "price_per_unit": 100.0},
                {"description": "VAT", "quantity": 1.0, "price_per_unit": 210.0},
            ],
        },
    )
    assert prompt == "e-Boekhouden MCP: Approve POST Invoice for EUR 1,210.00 to Relation 10?"


def test_format_write_prompt_invoice_without_amount():
    prompt = format_write_prompt("create_invoice", {"relation_id": 99})
    assert prompt == "e-Boekhouden MCP: Approve POST Invoice to Relation 99?"


def test_format_write_prompt_invoice_fallback_relation():
    prompt = format_write_prompt("create_invoice", {})
    assert prompt == "e-Boekhouden MCP: Approve POST Invoice to Relation [Relation]?"


def test_format_write_prompt_mutation_with_explicit_amount():
    prompt = format_write_prompt(
        "create_mutation",
        {"amount": 500.5, "ledger_id": 1000},
    )
    assert prompt == "e-Boekhouden MCP: Approve POST Mutation of EUR 500.50 on Ledger 1000?"


def test_format_write_prompt_mutation_computed_from_rows():
    prompt = format_write_prompt(
        "create_mutation",
        {
            "ledger_id": 2000,
            "rows": [
                {"amount": 150.0, "vat_code": "GEEN"},
                {"amount": -50.0, "vat_code": "GEEN"},
            ],
        },
    )
    assert prompt == "e-Boekhouden MCP: Approve POST Mutation of EUR 200.00 on Ledger 2000?"


def test_format_write_prompt_mutation_from_json_rows():
    prompt = format_write_prompt(
        "create_mutation",
        {
            "ledger_id": 3000,
            "rows_json": '[{"amount": 75.25, "vat_code": "GEEN"}]',
        },
    )
    assert prompt == "e-Boekhouden MCP: Approve POST Mutation of EUR 75.25 on Ledger 3000?"


def test_format_write_prompt_mutation_without_amount():
    prompt = format_write_prompt("create_mutation", {"ledger_id": 4000})
    assert prompt == "e-Boekhouden MCP: Approve POST Mutation on Ledger 4000?"


def test_format_write_prompt_deletions():
    assert (
        format_write_prompt("delete_relation", {"id": 12})
        == "e-Boekhouden MCP: Approve DELETE delete_relation for ID 12?"
    )
    assert (
        format_write_prompt("delete_product", {"id": 34})
        == "e-Boekhouden MCP: Approve DELETE delete_product for ID 34?"
    )
    assert (
        format_write_prompt("delete_cost_center", {"id": 56})
        == "e-Boekhouden MCP: Approve DELETE delete_cost_center for ID 56?"
    )
    assert (
        format_write_prompt("delete_member", {"id": 78})
        == "e-Boekhouden MCP: Approve DELETE delete_member for ID 78?"
    )


def test_format_write_prompt_general_tool():
    assert (
        format_write_prompt("create_product", {"name": "Product A"})
        == "e-Boekhouden MCP: Approve write tool 'create_product'?"
    )
    assert (
        format_write_prompt("update_ledger", {"code": "1000"})
        == "e-Boekhouden MCP: Approve write tool 'update_ledger'?"
    )


# ============================================================================
# 2. Windows verification tests
# ============================================================================


def _mock_proc(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


def test_verify_windows_success():
    with patch("subprocess.run", return_value=_mock_proc(stdout="VerificationSucceeded")):
        assert verify_windows("Test prompt") is True

    with patch("subprocess.run", return_value=_mock_proc(stdout="Verified")):
        assert verify_windows("Test prompt") is True


def test_verify_windows_canceled_or_exhausted():
    with patch("subprocess.run", return_value=_mock_proc(stdout="Canceled")), pytest.raises(
        BiometricVerificationDenied, match="canceled or failed"
    ):
        verify_windows("Test prompt")

    with patch("subprocess.run", return_value=_mock_proc(stdout="RetriesExhausted")), pytest.raises(
        BiometricVerificationDenied, match="canceled or failed"
    ):
        verify_windows("Test prompt")


def test_verify_windows_hardware_unavailable():
    for status in ("DeviceNotPresent", "NotConfiguredForUser", "DisabledByPolicy"):
        with patch("subprocess.run", return_value=_mock_proc(stdout=status)), pytest.raises(
            BiometricHardwareUnavailable, match=status
        ):
            verify_windows("Test prompt")

    with patch("subprocess.run", return_value=_mock_proc(stdout="DeviceBusy")), pytest.raises(
        BiometricHardwareUnavailable, match="currently busy"
    ):
        verify_windows("Test prompt")


def test_verify_windows_unexpected_output_fails_closed():
    with patch("subprocess.run", return_value=_mock_proc(stdout="UnknownErrorState")), pytest.raises(
        BiometricVerificationDenied, match="UnknownErrorState"
    ):
        verify_windows("Test prompt")


def test_verify_windows_timeout():
    with patch(
        "subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="powershell", timeout=5)
    ), pytest.raises(BiometricVerificationDenied, match="timed out"):
        verify_windows("Test prompt", timeout_seconds=5)


def test_verify_windows_powershell_error():
    with patch(
        "subprocess.run",
        return_value=_mock_proc(stderr="Script execution blocked", returncode=1),
    ), pytest.raises(BiometricHardwareUnavailable, match="Script execution blocked"):
        verify_windows("Test prompt")


def test_verify_windows_powershell_not_found():
    with patch("subprocess.run", side_effect=FileNotFoundError("No powershell")), pytest.raises(
        BiometricHardwareUnavailable, match="not found"
    ):
        verify_windows("Test prompt")


def test_verify_windows_topmost_presence_invocation():
    from eboekhouden_mcp.biometric_guard import WINDOWS_USER_CONSENT_PS

    with patch(
        "subprocess.run", return_value=_mock_proc(stdout="VerificationSucceeded")
    ) as mock_run:
        assert verify_windows("High presence prompt") is True
        cmd = mock_run.call_args[0][0]
        assert "-Sta" in cmd
        assert "-WindowStyle" in cmd
        assert "Hidden" in cmd
        assert "Win32ConsentHelper" in WINDOWS_USER_CONSENT_PS
        assert "39E050C3-4E74-441A-8DC0-B81104DF949C" in WINDOWS_USER_CONSENT_PS
        assert "Credential Dialog Xaml Host" in WINDOWS_USER_CONSENT_PS
        assert "$backdrop.TopMost = $true" in WINDOWS_USER_CONSENT_PS


# ============================================================================
# 3. macOS verification tests
# ============================================================================


def test_verify_macos_swift_success():
    with patch("shutil.which", return_value="/usr/bin/swift"), patch(
        "subprocess.run", return_value=_mock_proc(returncode=0)
    ):
        assert verify_macos("Approve action") is True


def test_verify_macos_swift_canceled():
    with patch("shutil.which", return_value="/usr/bin/swift"), patch(
        "subprocess.run", return_value=_mock_proc(returncode=1)
    ), pytest.raises(BiometricVerificationDenied, match="canceled or failed"):
        verify_macos("Approve action")


def test_verify_macos_swift_unavailable_falls_back_to_osascript():
    with patch("shutil.which", return_value="/usr/bin/swift"), patch(
        "subprocess.run",
        side_effect=[_mock_proc(returncode=2), _mock_proc(returncode=0)],
    ):
        assert verify_macos("Approve action") is True


def test_verify_macos_without_swift_uses_osascript():
    with patch("shutil.which", return_value=None), patch(
        "os.path.exists", return_value=False
    ), patch("subprocess.run", return_value=_mock_proc(returncode=0)):
        assert verify_macos("Approve action") is True


def test_verify_macos_osascript_user_cancel():
    with patch("shutil.which", return_value=None), patch(
        "os.path.exists", return_value=False
    ), patch("subprocess.run", return_value=_mock_proc(returncode=1)), pytest.raises(
        BiometricVerificationDenied, match="canceled or failed"
    ):
        verify_macos("Approve action")


def test_verify_macos_timeout():
    with patch("shutil.which", return_value="/usr/bin/swift"), patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="swift", timeout=10),
    ), pytest.raises(BiometricVerificationDenied, match="timed out"):
        verify_macos("Approve action", timeout_seconds=10)


def test_verify_macos_missing_osascript():
    with patch("shutil.which", return_value=None), patch(
        "os.path.exists", return_value=False
    ), patch("subprocess.run", side_effect=FileNotFoundError("No osascript")), pytest.raises(
        BiometricHardwareUnavailable, match="osascript not found"
    ):
        verify_macos("Approve action")


# ============================================================================
# 4. Cross-platform dispatch and fail-closed tests
# ============================================================================


def test_verify_user_presence_dispatches_by_platform():
    with patch("sys.platform", "win32"), patch(
        "eboekhouden_mcp.biometric_guard.verify_windows", return_value=True
    ) as mock_win:
        assert verify_user_presence("Win prompt") is True
        mock_win.assert_called_once_with("Win prompt", timeout_seconds=60)

    with patch("sys.platform", "darwin"), patch(
        "eboekhouden_mcp.biometric_guard.verify_macos", return_value=True
    ) as mock_mac:
        assert verify_user_presence("Mac prompt") is True
        mock_mac.assert_called_once_with("Mac prompt", timeout_seconds=60)


def test_verify_user_presence_fails_closed_on_linux_and_unsupported():
    for unsupported in ("linux", "linux2", "freebsd", "sunos5"):
        with patch("sys.platform", unsupported), pytest.raises(
            BiometricHardwareUnavailable, match="not supported or unavailable"
        ):
            verify_user_presence("Prompt")


# ============================================================================
# 5. Keyring token fallback tests
# ============================================================================


def test_keyring_fallback_populates_secret_token():
    with patch("keyring.get_password", return_value="vault-token-12345") as mock_keyring:
        config = MCPConfig(_env_file=None)
        mock_keyring.assert_called_once_with("eboekhouden_mcp", "secret_token")
        assert config.secret_token.get_secret_value() == "vault-token-12345"


def test_keyring_fallback_custom_service_name():
    with patch("keyring.get_password", return_value="custom-vault-token") as mock_keyring:
        config = MCPConfig(keyring_service_name="custom_accounting_app", _env_file=None)
        mock_keyring.assert_called_once_with("custom_accounting_app", "secret_token")
        assert config.secret_token.get_secret_value() == "custom-vault-token"


def test_keyring_fallback_fails_when_token_empty_and_keyring_empty():
    with patch("keyring.get_password", return_value=None), pytest.raises(ValidationError):
        MCPConfig(_env_file=None)


def test_keyring_not_consulted_when_secret_token_explicitly_provided():
    with patch("keyring.get_password") as mock_keyring:
        config = MCPConfig(secret_token="direct-token", _env_file=None)
        assert config.secret_token.get_secret_value() == "direct-token"
        mock_keyring.assert_not_called()


def test_keyring_exception_handled_gracefully():
    with patch("keyring.get_password", side_effect=RuntimeError("Keyring locked")), pytest.raises(
        ValidationError
    ):
        MCPConfig(_env_file=None)


# ============================================================================
# 6. Server integration tests with require_biometric
# ============================================================================


@pytest.mark.asyncio
async def test_server_write_gate_biometric_success():
    config = MCPConfig(
        secret_token="test-token",
        enable_write_tools=True,
        require_biometric=True,
        _env_file=None,
    )
    server = create_server(config)

    with patch(
        "eboekhouden_mcp.server.verify_user_presence", return_value=True
    ) as mock_verify, patch(
        "eboekhouden.services.relation.RelationService.delete"
    ) as mock_delete:
        result = await server.request_handlers[CallToolRequest](
            CallToolRequest(
                params=CallToolRequestParams(
                    name="delete_relation",
                    arguments={"id": 42, "confirm": True},
                )
            )
        )
        assert result.root.isError is not True
        mock_verify.assert_called_once_with(
            "e-Boekhouden MCP: Approve DELETE delete_relation for ID 42?"
        )
        mock_delete.assert_called_once_with(42)


@pytest.mark.asyncio
async def test_server_write_gate_biometric_denied():
    config = MCPConfig(
        secret_token="test-token",
        enable_write_tools=True,
        require_biometric=True,
        _env_file=None,
    )
    server = create_server(config)

    with patch(
        "eboekhouden_mcp.server.verify_user_presence",
        side_effect=BiometricVerificationDenied("User canceled verification."),
    ):
        result = await server.request_handlers[CallToolRequest](
            CallToolRequest(
                params=CallToolRequestParams(
                    name="delete_relation",
                    arguments={"id": 42, "confirm": True},
                )
            )
        )
        assert result.root.isError is True
        text = result.root.content[0].text
        assert "Biometric verification denied" in text
        assert "User canceled verification." in text
        assert "delete_relation" in text


@pytest.mark.asyncio
async def test_server_write_gate_biometric_hardware_unavailable():
    config = MCPConfig(
        secret_token="test-token",
        enable_write_tools=True,
        require_biometric=True,
        _env_file=None,
    )
    server = create_server(config)

    with patch(
        "eboekhouden_mcp.server.verify_user_presence",
        side_effect=BiometricHardwareUnavailable("Device not configured."),
    ):
        result = await server.request_handlers[CallToolRequest](
            CallToolRequest(
                params=CallToolRequestParams(
                    name="delete_relation",
                    arguments={"id": 42, "confirm": True},
                )
            )
        )
        assert result.root.isError is True
        text = result.root.content[0].text
        assert "Biometric verification denied" in text
        assert "Device not configured." in text


@pytest.mark.asyncio
async def test_server_write_gate_bypassed_when_require_biometric_false():
    config = MCPConfig(
        secret_token="test-token",
        enable_write_tools=True,
        require_biometric=False,
        _env_file=None,
    )
    server = create_server(config)

    with patch(
        "eboekhouden_mcp.server.verify_user_presence"
    ) as mock_verify, patch(
        "eboekhouden.services.relation.RelationService.delete"
    ) as mock_delete:
        result = await server.request_handlers[CallToolRequest](
            CallToolRequest(
                params=CallToolRequestParams(
                    name="delete_relation",
                    arguments={"id": 100, "confirm": True},
                )
            )
        )
        assert result.root.isError is not True
        mock_verify.assert_not_called()
        mock_delete.assert_called_once_with(100)


@pytest.mark.asyncio
async def test_read_tools_never_trigger_biometric_check():
    config = MCPConfig(
        secret_token="test-token",
        require_biometric=True,
        _env_file=None,
    )
    server = create_server(config)

    with patch("eboekhouden_mcp.server.verify_user_presence") as mock_verify:
        await server.request_handlers[CallToolRequest](
            CallToolRequest(
                params=CallToolRequestParams(
                    name="list_relations",
                    arguments={"limit": 5},
                )
            )
        )
        # Even if API connection fails, biometric verification was never called
        mock_verify.assert_not_called()


# ============================================================================
# 7. CLI entrypoint tests
# ============================================================================


def test_biometric_guard_cli_success(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["biometric_guard", "--test"])
    with patch(
        "eboekhouden_mcp.biometric_guard.verify_user_presence", return_value=True
    ), patch("sys.exit") as mock_exit:
        biometric_main()
        mock_exit.assert_called_once_with(0)


def test_biometric_guard_cli_denied(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["biometric_guard", "--test"])
    with patch(
        "eboekhouden_mcp.biometric_guard.verify_user_presence",
        side_effect=BiometricVerificationDenied("Canceled"),
    ), patch("sys.exit") as mock_exit:
        biometric_main()
        mock_exit.assert_called_once_with(1)


def test_biometric_guard_cli_unavailable(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["biometric_guard", "--test"])
    with patch(
        "eboekhouden_mcp.biometric_guard.verify_user_presence",
        side_effect=BiometricHardwareUnavailable("No device"),
    ), patch("sys.exit") as mock_exit:
        biometric_main()
        mock_exit.assert_called_once_with(2)


def test_server_cli_test_biometric_flag_success(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["eboekhouden-mcp", "--test-biometric"])
    with patch(
        "eboekhouden_mcp.server.verify_user_presence", return_value=True
    ) as mock_verify:
        # Should return cleanly without raising SystemExit or launching server
        server_main()
        mock_verify.assert_called_once()


def test_server_cli_test_biometric_flag_denied(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["eboekhouden-mcp", "--test-biometric"])
    with patch(
        "eboekhouden_mcp.server.verify_user_presence",
        side_effect=BiometricVerificationDenied("User rejected"),
    ):
        with pytest.raises(SystemExit) as exc_info:
            server_main()
        assert exc_info.value.code == 1


def test_server_cli_test_biometric_flag_unavailable(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["eboekhouden-mcp", "--test-biometric"])
    with patch(
        "eboekhouden_mcp.server.verify_user_presence",
        side_effect=BiometricHardwareUnavailable("Hardware disabled"),
    ):
        with pytest.raises(SystemExit) as exc_info:
            server_main()
        assert exc_info.value.code == 2


def test_server_cli_test_biometric_flag_generic_exception(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["eboekhouden-mcp", "--test-biometric"])
    with patch(
        "eboekhouden_mcp.server.verify_user_presence",
        side_effect=RuntimeError("Unexpected OS crash"),
    ):
        with pytest.raises(SystemExit) as exc_info:
            server_main()
        assert exc_info.value.code == 3


def test_biometric_guard_cli_generic_exception(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["biometric_guard", "--test"])
    with patch(
        "eboekhouden_mcp.biometric_guard.verify_user_presence",
        side_effect=RuntimeError("Unexpected error"),
    ), patch("sys.exit") as mock_exit:
        biometric_main()
        mock_exit.assert_called_once_with(3)


def test_format_write_prompt_resilience_to_malformed_values():
    from eboekhouden_mcp.biometric_guard import _format_currency

    assert _format_currency(None) is None
    assert _format_currency("not_numeric") == "not_numeric"

    # Malformed invoice amount
    res = format_write_prompt("create_invoice", {"amount": "bad_number", "relation_id": 10})
    assert res == "e-Boekhouden MCP: Approve POST Invoice to Relation 10?"

    # Malformed invoice items
    res = format_write_prompt(
        "create_invoice",
        {"relation_id": 10, "items": [{"price_per_unit": "invalid"}]},
    )
    assert res == "e-Boekhouden MCP: Approve POST Invoice to Relation 10?"

    # Malformed mutation amount
    res = format_write_prompt("create_mutation", {"amount": "bad_number", "ledger_id": 20})
    assert res == "e-Boekhouden MCP: Approve POST Mutation on Ledger 20?"

    # Malformed mutation JSON rows
    res = format_write_prompt(
        "create_mutation",
        {"rows_json": "not valid json {", "ledger_id": 30},
    )
    assert res == "e-Boekhouden MCP: Approve POST Mutation on Ledger 30?"

    # Mutation rows as single dict
    res = format_write_prompt(
        "create_mutation",
        {"rows": {"amount": 42.50, "vat_code": "GEEN"}, "ledger_id": 40},
    )
    assert res == "e-Boekhouden MCP: Approve POST Mutation of EUR 42.50 on Ledger 40?"

    # Mutation rows with non-numeric amount
    res = format_write_prompt(
        "create_mutation",
        {"rows": [{"amount": "not_numeric"}], "ledger_id": 50},
    )
    assert res == "e-Boekhouden MCP: Approve POST Mutation on Ledger 50?"


def test_verify_macos_swift_exception_falls_back_to_osascript():
    with patch("shutil.which", return_value="/usr/bin/swift"), patch(
        "subprocess.run",
        side_effect=[
            OSError("Swift crashed"),
            _mock_proc(returncode=0),
        ],
    ):
        assert verify_macos("Approve action") is True


def test_verify_macos_osascript_timeout():
    from eboekhouden_mcp.biometric_guard import _verify_macos_osascript

    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="osascript", timeout=15),
    ), pytest.raises(BiometricVerificationDenied, match="timed out"):
        _verify_macos_osascript("Approve action", timeout_seconds=15)

