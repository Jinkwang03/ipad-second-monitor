"""Windows' Mobile hotspot: let the laptop make its own Wi-Fi so the iPad can connect anywhere.

Windows normally only starts the hotspot to share an internet connection. With no internet
we share from a connection that is always present but never online (WSL's virtual network,
the Bluetooth network adapter, ...), which Windows accepts, so the hotspot also works fully
offline. Uses the WinRT tethering API through Windows PowerShell; nothing to install.
"""
from __future__ import annotations

import base64
import json
import logging
import subprocess
import sys

log = logging.getLogger("ipad-display")

_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  $null = [Windows.Networking.Connectivity.NetworkInformation, Windows.Networking.Connectivity, ContentType=WindowsRuntime]
  $null = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager, Windows.Networking.NetworkOperators, ContentType=WindowsRuntime]
  $null = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult, Windows.Networking.NetworkOperators, ContentType=WindowsRuntime]
  $asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
      $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
      $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
  function Await($op) {
    $task = $asTask.MakeGenericMethod([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult]).Invoke($null, @($op))
    $null = $task.Wait(-1)
    $task.Result
  }
  $Info = [Windows.Networking.Connectivity.NetworkInformation]
  $TM = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]
  $inet = $Info::GetInternetConnectionProfile()
  $source = $inet
  $offline = $false
  if (-not $source -or $ForceOffline) {
    # No internet to share: pick a connection that is always there, so Windows still starts the hotspot.
    $offline = $true
    $source = $Info::GetConnectionProfiles() | Where-Object {
        $_.NetworkAdapter -and "$($_.GetNetworkConnectivityLevel())" -ne 'InternetAccess' -and
        "$($TM::GetTetheringCapabilityFromConnectionProfile($_))" -eq 'Enabled' } |
      Sort-Object { if ($_.ProfileName -match 'vEthernet|WSL|Hyper-V') { 0 } elseif ($_.ProfileName -match 'Bluetooth') { 1 } else { 2 } } |
      Select-Object -First 1
  }
  if (-not $source) { throw 'Windows has no network connection it can start a hotspot from.' }
  $m = $TM::CreateFromConnectionProfile($source)
  if ($Action -eq 'start' -and "$($m.TetheringOperationalState)" -ne 'On') {
    $r = Await ($m.StartTetheringAsync())
    if ("$($r.Status)" -ne 'Success') { throw "Windows could not start the hotspot ($($r.Status)) $($r.AdditionalErrorMessage)" }
  }
  if ($Action -eq 'stop' -and "$($m.TetheringOperationalState)" -ne 'Off') {
    $r = Await ($m.StopTetheringAsync())
    if ("$($r.Status)" -ne 'Success') { throw "Windows could not stop the hotspot ($($r.Status)) $($r.AdditionalErrorMessage)" }
  }
  $cfg = $m.GetCurrentAccessPointConfiguration()
  [pscustomobject]@{ ok = $true; state = "$($m.TetheringOperationalState)"; ssid = $cfg.Ssid;
                     passphrase = $cfg.Passphrase; source = $source.ProfileName; offline = $offline;
                     clients = $m.ClientCount } | ConvertTo-Json -Compress
} catch {
  [pscustomobject]@{ ok = $false; error = $_.Exception.Message } | ConvertTo-Json -Compress
}
"""


def _run(action: str, force_offline: bool = False) -> dict:
    if sys.platform != "win32":
        return {"ok": False, "error": "the hotspot is only available on Windows"}
    script = f"$Action = '{action}'; $ForceOffline = ${'true' if force_offline else 'false'}\n" + _SCRIPT
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    try:
        done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                              capture_output=True, timeout=90, creationflags=0x08000000)   # CREATE_NO_WINDOW
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc)}
    lines = [line for line in done.stdout.decode("utf-8", "replace").splitlines() if line.strip().startswith("{")]
    if not lines:
        return {"ok": False, "error": done.stderr.decode("utf-8", "replace").strip()[-300:] or "no answer from PowerShell"}
    return json.loads(lines[-1])


def status() -> dict:
    return _run("status")


def start(force_offline: bool = False) -> dict:
    """Turn the hotspot on, sharing the internet if there is any (else still start it, offline)."""
    return _run("start", force_offline)


def stop() -> dict:
    return _run("stop")


def wifi_qr_text(ssid: str, passphrase: str) -> str:
    """The text of a "join this Wi-Fi" QR code, which the iPad camera understands."""
    def esc(value: str) -> str:
        for ch in '\\;,:"':
            value = value.replace(ch, "\\" + ch)
        return value
    return f"WIFI:T:WPA;S:{esc(ssid)};P:{esc(passphrase)};;"
