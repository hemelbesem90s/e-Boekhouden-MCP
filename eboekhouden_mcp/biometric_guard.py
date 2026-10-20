"""Native biometric verification guard and OS prompt integration.

Provides fail-closed user presence verification via native OS modals:
- Windows Hello (PIN, IR Facial Recognition, Fingerprint) via UserConsentVerifier
- Apple LocalAuthentication / osascript (Touch ID, Face ID, Passcode) on macOS
- Fail-closed on Linux / unsupported platforms.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from typing import Any


class BiometricVerificationDenied(Exception):
    """Raised when biometric verification fails, is canceled, or is rejected."""


class BiometricHardwareUnavailable(Exception):
    """Raised when biometric hardware or OS verification mechanism is unavailable or unsupported."""


def _format_currency(amount: float | str | None) -> str | None:
    """Format a monetary amount to 2 decimal places with comma separation."""
    if amount is None:
        return None
    try:
        val = float(str(amount).replace(",", "").strip())
        return f"{val:,.2f}"
    except (ValueError, TypeError):
        return str(amount)


def format_write_prompt(tool_name: str, arguments: dict[str, Any]) -> str:
    """Format a human-readable prompt for write operations.

    Args:
        tool_name: Name of the write tool being invoked.
        arguments: Tool input parameters.

    Returns:
        A human-readable prompt string describing the action and financial details.
    """
    args = dict(arguments or {})

    # Invoices
    if tool_name == "create_invoice" or (
        "invoice" in tool_name and "create" in tool_name
    ):
        relation_id = args.get("relation_id", "[Relation]")
        amount_val: float | None = None
        if "amount" in args:
            try:
                amount_val = float(str(args["amount"]).replace(",", "").strip())
            except (ValueError, TypeError):
                amount_val = None
        elif "items" in args and isinstance(args["items"], list):
            try:
                amount_val = sum(
                    float(item.get("price_per_unit", 0.0) or 0.0)
                    * float(item.get("quantity", 1.0) or 1.0)
                    for item in args["items"]
                    if isinstance(item, dict)
                )
            except (ValueError, TypeError):
                amount_val = None

        if amount_val is not None:
            return f"e-Boekhouden MCP: Approve POST Invoice for EUR {_format_currency(amount_val)} to Relation {relation_id}?"
        return f"e-Boekhouden MCP: Approve POST Invoice to Relation {relation_id}?"

    # Mutations (Journal entries)
    if tool_name == "create_mutation" or (
        "mutation" in tool_name and "create" in tool_name
    ):
        ledger_id = args.get("ledger_id", "Unknown")
        amount_val = None
        if "amount" in args:
            try:
                amount_val = abs(float(str(args["amount"]).replace(",", "").strip()))
            except (ValueError, TypeError):
                amount_val = None
        elif "rows" in args or "rows_json" in args:
            rows = args.get("rows") or args.get("rows_json")
            if isinstance(rows, str):
                try:
                    rows = json.loads(rows)
                except (json.JSONDecodeError, ValueError):
                    rows = None
            if isinstance(rows, dict):
                rows = [rows]
            if isinstance(rows, list):
                try:
                    amount_val = sum(
                        abs(float(row.get("amount", 0.0) or 0.0))
                        for row in rows
                        if isinstance(row, dict) and "amount" in row
                    )
                except (ValueError, TypeError):
                    amount_val = None

        if amount_val is not None:
            return f"e-Boekhouden MCP: Approve POST Mutation of EUR {_format_currency(amount_val)} on Ledger {ledger_id}?"
        return f"e-Boekhouden MCP: Approve POST Mutation on Ledger {ledger_id}?"

    # Deletions
    if tool_name.startswith("delete_"):
        entity_id = args.get(
            "id",
            args.get(
                "relation_id",
                args.get(
                    "product_id",
                    args.get(
                        "cost_center_id",
                        args.get("member_id", args.get("ledger_id", "Unknown")),
                    ),
                ),
            ),
        )
        return f"e-Boekhouden MCP: Approve DELETE {tool_name} for ID {entity_id}?"

    # General fallback
    return f"e-Boekhouden MCP: Approve write tool '{tool_name}'?"


WINDOWS_USER_CONSENT_PS = r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Security.Credentials.UI.UserConsentVerifier, Windows.Security.Credentials.UI, ContentType = WindowsRuntime]
$null = [Windows.Security.Credentials.UI.UserConsentVerificationResult, Windows.Security.Credentials.UI, ContentType = WindowsRuntime]

$prompt = $env:MCP_BIOMETRIC_PROMPT
if (-not $prompt) {
    $prompt = "e-Boekhouden MCP Verification"
}

$op = [Windows.Security.Credentials.UI.UserConsentVerifier]::RequestVerificationAsync($prompt)
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and
    $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]
$asTask = $asTaskGeneric.MakeGenericMethod([Windows.Security.Credentials.UI.UserConsentVerificationResult])
$task = $asTask.Invoke($null, @($op))
$task.Wait()
$res = $task.Result.ToString()
if ($res -eq 'Verified') {
    Write-Output 'VerificationSucceeded'
} else {
    Write-Output $res
}
"""


