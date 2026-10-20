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
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$null = [Windows.Security.Credentials.UI.UserConsentVerifier, Windows.Security.Credentials.UI, ContentType = WindowsRuntime]
$null = [Windows.Security.Credentials.UI.UserConsentVerificationResult, Windows.Security.Credentials.UI, ContentType = WindowsRuntime]

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class Win32ConsentHelper
{
    [DllImport("user32.dll")]
    public static extern bool SetProcessDPIAware();

    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);

    [DllImport("kernel32.dll")]
    public static extern uint GetCurrentThreadId();

    [DllImport("user32.dll")]
    public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);

    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool BringWindowToTop(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern void SwitchToThisWindow(IntPtr hWnd, bool fAltTab);

    [DllImport("user32.dll")]
    public static extern bool AllowSetForegroundWindow(int dwProcessId);

    [DllImport("user32.dll")]
    public static extern bool SetWindowPos(
        IntPtr hWnd,
        IntPtr hWndInsertAfter,
        int X,
        int Y,
        int cx,
        int cy,
        uint uFlags);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern IntPtr FindWindow(string lpClassName, string lpWindowName);

    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr LoadLibrary(string lpFileName);

    [DllImport("kernel32.dll", CharSet = CharSet.Ansi)]
    private static extern IntPtr GetProcAddress(IntPtr hModule, string procName);

    [DllImport("combase.dll", PreserveSig = true)]
    private static extern int WindowsCreateString(
        [MarshalAs(UnmanagedType.LPWStr)] string sourceString,
        int length,
        out IntPtr hstring);

    [DllImport("combase.dll", PreserveSig = true)]
    private static extern int WindowsDeleteString(IntPtr hstring);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate int DllGetActivationFactoryDelegate(IntPtr activatableClassId, out IntPtr factory);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate int RequestVerificationForWindowAsyncDelegate(
        IntPtr thisPtr,
        IntPtr appWindow,
        IntPtr message,
        ref Guid riid,
        out IntPtr asyncOperation);

    private static readonly IntPtr HWND_TOPMOST = new IntPtr(-1);
    private const uint SWP_NOSIZE = 0x0001;
    private const uint SWP_NOMOVE = 0x0002;
    private const uint SWP_NOACTIVATE = 0x0010;
    private const uint SWP_SHOWWINDOW = 0x0040;
    private const int ASFW_ANY = -1;
    private const byte VK_MENU = 0x12;
    private const uint KEYEVENTF_EXTENDEDKEY = 0x0001;
    private const uint KEYEVENTF_KEYUP = 0x0002;

    public static void ForceForeground(IntPtr hWnd)
    {
        if (hWnd == IntPtr.Zero) return;
        IntPtr fg = GetForegroundWindow();
        uint dummy;
        uint fgThread = fg != IntPtr.Zero ? GetWindowThreadProcessId(fg, out dummy) : 0;
        uint curThread = GetCurrentThreadId();
        bool attached = false;

        try
        {
            keybd_event(VK_MENU, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, UIntPtr.Zero);
            AllowSetForegroundWindow(ASFW_ANY);

            if (fgThread != 0 && fgThread != curThread)
            {
                attached = AttachThreadInput(curThread, fgThread, true);
            }

            SetWindowPos(hWnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW);
            BringWindowToTop(hWnd);
            SetForegroundWindow(hWnd);
            SwitchToThisWindow(hWnd, true);
        }
        finally
        {
            if (attached)
            {
                AttachThreadInput(curThread, fgThread, false);
            }
        }
    }

    public static IntPtr FindCredentialDialog()
    {
        IntPtr hWnd = FindWindow("Credential Dialog Xaml Host", null);
        if (hWnd != IntPtr.Zero && IsWindowVisible(hWnd))
        {
            return hWnd;
        }
        hWnd = FindWindow("#32770", "Windows Security");
        if (hWnd != IntPtr.Zero && IsWindowVisible(hWnd))
        {
            return hWnd;
        }
        return IntPtr.Zero;
    }

    public static bool ElevateCredentialDialog(ref bool alreadyFocused)
    {
        IntPtr credHwnd = FindCredentialDialog();
        if (credHwnd == IntPtr.Zero) return false;

        if (!alreadyFocused)
        {
            ForceForeground(credHwnd);
            alreadyFocused = true;
        }
        else
        {
            SetWindowPos(credHwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW);
        }
        return true;
    }

    public static object RequestForWindow(IntPtr hwnd, string prompt, Guid asyncOpIid)
    {
        IntPtr hMod = LoadLibrary(@"C:\Windows\System32\Windows.Security.Credentials.UI.UserConsentVerifier.dll");
        if (hMod == IntPtr.Zero) return null;

        IntPtr pFunc = GetProcAddress(hMod, "DllGetActivationFactory");
        if (pFunc == IntPtr.Zero) return null;

        var getFactory = (DllGetActivationFactoryDelegate)Marshal.GetDelegateForFunctionPointer(
            pFunc, typeof(DllGetActivationFactoryDelegate));

        IntPtr hClassId = IntPtr.Zero;
        IntPtr hPrompt = IntPtr.Zero;
        IntPtr factory = IntPtr.Zero;
        IntPtr interopPtr = IntPtr.Zero;
        IntPtr asyncOpPtr = IntPtr.Zero;

        try
        {
            string cls = "Windows.Security.Credentials.UI.UserConsentVerifier";
            if (WindowsCreateString(cls, cls.Length, out hClassId) != 0) return null;
            if (getFactory(hClassId, out factory) != 0 || factory == IntPtr.Zero) return null;

            Guid interopIid = new Guid("39E050C3-4E74-441A-8DC0-B81104DF949C");
            if (Marshal.QueryInterface(factory, ref interopIid, out interopPtr) != 0 || interopPtr == IntPtr.Zero)
                return null;

            if (WindowsCreateString(prompt, prompt.Length, out hPrompt) != 0) return null;

            IntPtr vtbl = Marshal.ReadIntPtr(interopPtr);
            IntPtr methodPtr = Marshal.ReadIntPtr(vtbl, 6 * IntPtr.Size);
            var reqFunc = (RequestVerificationForWindowAsyncDelegate)Marshal.GetDelegateForFunctionPointer(
                methodPtr, typeof(RequestVerificationForWindowAsyncDelegate));

            int hr = reqFunc(interopPtr, hwnd, hPrompt, ref asyncOpIid, out asyncOpPtr);
            if (hr != 0 || asyncOpPtr == IntPtr.Zero) return null;

            return Marshal.GetObjectForIUnknown(asyncOpPtr);
        }
        finally
        {
            if (asyncOpPtr != IntPtr.Zero) Marshal.Release(asyncOpPtr);
            if (interopPtr != IntPtr.Zero) Marshal.Release(interopPtr);
            if (factory != IntPtr.Zero) Marshal.Release(factory);
            if (hPrompt != IntPtr.Zero) WindowsDeleteString(hPrompt);
            if (hClassId != IntPtr.Zero) WindowsDeleteString(hClassId);
        }
    }
}
'@