def verify_windows(prompt_message: str, timeout_seconds: int = 60) -> bool:
    """Verify presence on Windows via Windows Hello UserConsentVerifier.

    Args:
        prompt_message: The text prompt to display to the user.
        timeout_seconds: Subprocess timeout in seconds.

    Returns:
        True if verification succeeded.

    Raises:
        BiometricVerificationDenied: If user cancels or fails verification.
        BiometricHardwareUnavailable: If biometric verification is not configured or available.
    """
    env = os.environ.copy()
    env["MCP_BIOMETRIC_PROMPT"] = prompt_message

    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", WINDOWS_USER_CONSENT_PS],
            text=True,
            capture_output=True,
            check=False,
            env=env,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise BiometricVerificationDenied(
            f"Biometric verification timed out after {timeout_seconds} seconds."
        ) from exc
    except FileNotFoundError as exc:
        raise BiometricHardwareUnavailable(
            f"powershell.exe not found on system: {exc}"
        ) from exc

    if proc.returncode != 0:
        err = proc.stderr.strip() or f"Process exited with code {proc.returncode}"
        raise BiometricHardwareUnavailable(
            f"Windows UserConsentVerifier invocation failed: {err}"
        )

    output = proc.stdout.strip()
    if output in ("VerificationSucceeded", "Verified"):
        return True
    if output in ("Canceled", "RetriesExhausted"):
        raise BiometricVerificationDenied("User canceled or failed biometric verification.")
    if output in ("DeviceNotPresent", "NotConfiguredForUser", "DisabledByPolicy"):
        raise BiometricHardwareUnavailable(
            f"Biometric verification unavailable on this system: {output}."
        )
    if output == "DeviceBusy":
        raise BiometricHardwareUnavailable("Biometric device is currently busy.")

    raise BiometricVerificationDenied(f"Biometric verification failed: {output}.")


MACOS_SWIFT_SCRIPT = """
import Foundation
import LocalAuthentication

let context = LAContext()
var error: NSError?
let reason = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "e-Boekhouden MCP Verification"

if context.canEvaluatePolicy(.deviceOwnerAuthentication, error: &error) {
    let semaphore = DispatchSemaphore(value: 0)
    var success = false
    context.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: reason) { (succ, err) in
        success = succ
        semaphore.signal()
    }
    _ = semaphore.wait(timeout: .distantFuture)
    exit(success ? 0 : 1)
} else {
    exit(2)
}
"""


def _verify_macos_osascript(prompt_message: str, timeout_seconds: int = 60) -> bool:
    """Prompt the user on macOS via AppleScript dialog."""
    escaped = prompt_message.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        f'display dialog "{escaped}" '
        'with title "e-Boekhouden MCP" '
        'buttons {"Cancel", "Approve"} default button "Approve" '
        'with icon caution'
    )
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds,
        )
        if proc.returncode == 0:
            return True
        raise BiometricVerificationDenied("User canceled or failed biometric verification.")
    except subprocess.TimeoutExpired as exc:
        raise BiometricVerificationDenied(
            f"Biometric verification timed out after {timeout_seconds} seconds."
        ) from exc
    except FileNotFoundError as exc:
        raise BiometricHardwareUnavailable(f"osascript not found on system: {exc}") from exc


def verify_macos(prompt_message: str, timeout_seconds: int = 60) -> bool:
    """Verify presence on macOS via Swift LocalAuthentication or osascript fallback.

    Args:
        prompt_message: The text prompt to display to the user.
        timeout_seconds: Timeout in seconds.

    Returns:
        True if verification succeeded.

    Raises:
        BiometricVerificationDenied: If user cancels or fails verification.
        BiometricHardwareUnavailable: If verification mechanisms are unavailable.
    """
    swift_cmd = shutil.which("swift") or (
        "/usr/bin/swift" if os.path.exists("/usr/bin/swift") else None
    )
    if swift_cmd:
        try:
            proc = subprocess.run(
                [swift_cmd, "-e", MACOS_SWIFT_SCRIPT, prompt_message],
                capture_output=True,
                check=False,
                text=True,
                timeout=timeout_seconds,
            )
            if proc.returncode == 0:
                return True
            if proc.returncode == 1:
                raise BiometricVerificationDenied("User canceled or failed biometric verification.")
            if proc.returncode == 2:
                # Device cannot evaluate policy, fall back to dialog
                return _verify_macos_osascript(prompt_message, timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            raise BiometricVerificationDenied(
                f"Biometric verification timed out after {timeout_seconds} seconds."
            ) from exc
        except (BiometricVerificationDenied, BiometricHardwareUnavailable):
            raise
        except OSError:
            # Fall back to osascript dialog on unexpected swift invocation error
            pass

    return _verify_macos_osascript(prompt_message, timeout_seconds)


def verify_user_presence(prompt_message: str, timeout_seconds: int = 60) -> bool:
    """Perform native fail-closed biometric verification for the current OS.

    Args:
        prompt_message: Explanation message shown in the verification modal.
        timeout_seconds: Maximum time to wait for user interaction.

    Returns:
        True if verification succeeded.

    Raises:
        BiometricVerificationDenied: If user denies, cancels, or times out.
        BiometricHardwareUnavailable: If OS does not support or has no biometric hardware.
    """
    if sys.platform == "win32":
        return verify_windows(prompt_message, timeout_seconds=timeout_seconds)
    if sys.platform == "darwin":
        return verify_macos(prompt_message, timeout_seconds=timeout_seconds)

    raise BiometricHardwareUnavailable(
        f"Biometric verification is not supported or unavailable on {sys.platform}."
    )


def main() -> None:
    """CLI test entrypoint."""
    parser = argparse.ArgumentParser(
        description="e-Boekhouden MCP Native Biometric Verification Guard"
    )
    parser.add_argument(
        "--test",
        "--test-biometric",
        action="store_true",
        dest="test",
        help="Trigger a test prompt to verify OS biometric setup",
    )
    parser.add_argument(
        "--message",
        "-m",
        default="e-Boekhouden MCP: Biometric Verification Test",
        help="Prompt message for verification",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Verification timeout in seconds (default: 60)",
    )
    args = parser.parse_args()

    print(f"Testing native biometric verification on {sys.platform}...")
    print(f"Prompt: {args.message}")
    try:
        verify_user_presence(args.message, timeout_seconds=args.timeout)
        print("Biometric verification succeeded.")
        sys.exit(0)
    except BiometricVerificationDenied as exc:
        print(f"Biometric verification denied: {exc}", file=sys.stderr)
        sys.exit(1)
    except BiometricHardwareUnavailable as exc:
        print(f"Biometric hardware unavailable: {exc}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        print(f"Biometric verification error: {exc}", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()