$prompt = $env:MCP_BIOMETRIC_PROMPT
if (-not $prompt) {
    $prompt = "e-Boekhouden MCP Verification"
}

[Win32ConsentHelper]::SetProcessDPIAware() | Out-Null

$backdrop = $null
$ownerForm = $null

try {
    $vScreen = [System.Windows.Forms.SystemInformation]::VirtualScreen
    $activeScreen = [System.Windows.Forms.Screen]::FromPoint([System.Windows.Forms.Cursor]::Position).Bounds

    # UAC-style dimmed full-screen backdrop across all monitors
    $backdrop = New-Object System.Windows.Forms.Form
    $backdrop.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
    $backdrop.StartPosition = [System.Windows.Forms.FormStartPosition]::Manual
    $backdrop.Bounds = $vScreen
    $backdrop.BackColor = [System.Drawing.Color]::Black
    $backdrop.Opacity = 0.55
    $backdrop.TopMost = $true
    $backdrop.ShowInTaskbar = $false
    $backdrop.Text = "e-Boekhouden Security Backdrop"
    $backdrop.Add_MouseDown({
        $cred = [Win32ConsentHelper]::FindCredentialDialog()
        if ($cred -ne [IntPtr]::Zero) {
            [Win32ConsentHelper]::ForceForeground($cred)
        }
    })

    # TopMost owner anchor centered on the active monitor
    $ownerForm = New-Object System.Windows.Forms.Form
    $ownerForm.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
    $ownerForm.StartPosition = [System.Windows.Forms.FormStartPosition]::Manual
    $ownerForm.Size = New-Object System.Drawing.Size(1, 1)
    $ownerForm.Location = New-Object System.Drawing.Point(
        ($activeScreen.X + [int]($activeScreen.Width / 2)),
        ($activeScreen.Y + [int]($activeScreen.Height / 2))
    )
    $ownerForm.BackColor = [System.Drawing.Color]::Black
    $ownerForm.Opacity = 0.01
    $ownerForm.TopMost = $true
    $ownerForm.ShowInTaskbar = $false
    $ownerForm.Owner = $backdrop

    [System.Media.SystemSounds]::Exclamation.Play()

    $backdrop.Show()
    $ownerForm.Show()
    [System.Windows.Forms.Application]::DoEvents()

    [Win32ConsentHelper]::ForceForeground($ownerForm.Handle)
    [System.Windows.Forms.Application]::DoEvents()

    $retType = [Windows.Security.Credentials.UI.UserConsentVerifier].GetMethod("RequestVerificationAsync").ReturnType
    $asyncOpGuid = [System.Runtime.InteropServices.Marshal]::GenerateGuidForType($retType)

    $op = [Win32ConsentHelper]::RequestForWindow($ownerForm.Handle, $prompt, $asyncOpGuid)
    if ($null -eq $op) {
        $op = [Windows.Security.Credentials.UI.UserConsentVerifier]::RequestVerificationAsync($prompt)
    }

    $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and
        $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]
    $asTask = $asTaskGeneric.MakeGenericMethod([Windows.Security.Credentials.UI.UserConsentVerificationResult])
    $task = $asTask.Invoke($null, @($op))

    $focusedDialog = $false
    while (-not $task.IsCompleted) {
        [System.Windows.Forms.Application]::DoEvents()
        [Win32ConsentHelper]::ElevateCredentialDialog([ref]$focusedDialog) | Out-Null
        Start-Sleep -Milliseconds 30
    }

    $res = $task.GetAwaiter().GetResult().ToString()
    if ($res -eq 'Verified') {
        Write-Output 'VerificationSucceeded'
    } else {
        Write-Output $res
    }
} finally {
    if ($null -ne $ownerForm) {
        $ownerForm.Close()
        $ownerForm.Dispose()
    }
    if ($null -ne $backdrop) {
        $backdrop.Close()
        $backdrop.Dispose()
    }
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
            [
                "powershell.exe",
                "-NoProfile",
                "-Sta",
                "-WindowStyle",
                "Hidden",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                WINDOWS_USER_CONSENT_PS,
            ],
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
