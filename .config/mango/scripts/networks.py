#!/usr/bin/env python3
#
# Network Overview — a full-featured NetworkManager GUI driven entirely by nmcli.
# Production-ready GTK3/PyGObject implementation, UX-polished in the style of
# the MangoWM Grid Overview (grid.py).
#
# Runtime dependencies:
#   - Python 3.9+
#   - GTK3
#   - PyGObject
#   - NetworkManager with `nmcli` available in PATH
#   - A valid icon theme (Adwaita or any desktop icon theme)
#
# Usage:
#   python3 networks.py
#
# Features:
#   - Device overview (type, state, active connection) with Wi-Fi radio toggle.
#   - Wi-Fi scan list: SSID, signal bars, security, band/frequency, BSSID, rate.
#   - Connect to open / WPA-PSK / WPA3-SAE / WPA-Enterprise / WEP networks.
#   - Password dialog with show/hide and "store in profile" option.
#   - Hidden network support (manual SSID entry).
#   - Connection profiles: activate, deactivate, edit, clone, delete,
#     autoconnect toggle, VPN import/export, reload from disk.
#   - Create new profiles: Wi-Fi, Ethernet, WireGuard (nmcli property syntax).
#   - Search + type filter for profiles; active profiles highlighted.
#   - Real-time updates via `nmcli monitor` stream + periodic polling.
#   - Secrets are passed via 0600 temporary files (passwd-file) and are never
#     logged; stored secrets are shown only on explicit user request
#     (--show-secrets pattern).
#
# Exit behavior:
#   - ESC closes the window.
#   - Clicking the background does nothing.
#   - A second launched instance asks the first one to present itself.
#
# UX keys:
#   - s: Trigger a Wi-Fi rescan
#   - F5: Force a state refresh
#   - ESC: Close
#
# =============================================================================
from __future__ import annotations

import errno
import fcntl
import gi
import logging
import os
import queue
import re
import signal
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Pango

logging.basicConfig(
    level=logging.INFO,
    format="[NetworkOverview] %(levelname)s: %(message)s",
)
logger = logging.getLogger("NetworkOverview")

# =============================================================================
# Constants
# =============================================================================
POLL_INTERVAL_MS = 4000
POLL_INTERVAL_NM_DOWN_MS = 7000
SYNC_DEBOUNCE_MS = 160
SYNC_AFTER_ACTION_MS = 260
SCAN_SETTLE_MS = 1600
MONITOR_RESTART_DELAY_SECONDS = 2.0

NMCLI_TIMEOUT = 6.0
NMCLI_SCAN_TIMEOUT = 12.0
NMCLI_UP_TIMEOUT = 28.0
NMCLI_DOWN_TIMEOUT = 12.0

STATUS_FLASH_MS = 4200
MAX_CACHED_ICONS = 512

APP_NAME = "network-overview"
WINDOW_TITLE = "Network Overview"
WINDOW_ROLE = "network-overview"
APP_NAME_LOWER = APP_NAME.lower()
WINDOW_TITLE_LOWER = WINDOW_TITLE.lower()
OWN_PID = os.getpid()

LOCK_FILE_NAME = "network-overview.lock"
SOCKET_FILE_NAME = "network-overview.sock"
IPC_MESSAGE_SHOW = "show"
IPC_MESSAGE_QUIT = "quit"

FADE_IN_STEP_MS = 16
FADE_IN_STEPS = 8

# Matches the UUID printed by `nmcli connection add`:
#   Connection 'Name' (12345678-1234-1234-1234-123456789012) successfully added.
_UUID_PATTERN = re.compile(
    r"\(([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\)"
)

# Security token -> nmcli key management mapping.
SECURITY_COMBO_LABELS = [
    "Open",
    "WPA / WPA2 Personal",
    "WPA3 Personal (SAE)",
    "WPA Enterprise (802.1X)",
    "WEP (legacy)",
]
SECURITY_COMBO_KM = ["", "wpa-psk", "sae", "wpa-eap", "wep"]

VPN_IMPORT_TYPES = [
    "openvpn",
    "pptp",
    "l2tp",
    "vpnc",
    "strongswan",
    "ikev2",
    "wireguard",
]

TYPE_FILTERS: Dict[str, Optional[Tuple[str, ...]]] = {
    "All": None,
    "Wi-Fi": ("wifi", "wireless", "olpc"),
    "Ethernet": ("ethernet", "802-3", "adsl", "pppoe"),
    "VPN": ("vpn",),
    "WireGuard": ("wireguard",),
    "Bridge": ("bridge",),
    "VLAN": ("vlan",),
    "Bond / Team": ("bond", "team"),
    "Cellular": ("gsm", "cdma"),
}


def display_type(ctype: str) -> str:
    """Human-friendly connection type name."""
    t = (ctype or "").lower()
    if "wireless" in t or t == "wifi":
        return "Wi-Fi"
    if "ethernet" in t or "802-3" in t:
        return "Ethernet"
    if t == "vpn":
        return "VPN"
    if "wireguard" in t:
        return "WireGuard"
    if "bridge" in t:
        return "Bridge"
    if "vlan" in t:
        return "VLAN"
    if "bond" in t:
        return "Bond"
    if "team" in t:
        return "Team"
    if t in ("gsm", "cdma"):
        return "Cellular"
    if "bluetooth" in t:
        return "Bluetooth"
    return ctype or "Unknown"


def key_mgmt_for_security(security: str) -> str:
    """Map the nmcli SECURITY column to an nmcli key-mgmt value."""
    raw = security or ""
    up = raw.upper()
    if "802.1X" in raw or "8021X" in up or "EAP" in up:
        return "wpa-eap"
    if "WEP" in up:
        return "wep"
    if "WPA3" in up and "WPA2" not in up and "WPA1" not in up:
        return "sae"
    if "WPA" in up:
        return "wpa-psk"
    if "OWE" in up:
        return "owe"
    return ""


# =============================================================================
# CSS
# =============================================================================
CSS_TEXT = """
window {
  background-color: rgba(25, 23, 36, 0.95);
}
label {
  color: #e0def4;
}
.bg-overlay {
  background-color: transparent;
}
.outer-box {
  background-color: transparent;
  padding: 22px;
}
.header {
  background-color: rgba(38, 35, 58, 0.55);
  border: 1px solid rgba(110, 106, 134, 0.35);
  border-radius: 16px;
  margin-bottom: 14px;
  /* NOTE: no CSS padding here. The header is a Gtk.EventBox, which does
     not apply CSS padding to its child layout. The inner box carries
     real margins so text and buttons never touch the border. */
}
.title-label {
  color: #e0def4;
  font-size: 18px;
  font-weight: bold;
}
.subtitle-label {
  color: #908caa;
  font-size: 12px;
}
.section-label {
  color: #ebbcba;
  font-size: 14px;
  font-weight: bold;
  margin: 12px 2px 6px 2px;
}
.content-box {
  background-color: transparent;
}
.row-card {
  background-color: rgba(38, 35, 58, 0.62);
  border: 1px solid #6e6a86;
  border-radius: 14px;
  padding: 10px 14px;
  margin-bottom: 7px;
  transition: background-color 140ms ease, border-color 140ms ease,
    box-shadow 140ms ease;
}
.row-card:hover {
  border-color: #ebbcba;
  background-color: rgba(235, 188, 186, 0.10);
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.25);
}
.row-active {
  border-color: #c4a7e7;
  background-color: rgba(196, 167, 231, 0.14);
  box-shadow: 0 0 0 1px rgba(196, 167, 231, 0.45),
    0 0 18px rgba(196, 167, 231, 0.28),
    0 6px 18px rgba(0, 0, 0, 0.22);
}
.row-title {
  color: #e0def4;
  font-size: 14px;
  font-weight: 600;
}
.sub-label {
  color: #908caa;
  font-size: 12px;
}
.empty-label {
  color: #6e6a86;
  font-style: italic;
  font-size: 12px;
  padding: 6px 10px 10px 10px;
}
.badge {
  border-radius: 999px;
  padding: 2px 10px;
  font-size: 11px;
  font-weight: bold;
}
.badge-connected {
  background-color: rgba(196, 167, 231, 0.22);
  color: #c4a7e7;
  border: 1px solid rgba(196, 167, 231, 0.55);
}
.badge-idle {
  background-color: rgba(110, 106, 134, 0.25);
  color: #908caa;
  border: 1px solid rgba(110, 106, 134, 0.45);
}
button {
  background-image: none;
  background-color: rgba(110, 106, 134, 0.30);
  color: #e0def4;
  border: 1px solid rgba(110, 106, 134, 0.5);
  border-radius: 9px;
  padding: 5px 12px;
  transition: background-color 110ms ease, border-color 110ms ease,
    color 110ms ease;
}
button:hover {
  background-color: rgba(235, 188, 186, 0.28);
  border-color: #ebbcba;
}
button:active, button:checked {
  background-color: rgba(196, 167, 231, 0.35);
}
.btn-primary {
  background-color: rgba(196, 167, 231, 0.25);
  border-color: rgba(196, 167, 231, 0.6);
}
.btn-primary:hover {
  background-color: #c4a7e7;
  color: #191724;
}
.btn-danger:hover {
  background-color: #eb6f92;
  border-color: #eb6f92;
  color: #191724;
}
entry {
  background-color: rgba(25, 23, 36, 0.85);
  color: #e0def4;
  border: 1px solid #6e6a86;
  border-radius: 8px;
  padding: 5px 8px;
  caret-color: #ebbcba;
}
entry:focus {
  border-color: #c4a7e7;
}
entry selection {
  background-color: rgba(196, 167, 231, 0.4);
}
combobox {
  color: #e0def4;
}
switch {
  background-color: rgba(110, 106, 134, 0.40);
  border: 1px solid rgba(110, 106, 134, 0.6);
  border-radius: 999px;
  min-width: 44px;
  min-height: 24px;
}
switch:checked {
  background-color: rgba(196, 167, 231, 0.55);
  border-color: #c4a7e7;
}
switch slider {
  background-color: #e0def4;
  border-radius: 999px;
  min-width: 18px;
  min-height: 18px;
  margin: 2px;
}
menu, .menu {
  background-color: #26233a;
  border: 1px solid #6e6a86;
  border-radius: 10px;
  padding: 4px;
}
menuitem {
  color: #e0def4;
  border-radius: 6px;
  padding: 4px 10px;
}
menuitem:hover {
  background-color: rgba(196, 167, 231, 0.25);
}
menuitem.menu-danger label {
  color: #eb6f92;
}
menu separator {
  background-color: rgba(110, 106, 134, 0.4);
  min-height: 1px;
  margin: 4px 0;
}
tooltip {
  background-color: #26233a;
  color: #e0def4;
  border: 1px solid #6e6a86;
  border-radius: 8px;
  padding: 4px 8px;
}
scrollbar slider {
  background-color: rgba(110, 106, 134, 0.5);
  border-radius: 999px;
  min-width: 8px;
  min-height: 8px;
}
scrollbar slider:hover {
  background-color: rgba(196, 167, 231, 0.6);
}
.hint-label {
  color: #908caa;
  font-size: 12px;
  margin-top: 12px;
}
.hint-error {
  color: #eb6f92;
}
.hint-ok {
  color: #9ccfd8;
}
"""

# =============================================================================
# Utilities
# =============================================================================
_LOG_THROTTLE: Dict[str, float] = {}
_LOG_LOCK = threading.Lock()


def log_throttled(level: int, key: str, message: str, *args: Any) -> None:
    """Log at most once per 5 seconds for the same key."""
    now = time.monotonic()
    with _LOG_LOCK:
        if len(_LOG_THROTTLE) > 1024:
            _LOG_THROTTLE.clear()
        last = _LOG_THROTTLE.get(key, 0.0)
        if now - last < 5.0:
            return
        _LOG_THROTTLE[key] = now
    logger.log(level, message, *args)


def split_terse(line: str) -> List[str]:
    """
    Split one line of `nmcli --terse` output on unescaped colons.
    nmcli escapes ':' as '\\:' and '\\' as '\\\\' inside values.
    """
    parts: List[str] = []
    current: List[str] = []
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == "\\" and i + 1 < n and line[i + 1] in (":", "\\"):
            current.append(line[i + 1])
            i += 2
            continue
        if ch == ":":
            parts.append("".join(current))
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    parts.append("".join(current))
    return parts


def get_runtime_dir() -> str:
    """
    Return a per-user runtime directory for lock/socket files.
    Prefer XDG_RUNTIME_DIR. Fall back to a private directory under /tmp.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        try:
            os.makedirs(runtime, mode=0o700, exist_ok=True)
            return runtime
        except OSError as exc:
            logger.debug("Cannot use XDG_RUNTIME_DIR %s: %s", runtime, exc)
    uid = os.getuid() if hasattr(os, "getuid") else 0
    fallback = os.path.join(tempfile.gettempdir(), f"network-overview-{uid}")
    os.makedirs(fallback, mode=0o700, exist_ok=True)
    return fallback


def is_instance_or_ancestor(widget: Optional[Gtk.Widget], cls: Any) -> bool:
    """Return True if widget is an instance of cls or a descendant of one."""
    current = widget
    while current is not None:
        if isinstance(current, cls):
            return True
        current = current.get_parent()
    return False


# =============================================================================
# Models
# =============================================================================
@dataclass(frozen=True)
class DeviceState:
    device: str
    dtype: str
    state: str
    connection: str


@dataclass(frozen=True)
class ConnectionProfile:
    name: str
    uuid: str
    ctype: str
    device: str
    autoconnect: bool
    ssid: str
    active: bool = False
    active_device: str = ""
    ip_summary: str = ""


@dataclass(frozen=True)
class AccessPoint:
    key: str
    ssid: str
    bssid: str
    signal: int
    freq: str
    rate: str
    security: str
    in_use: bool


@dataclass(frozen=True)
class GeneralState:
    state: str
    connectivity: str
    wifi_radio: str
    wwan_radio: str


@dataclass
class OverviewState:
    nm_available: bool
    general: Optional[GeneralState]
    devices: List[DeviceState]
    profiles: List[ConnectionProfile]
    access_points: List[AccessPoint]
    wifi_enabled: bool


# =============================================================================
# nmcli backend
# =============================================================================
class NmcliBackend:
    """
    All NetworkManager interaction goes through the `nmcli` binary.
    Every method is intended to be executed from a worker thread, never
    directly from the GTK main thread.
    """

    # -------------------------------------------------------------------------
    # Low-level subprocess helpers
    # -------------------------------------------------------------------------
    @staticmethod
    def _nmcli_env() -> Dict[str, str]:
        """
        Force the C locale so nmcli output is never localized.
        This keeps terse field values ("enabled"/"disabled"), error strings
        and the `connection add` confirmation line reliably parseable.
        """
        env = dict(os.environ)
        env["LC_ALL"] = "C"
        env["LANG"] = "C"
        env.pop("LANGUAGE", None)
        return env

    @staticmethod
    def _run_full(
        args: List[str],
        timeout: float = NMCLI_TIMEOUT,
        log_errors: bool = True,
    ) -> Tuple[bool, str, str]:
        """Run `nmcli <args>` and return (success, stdout, error_message)."""
        cmd_str = " ".join(args)
        try:
            proc = subprocess.run(
                ["nmcli"] + args,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=NmcliBackend._nmcli_env(),
            )
        except FileNotFoundError:
            if log_errors:
                log_throttled(
                    logging.ERROR,
                    "nmcli-missing",
                    "Cannot find `nmcli`. Make sure NetworkManager is installed "
                    "and `nmcli` is in PATH.",
                )
            return False, "", "`nmcli` not found in PATH"
        except subprocess.TimeoutExpired:
            if log_errors:
                log_throttled(
                    logging.WARNING,
                    f"timeout:nmcli {cmd_str}",
                    "Timeout while running `nmcli %s` after %.2fs.",
                    cmd_str,
                    timeout,
                )
            return False, "", f"nmcli timed out after {timeout:.0f}s"
        except Exception as exc:
            if log_errors:
                log_throttled(
                    logging.ERROR,
                    f"exec:nmcli {cmd_str}",
                    "Unexpected error while running `nmcli %s`: %s",
                    cmd_str,
                    exc,
                )
            return False, "", str(exc)

        stdout = proc.stdout or ""
        if proc.returncode != 0:
            lines = (proc.stderr or stdout).strip().splitlines()
            msg = lines[0].strip() if lines else f"nmcli exited with code {proc.returncode}"
            if msg.lower().startswith("error:"):
                msg = msg[6:].strip()
            if log_errors:
                log_throttled(
                    logging.WARNING,
                    f"fail:nmcli {cmd_str}:{msg[:100]}",
                    "`nmcli %s` failed: %s",
                    cmd_str,
                    msg,
                )
            return False, stdout, msg
        return True, stdout, ""

    @staticmethod
    def _run(
        args: List[str],
        timeout: float = NMCLI_TIMEOUT,
        log_errors: bool = True,
    ) -> Optional[str]:
        """Run `nmcli <args>` and return stdout, or None on any error."""
        ok, out, _err = NmcliBackend._run_full(
            args, timeout=timeout, log_errors=log_errors
        )
        return out if ok else None

    @staticmethod
    def _run_result(
        args: List[str],
        timeout: float = NMCLI_TIMEOUT,
        log_errors: bool = True,
    ) -> Tuple[bool, str]:
        """Run `nmcli <args>` and return (success, error_message)."""
        ok, _out, err = NmcliBackend._run_full(
            args, timeout=timeout, log_errors=log_errors
        )
        return ok, err

    @staticmethod
    def _terse(
        args: List[str],
        timeout: float = NMCLI_TIMEOUT,
        log_errors: bool = True,
    ) -> Optional[List[List[str]]]:
        """Run `nmcli -t <args>` and parse terse rows."""
        out = NmcliBackend._run(["-t"] + args, timeout=timeout, log_errors=log_errors)
        if out is None:
            return None
        rows: List[List[str]] = []
        for line in out.splitlines():
            if not line.strip():
                continue
            rows.append(split_terse(line))
        return rows

    @staticmethod
    def _parse_added_uuid(output: str) -> Optional[str]:
        """
        Extract the new profile UUID from `nmcli connection add` output:
            Connection 'Name' (12345678-...) successfully added.
        This is the authoritative source for the UUID of a freshly created
        profile; re-querying by name is unreliable because nmcli renames
        profiles on name collisions (e.g. "MyWifi 1").
        """
        match = _UUID_PATTERN.search(output or "")
        return match.group(1) if match else None

    @staticmethod
    def _write_passwd_file(entries: List[str]) -> str:
        """
        Write a 0600 temporary passwd-file for `nmcli ... passwd-file`.
        Each entry has the form `setting.property:value`.
        The caller must unlink the returned path.
        """
        fd, path = tempfile.mkstemp(prefix="nm-passwd-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(entries) + "\n")
        os.chmod(path, 0o600)
        return path

    # -------------------------------------------------------------------------
    # State queries
    # -------------------------------------------------------------------------
    @staticmethod
    def get_general() -> Optional[GeneralState]:
        rows = NmcliBackend._terse(["-f", "STATE,CONNECTIVITY,WIFI,WWAN", "general"])
        if not rows:
            return None
        r = rows[0] + ["", "", "", ""]
        return GeneralState(
            state=r[0],
            connectivity=r[1],
            wifi_radio=r[2],
            wwan_radio=r[3],
        )

    @staticmethod
    def get_devices() -> List[DeviceState]:
        rows = NmcliBackend._terse(["-f", "DEVICE,TYPE,STATE,CONNECTION", "device"])
        if rows is None:
            return []
        devices: List[DeviceState] = []
        for r in rows:
            r = r + ["", "", "", ""]
            devices.append(
                DeviceState(
                    device=r[0],
                    dtype=r[1],
                    state=r[2],
                    connection="" if r[3] == "--" else r[3],
                )
            )
        return devices

    @staticmethod
    def get_profiles() -> List[ConnectionProfile]:
        rows = NmcliBackend._terse(
            [
                "-f",
                "NAME,UUID,TYPE,DEVICE,connection.autoconnect,802-11-wireless.ssid",
                "connection",
            ]
        )
        if rows is None:
            return []
        profiles: List[ConnectionProfile] = []
        for r in rows:
            r = r + ["", "", "", "", "", ""]
            profiles.append(
                ConnectionProfile(
                    name=r[0],
                    uuid=r[1],
                    ctype=r[2],
                    device="" if r[3] == "--" else r[3],
                    autoconnect=r[4].strip().lower() == "yes",
                    ssid=r[5],
                )
            )
        return profiles

    @staticmethod
    def get_active_connections() -> Dict[str, Tuple[str, str]]:
        """Return {uuid: (name, device)} for active connections."""
        rows = NmcliBackend._terse(
            ["-f", "UUID,NAME,TYPE,DEVICE", "connection", "show", "--active"]
        )
        result: Dict[str, Tuple[str, str]] = {}
        if rows is None:
            return result
        for r in rows:
            r = r + ["", "", "", ""]
            if r[0]:
                result[r[0]] = (r[1], "" if r[3] == "--" else r[3])
        return result

    @staticmethod
    def get_ip_summary(uuid: str) -> str:
        """Return a short 'IPv4 via gateway [• IPv6]' summary for a connection."""
        rows = NmcliBackend._terse(
            ["-f", "IP4.ADDRESS,IP4.GATEWAY,IP6.ADDRESS", "connection", "show", "uuid", uuid],
            log_errors=False,
        )
        if not rows:
            return ""
        ip4 = gw = ip6 = ""
        for r in rows:
            r = r + ["", "", ""]
            if not ip4 and r[0]:
                ip4 = r[0]
            if not gw and r[1]:
                gw = r[1]
            if not ip6 and r[2]:
                ip6 = r[2]
        parts = []
        if ip4:
            parts.append(ip4 + (f" via {gw}" if gw else ""))
        if ip6:
            parts.append("IPv6")
        return " • ".join(parts)

    @staticmethod
    def get_access_points() -> List[AccessPoint]:
        rows = NmcliBackend._terse(
            [
                "-f",
                "IN-USE,BSSID,SSID,MODE,CHAN,FREQ,RATE,SIGNAL,BARS,SECURITY",
                "device",
                "wifi",
                "list",
            ],
            timeout=NMCLI_SCAN_TIMEOUT,
            log_errors=False,
        )
        if rows is None:
            return []
        best: Dict[str, AccessPoint] = {}
        for r in rows:
            r = r + [""] * 10
            in_use = r[0].strip() == "*"
            bssid = r[1].strip()
            ssid = r[2].strip()
            if ssid in ("--", "\\--"):
                ssid = ""
            try:
                signal = int(r[7].strip() or 0)
            except ValueError:
                signal = 0
            security = "" if r[9].strip() == "--" else r[9].strip()
            key = ssid if ssid else f"hidden:{bssid}"
            existing = best.get(key)
            if existing is None or signal > existing.signal:
                best[key] = AccessPoint(
                    key=key,
                    ssid=ssid,
                    bssid=bssid,
                    signal=signal,
                    freq=r[5].strip(),
                    rate=r[6].strip(),
                    security=security or (existing.security if existing else ""),
                    in_use=in_use or (existing.in_use if existing else False),
                )
            elif in_use and not existing.in_use:
                best[key] = replace(existing, in_use=True)
        return list(best.values())

    @staticmethod
    def wifi_radio_on() -> bool:
        rows = NmcliBackend._terse(["-f", "WIFI", "radio"], log_errors=False)
        if not rows:
            return False
        return (rows[0][0] if rows[0] else "").strip().lower() == "enabled"

    @staticmethod
    def get_profile_details(uuid: str) -> Optional[Dict[str, Any]]:
        """Fetch editable fields of one profile."""
        rows = NmcliBackend._terse(
            [
                "-f",
                "connection.id,connection.autoconnect,connection.interface-name,"
                "ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns,"
                "802-11-wireless.ssid,TYPE",
                "connection",
                "show",
                "uuid",
                uuid,
            ],
            log_errors=False,
        )
        if rows is None:
            return None
        cols: Dict[int, List[str]] = {i: [] for i in range(9)}
        for r in rows:
            for i in range(min(len(r), 9)):
                if r[i]:
                    cols[i].append(r[i])

        def first(i: int) -> str:
            return cols[i][0] if cols[i] else ""

        return {
            "name": first(0),
            "autoconnect": first(1).strip().lower() == "yes",
            "ifname": "" if first(2) == "--" else first(2),
            "ipv4_method": first(3) or "auto",
            "ipv4_addresses": ", ".join(cols[4]),
            "gateway": first(5),
            "dns": ", ".join(cols[6]),
            "ssid": first(7),
            "type": first(8),
        }

    @staticmethod
    def get_wifi_psk(uuid: str) -> str:
        """
        Fetch the stored Wi-Fi secret. Only call this on explicit user
        request (mirrors the `--show-secrets` pattern). Never log the result.
        """
        rows = NmcliBackend._terse(
            ["--show-secrets", "-f", "802-11-wireless-security.psk",
             "connection", "show", "uuid", uuid],
            log_errors=False,
        )
        if not rows or not rows[0]:
            return ""
        return rows[0][0]

    @staticmethod
    def get_overview_state() -> OverviewState:
        general = NmcliBackend.get_general()
        nm_available = general is not None
        devices = NmcliBackend.get_devices() if nm_available else []
        profiles = NmcliBackend.get_profiles() if nm_available else []
        access_points = NmcliBackend.get_access_points() if nm_available else []
        wifi_enabled = NmcliBackend.wifi_radio_on() if nm_available else False
        active = NmcliBackend.get_active_connections() if nm_available else {}

        enriched: List[ConnectionProfile] = []
        for p in profiles:
            hit = active.get(p.uuid)
            if hit is not None:
                name, dev = hit
                enriched.append(
                    replace(
                        p,
                        active=True,
                        active_device=dev or p.device,
                        ip_summary=NmcliBackend.get_ip_summary(p.uuid),
                    )
                )
            else:
                enriched.append(p)

        return OverviewState(
            nm_available=nm_available,
            general=general,
            devices=devices,
            profiles=enriched,
            access_points=access_points,
            wifi_enabled=wifi_enabled,
        )

    # -------------------------------------------------------------------------
    # Mutations
    # -------------------------------------------------------------------------
    @staticmethod
    def set_wifi_radio(on: bool) -> Tuple[bool, str]:
        return NmcliBackend._run_result(["radio", "wifi", "on" if on else "off"])

    @staticmethod
    def rescan() -> Tuple[bool, str]:
        # May fail when NM rate-limits scans; treat that as non-fatal.
        ok, err = NmcliBackend._run_result(
            ["device", "wifi", "rescan"], log_errors=False
        )
        if not ok and "rescan" in err.lower():
            return True, ""
        return ok, err

    @staticmethod
    def connect_profile(
        uuid: str,
        ifname: Optional[str] = None,
        ap: Optional[str] = None,
        passwd_entries: Optional[List[str]] = None,
    ) -> Tuple[bool, str]:
        args = ["--wait", "20", "connection", "up", "uuid", uuid]
        if ifname and ifname not in ("", "--"):
            args += ["ifname", ifname]
        if ap:
            args += ["ap", ap]
        path: Optional[str] = None
        if passwd_entries:
            path = NmcliBackend._write_passwd_file(passwd_entries)
            args += ["passwd-file", path]
        try:
            return NmcliBackend._run_result(args, timeout=NMCLI_UP_TIMEOUT)
        finally:
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    @staticmethod
    def disconnect_profile(uuid: str) -> Tuple[bool, str]:
        return NmcliBackend._run_result(
            ["--wait", "10", "connection", "down", "uuid", uuid],
            timeout=NMCLI_DOWN_TIMEOUT,
        )

    @staticmethod
    def delete_profile(uuid: str) -> Tuple[bool, str]:
        return NmcliBackend._run_result(["connection", "delete", "uuid", uuid])

    @staticmethod
    def clone_profile(uuid: str, new_name: str) -> Tuple[bool, str]:
        return NmcliBackend._run_result(
            ["connection", "clone", "uuid", uuid, new_name]
        )

    @staticmethod
    def set_autoconnect(uuid: str, on: bool) -> Tuple[bool, str]:
        return NmcliBackend._run_result(
            ["connection", "modify", "uuid", uuid,
             "connection.autoconnect", "yes" if on else "no"]
        )

    @staticmethod
    def modify_profile(uuid: str, pairs: List[str]) -> Tuple[bool, str]:
        if not pairs:
            return True, ""
        return NmcliBackend._run_result(
            ["connection", "modify", "uuid", uuid] + pairs
        )

    @staticmethod
    def reload_connections() -> Tuple[bool, str]:
        return NmcliBackend._run_result(["connection", "reload"])

    @staticmethod
    def import_vpn(vtype: str, path: str) -> Tuple[bool, str]:
        return NmcliBackend._run_result(
            ["connection", "import", "type", vtype, "file", path]
        )

    @staticmethod
    def export_vpn(uuid: str, path: str) -> Tuple[bool, str]:
        return NmcliBackend._run_result(
            ["connection", "export", "uuid", uuid, "file", path]
        )

    @staticmethod
    def add_ethernet(name: str, ifname: str, autoconnect: bool) -> Tuple[bool, str]:
        args = [
            "connection", "add", "type", "ethernet", "con-name", name,
            "save", "yes", "autoconnect", "yes" if autoconnect else "no",
        ]
        if ifname:
            args += ["ifname", ifname]
        return NmcliBackend._run_result(args)

    @staticmethod
    def add_wireguard(fields: Dict[str, str]) -> Tuple[bool, str]:
        ok, out, err = NmcliBackend._run_full(
            ["connection", "add", "type", "wireguard",
             "con-name", fields["name"], "save", "yes", "autoconnect", "no"]
        )
        if not ok:
            return False, err
        # Authoritative UUID from nmcli output; profile lookup only as fallback.
        uuid = NmcliBackend._parse_added_uuid(out)
        if not uuid:
            for p in NmcliBackend.get_profiles():
                if p.name == fields["name"] and "wireguard" in p.ctype.lower():
                    uuid = p.uuid
                    break
        if not uuid:
            return False, "Profile was created but could not be located."
        pairs = [
            "wireguard.private-key", fields["private_key"],
            "wireguard.listen-port", fields["listen_port"],
            "wireguard.peer", fields["peer_key"],
            "wireguard.allowed-ips", fields["allowed_ips"],
            "ipv4.method", "manual",
            "ipv4.addresses", fields["address"],
        ]
        if fields.get("endpoint"):
            pairs += ["wireguard.endpoint", fields["endpoint"]]
        ok, err = NmcliBackend._run_result(["connection", "modify", "uuid", uuid] + pairs)
        if not ok:
            NmcliBackend._run_result(["connection", "delete", "uuid", uuid],
                                     log_errors=False)
        return ok, err

    @staticmethod
    def connect_wifi(
        ssid: str,
        password: str,
        key_mgmt: str,
        identity: str,
        bssid: Optional[str],
        hidden: bool,
        save_secret: bool,
        existing_uuid: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """
        Connect to a Wi-Fi network, creating a profile when needed.
        Secrets are handed to nmcli through a temporary passwd-file and are
        never logged. When `save_secret` is False the secret flags are set to
        agent-owned (1) so the password is not persisted in the profile.
        """
        created: Optional[str] = None
        uuid = existing_uuid

        if not uuid:
            args = [
                "connection", "add", "type", "wifi",
                "con-name", ssid, "ssid", ssid,
                "save", "yes", "autoconnect", "yes",
            ]
            if hidden:
                args += ["802-11-wireless.hidden", "yes"]
            ok, out, err = NmcliBackend._run_full(args)
            if not ok:
                return False, err
            # Authoritative UUID straight from the nmcli confirmation line.
            # Re-querying by name is unreliable: nmcli silently renames the
            # new profile on name collisions (e.g. "MyWifi 1"), which used to
            # produce "Profile was created but could not be located."
            created = NmcliBackend._parse_added_uuid(out)
            if not created:
                # Fallback: match by SSID first, then by profile name.
                for p in NmcliBackend.get_profiles():
                    if "wireless" not in p.ctype.lower():
                        continue
                    if p.ssid == ssid or p.name == ssid:
                        created = p.uuid
                        break
            if not created:
                return False, "Profile was created but could not be located."
            uuid = created

        # Make sure the security method matches the network before handing
        # over any secret. This is idempotent for correctly configured
        # profiles and repairs profiles that are missing key-mgmt.
        if key_mgmt:
            pairs = ["802-11-wireless-security.key-mgmt", key_mgmt]
            if key_mgmt == "wpa-eap":
                pairs += [
                    "802-1x.eap", "peap",
                    "802-1x.phase2-auth", "mschapv2",
                ]
                if identity:
                    pairs += ["802-1x.identity", identity]
            ok, err = NmcliBackend._run_result(
                ["connection", "modify", "uuid", uuid] + pairs
            )
            if not ok:
                if created:
                    NmcliBackend._run_result(
                        ["connection", "delete", "uuid", uuid], log_errors=False
                    )
                return False, err

        entries: Optional[List[str]] = None
        if password:
            if key_mgmt == "wpa-eap":
                entries = [f"802-1x.password:{password}"]
                flag_prop = "802-1x.password-flags"
            elif key_mgmt == "wep":
                entries = [f"802-11-wireless-security.wep-key0:{password}"]
                flag_prop = "802-11-wireless-security.wep-key-flags"
            else:
                entries = [f"802-11-wireless-security.psk:{password}"]
                flag_prop = "802-11-wireless-security.psk-flags"
            if not save_secret:
                NmcliBackend._run_result(
                    ["connection", "modify", "uuid", uuid, flag_prop, "1"],
                    log_errors=False,
                )

        ok, err = NmcliBackend.connect_profile(uuid, ap=bssid, passwd_entries=entries)
        if not ok and created:
            # Do not leave half-configured profiles behind after a failed
            # first connection attempt.
            NmcliBackend._run_result(
                ["connection", "delete", "uuid", created], log_errors=False
            )
        return ok, err


# =============================================================================
# Async IPC worker
# =============================================================================
class IPCWorker(threading.Thread):
    """
    Daemon worker thread for all nmcli commands.
    - Avoids blocking the GTK main thread.
    - Keeps command order deterministic.
    - Daemonized so it cannot block process exit.
    """

    def __init__(self) -> None:
        super().__init__(daemon=True, name="nmcli-worker")
        self._queue: "queue.Queue[Optional[Callable[[], None]]]" = queue.Queue()

    def submit(self, task: Callable[[], None]) -> None:
        self._queue.put(task)

    def stop(self) -> None:
        self._queue.put(None)

    def run(self) -> None:
        while True:
            task = self._queue.get()
            if task is None:
                self._queue.task_done()
                break
            try:
                task()
            except Exception as exc:
                logger.error("Worker task failed: %s", exc)
            finally:
                self._queue.task_done()


# =============================================================================
# nmcli monitor worker
# =============================================================================
class NmcliMonitorWorker(threading.Thread):
    """
    Watch the persistent `nmcli monitor` stream and request a UI sync on any
    event. The payload is not parsed; every line is treated as a change
    notification and the GUI re-queries consistent state afterwards.
    """

    def __init__(self, on_event: Callable[[], None]) -> None:
        super().__init__(daemon=True, name="nmcli-monitor")
        self._on_event = on_event
        self._stop_event = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    def run(self) -> None:
        while not self._stop_event.is_set():
            proc: Optional[subprocess.Popen] = None
            try:
                with self._lock:
                    self._proc = subprocess.Popen(
                        ["nmcli", "monitor"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        text=True,
                        bufsize=1,
                        env=NmcliBackend._nmcli_env(),
                    )
                    proc = self._proc
                if proc is None or proc.stdout is None:
                    raise RuntimeError("Cannot open nmcli monitor stream")
                for line in proc.stdout:
                    if self._stop_event.is_set():
                        break
                    if line.strip():
                        try:
                            self._on_event()
                        except Exception as exc:
                            logger.debug("Monitor event callback failed: %s", exc)
            except FileNotFoundError:
                log_throttled(
                    logging.ERROR,
                    "nmcli-missing-monitor",
                    "Cannot find `nmcli` for the monitor stream.",
                )
                break
            except Exception as exc:
                logger.debug("Monitor stream error: %s", exc)
            finally:
                with self._lock:
                    current = self._proc
                    self._proc = None
                if current is not None:
                    try:
                        current.terminate()
                    except Exception:
                        pass
                    try:
                        current.wait(timeout=1.0)
                    except Exception:
                        try:
                            current.kill()
                        except Exception:
                            pass
            if not self._stop_event.is_set():
                time.sleep(MONITOR_RESTART_DELAY_SECONDS)

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass


# =============================================================================
# Single-instance controller
# =============================================================================
class SingleInstance:
    """
    Ensures only one Network Overview process is active.
    - First instance acquires an flock and listens on a Unix socket.
    - A second instance sends `show` to the first instance and exits.
    - If the first instance does not acknowledge, the second instance
      terminates it and takes over.
    """

    def __init__(self) -> None:
        runtime_dir = get_runtime_dir()
        self.lock_path = os.path.join(runtime_dir, LOCK_FILE_NAME)
        self.sock_path = os.path.join(runtime_dir, SOCKET_FILE_NAME)
        self._lock_file: Optional[Any] = None
        self.server_socket: Optional[socket.socket] = None
        self._released = False

    def ensure_single_instance(self) -> bool:
        """
        Return True when this process should continue as the active instance.
        Return False when an existing instance was notified and this process
        should exit.
        """
        result = self._try_acquire_lock()
        if result is True:
            self._write_pid()
            self._setup_socket()
            return True
        if result is None:
            logger.warning("Single-instance lock unavailable; continuing without lock.")
            return True
        if self._send_command(IPC_MESSAGE_SHOW):
            logger.info("Another instance is already running; asked it to show.")
            return False
        pid = self._read_pid()
        if pid and pid != os.getpid():
            logger.info("Existing instance is not responding; terminating PID %s.", pid)
            self._terminate_pid(pid, signal.SIGTERM)
            for attempt in range(15):
                time.sleep(0.1)
                retry = self._try_acquire_lock()
                if retry is True:
                    self._write_pid()
                    self._setup_socket()
                    return True
                if retry is None:
                    return True
                if attempt == 7 and pid and pid != os.getpid():
                    logger.warning("Existing instance still holds lock; sending SIGKILL.")
                    self._terminate_pid(pid, signal.SIGKILL)
        logger.error("Cannot acquire single-instance lock.")
        return False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self.server_socket is not None:
            try:
                self.server_socket.close()
            except OSError as exc:
                logger.debug("Cannot close instance socket: %s", exc)
            try:
                if os.path.exists(self.sock_path):
                    os.unlink(self.sock_path)
            except OSError as exc:
                logger.debug("Cannot unlink instance socket: %s", exc)
            self.server_socket = None
        if self._lock_file is not None:
            try:
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            except OSError as exc:
                logger.debug("Cannot unlock instance lock: %s", exc)
            try:
                self._lock_file.close()
            except OSError as exc:
                logger.debug("Cannot close instance lock file: %s", exc)
            self._lock_file = None

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------
    def _try_acquire_lock(self) -> Optional[bool]:
        """
        True: lock acquired. False: held by another process. None: infra error.
        """
        try:
            self._lock_file = open(self.lock_path, "w")
        except OSError as exc:
            logger.error("Cannot open lock file %s: %s", self.lock_path, exc)
            self._lock_file = None
            return None
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as exc:
            try:
                self._lock_file.close()
            except Exception as close_exc:
                logger.debug("Cannot close lock file after failed lock: %s", close_exc)
            self._lock_file = None
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                return False
            logger.error("Cannot lock %s: %s", self.lock_path, exc)
            return None

    def _write_pid(self) -> None:
        if self._lock_file is None:
            return
        try:
            self._lock_file.seek(0)
            self._lock_file.truncate()
            self._lock_file.write(str(os.getpid()))
            self._lock_file.flush()
        except OSError as exc:
            logger.debug("Cannot write PID to lock file: %s", exc)

    def _setup_socket(self) -> None:
        try:
            if os.path.exists(self.sock_path):
                os.unlink(self.sock_path)
        except OSError as exc:
            logger.debug("Cannot remove stale socket %s: %s", self.sock_path, exc)
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.setblocking(False)
            sock.bind(self.sock_path)
            sock.listen(8)
            try:
                os.chmod(self.sock_path, 0o600)
            except OSError as exc:
                logger.debug("Cannot chmod socket %s: %s", self.sock_path, exc)
            self.server_socket = sock
        except OSError as exc:
            logger.error("Cannot create single-instance socket: %s", exc)
            self.server_socket = None

    def _send_command(self, command: str) -> bool:
        """Send command to existing instance and wait for ACK."""
        for _attempt in range(3):
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(0.25)
                    s.connect(self.sock_path)
                    s.sendall(command.encode("utf-8"))
                    ack = s.recv(16)
                    if ack.startswith(b"ok"):
                        return True
            except OSError as exc:
                logger.debug("Cannot talk to existing instance: %s", exc)
            time.sleep(0.08)
        return False

    def _read_pid(self) -> Optional[int]:
        try:
            with open(self.lock_path, "r", encoding="utf-8") as f:
                text = f.read().strip()
            if text:
                return int(text)
        except (OSError, ValueError) as exc:
            logger.debug("Cannot read PID from lock file: %s", exc)
        return None

    def _terminate_pid(self, pid: int, sig: int) -> None:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            logger.debug("PID %s already exited.", pid)
        except PermissionError:
            logger.error("No permission to terminate PID %s.", pid)
        except OSError as exc:
            logger.error("Cannot terminate PID %s: %s", pid, exc)


# =============================================================================
# Icon provider
# =============================================================================
class IconProvider:
    """Cached icon lookup to avoid repeated Gtk.IconTheme queries."""

    def __init__(self) -> None:
        self._cache: Dict[str, str] = {}
        self._theme: Optional[Gtk.IconTheme] = Gtk.IconTheme.get_default()
        if self._theme is not None:
            try:
                self._theme.connect("changed", self._on_theme_changed)
            except Exception as exc:
                logger.debug("Cannot watch icon theme changes: %s", exc)

    def _on_theme_changed(self, _theme: Gtk.IconTheme) -> None:
        self._cache.clear()
        logger.info("Icon theme changed; icon cache cleared.")

    def first_available(self, candidates: List[str]) -> str:
        """Return the first installed icon among candidates (last = fallback)."""
        key = "|".join(candidates)
        cached = self._cache.get(key)
        if cached:
            return cached
        result = candidates[-1]
        if self._theme is not None:
            for candidate in candidates[:-1]:
                try:
                    if self._theme.has_icon(candidate):
                        result = candidate
                        break
                except Exception as exc:
                    logger.debug("Icon lookup failed for %s: %s", candidate, exc)
        if len(self._cache) > MAX_CACHED_ICONS:
            self._cache.clear()
        self._cache[key] = result
        return result

    def type_icon(self, ctype: str) -> str:
        t = (ctype or "").lower()
        if "wireless" in t or t == "wifi":
            candidates = ["network-wireless", "network-idle"]
        elif "ethernet" in t or "802-3" in t:
            candidates = ["network-wired", "network-idle"]
        elif "vpn" in t:
            candidates = ["network-vpn", "channel-secure", "network-secure", "network-idle"]
        elif "wireguard" in t:
            candidates = ["network-vpn", "channel-secure", "network-idle"]
        elif "bluetooth" in t:
            candidates = ["network-bluetooth", "bluetooth", "network-idle"]
        elif t in ("gsm", "cdma") or "cellular" in t:
            candidates = ["network-cellular", "phone", "network-idle"]
        elif "bridge" in t:
            candidates = ["network-bridge", "network-wired", "network-idle"]
        elif "vlan" in t or "bond" in t or "team" in t:
            candidates = ["network-workgroup", "network-wired", "network-idle"]
        else:
            candidates = ["network-idle", "application-x-executable"]
        return self.first_available(candidates)

    def device_icon(self, dtype: str) -> str:
        t = (dtype or "").lower()
        if "wifi" in t or "wireless" in t:
            candidates = ["network-wireless", "network-idle"]
        elif "ethernet" in t:
            candidates = ["network-wired", "network-idle"]
        elif "bluetooth" in t:
            candidates = ["network-bluetooth", "bluetooth", "network-idle"]
        elif t in ("gsm", "cdma"):
            candidates = ["network-cellular", "phone", "network-idle"]
        elif "bridge" in t:
            candidates = ["network-bridge", "network-wired", "network-idle"]
        elif "tun" in t or "tap" in t:
            candidates = ["network-vpn", "network-idle"]
        else:
            candidates = ["network-idle", "application-x-executable"]
        return self.first_available(candidates)

    def signal_icon(self, strength: int) -> str:
        if strength >= 80:
            level = "excellent"
        elif strength >= 55:
            level = "good"
        elif strength >= 40:
            level = "ok"
        elif strength >= 20:
            level = "low"
        else:
            level = "none"
        return self.first_available(
            [f"network-wireless-signal-{level}", "network-wireless", "network-idle"]
        )

    def lock_icon(self) -> str:
        return self.first_available(
            ["network-wireless-encrypted", "security-high",
             "changes-prevent", "network-wireless"]
        )

    def action_icon(self, names: List[str]) -> str:
        return self.first_available(names + ["image-missing"])


# =============================================================================
# Dialogs
# =============================================================================
class FormDialog(Gtk.Dialog):
    """Base class for simple grid-based forms."""

    def __init__(self, parent: Gtk.Window, title: str, ok_label: str = "OK") -> None:
        super().__init__(
            title=title,
            transient_for=parent,
            modal=True,
            destroy_with_parent=True,
        )
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        ok = self.add_button(ok_label, Gtk.ResponseType.OK)
        ok.get_style_context().add_class("suggested-action")
        self.set_default_response(Gtk.ResponseType.OK)
        area = self.get_content_area()
        area.set_spacing(10)
        area.set_border_width(16)
        self.grid = Gtk.Grid(column_spacing=12, row_spacing=9)
        area.pack_start(self.grid, True, True, 0)
        self._row = 0

    def add_row(self, text: str, widget: Gtk.Widget) -> Gtk.Label:
        label = Gtk.Label(label=text)
        label.set_halign(Gtk.Align.START)
        self.grid.attach(label, 0, self._row, 1, 1)
        widget.set_hexpand(True)
        self.grid.attach(widget, 1, self._row, 1, 1)
        self._row += 1
        return label

    def add_full(self, widget: Gtk.Widget) -> None:
        self.grid.attach(widget, 0, self._row, 2, 1)
        self._row += 1


class PasswordDialog(FormDialog):
    """Ask for a Wi-Fi secret, with show/hide and store-in-profile options."""

    def __init__(self, parent: Gtk.Window, ssid: str, key_mgmt: str) -> None:
        super().__init__(parent, "Wi-Fi Authentication Required", ok_label="Connect")
        self.key_mgmt = key_mgmt
        desc = {
            "wpa-eap": "WPA Enterprise (802.1X)",
            "sae": "WPA3 Personal (SAE)",
            "wep": "WEP (legacy)",
            "owe": "Enhanced Open (OWE)",
        }.get(key_mgmt, "WPA/WPA2 Personal")

        info = Gtk.Label(label=f"\u201c{ssid}\u201d is secured with {desc}.")
        info.set_halign(Gtk.Align.START)
        info.set_line_wrap(True)
        self.add_full(info)

        self.identity: Optional[Gtk.Entry] = None
        if key_mgmt == "wpa-eap":
            self.identity = Gtk.Entry()
            self.identity.set_placeholder_text("username")
            self.add_row("Identity", self.identity)

        secret_label = "WEP key" if key_mgmt == "wep" else "Password"
        self.entry = Gtk.Entry()
        self.entry.set_visibility(False)
        self.entry.set_invisible_char("\u2022")
        self.entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        self.entry.connect("activate", lambda _e: self.response(Gtk.ResponseType.OK))
        self.add_row(secret_label, self.entry)

        self.show_check = Gtk.CheckButton(label="Show password")
        self.show_check.connect("toggled", self._on_show_toggled)
        self.add_full(self.show_check)

        self.save_check = Gtk.CheckButton(label="Store password in profile")
        self.save_check.set_active(True)
        self.add_full(self.save_check)

        self.show_all()

    def _on_show_toggled(self, check: Gtk.CheckButton) -> None:
        self.entry.set_visibility(check.get_active())

    def get_values(self) -> Dict[str, Any]:
        return {
            "password": self.entry.get_text(),
            "save": self.save_check.get_active(),
            "identity": self.identity.get_text().strip() if self.identity else "",
        }


class HiddenNetworkDialog(FormDialog):
    """Collect details for connecting to a hidden Wi-Fi network."""

    def __init__(self, parent: Gtk.Window) -> None:
        super().__init__(parent, "Connect to a Hidden Network", ok_label="Connect")

        self.ssid = Gtk.Entry()
        self.ssid.set_placeholder_text("Network name (SSID)")
        self.add_row("SSID", self.ssid)

        self.security = Gtk.ComboBoxText()
        for label in SECURITY_COMBO_LABELS:
            self.security.append_text(label)
        self.security.set_active(1)
        self.security.connect("changed", self._on_security_changed)
        self.add_row("Security", self.security)

        self.password = Gtk.Entry()
        self.password.set_visibility(False)
        self.password.set_invisible_char("\u2022")
        self.password.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        self.password.connect("activate", lambda _e: self.response(Gtk.ResponseType.OK))
        self.pw_label = self.add_row("Password", self.password)

        self.save_check = Gtk.CheckButton(label="Store password in profile")
        self.save_check.set_active(True)
        self.add_full(self.save_check)

        self.show_all()

    def _on_security_changed(self, combo: Gtk.ComboBoxText) -> None:
        needs = SECURITY_COMBO_KM[combo.get_active()] != ""
        self.password.set_visible(needs)
        self.pw_label.set_visible(needs)

    def get_values(self) -> Dict[str, Any]:
        idx = max(0, self.security.get_active())
        return {
            "ssid": self.ssid.get_text().strip(),
            "key_mgmt": SECURITY_COMBO_KM[idx],
            "password": self.password.get_text(),
            "save": self.save_check.get_active(),
        }


class CreateConnectionDialog(FormDialog):
    """Create a new profile using the same properties as `nmcli connection add`."""

    def __init__(self, parent: Gtk.Window, eth_devices: List[str]) -> None:
        super().__init__(parent, "New Connection", ok_label="Create")
        self._typed_rows: List[Tuple[Gtk.Widget, Gtk.Widget, Set[str]]] = []

        self.ctype = Gtk.ComboBoxText()
        for label in ("Wi-Fi", "Ethernet", "WireGuard"):
            self.ctype.append_text(label)
        self.ctype.set_active(0)
        self.ctype.connect("changed", self._on_type_changed)
        self.add_row("Type", self.ctype)

        self.name = Gtk.Entry()
        self.name.set_text("New connection")
        self.add_row("Name", self.name)

        # --- Wi-Fi fields ---
        self.ssid = Gtk.Entry()
        self._add_typed("SSID", self.ssid, {"Wi-Fi"})

        self.security = Gtk.ComboBoxText()
        for label in SECURITY_COMBO_LABELS:
            self.security.append_text(label)
        self.security.set_active(1)
        self._add_typed("Security", self.security, {"Wi-Fi"})

        self.password = Gtk.Entry()
        self.password.set_visibility(False)
        self.password.set_invisible_char("\u2022")
        self._add_typed("Password", self.password, {"Wi-Fi"})

        self.save_check = Gtk.CheckButton(label="Store password in profile")
        self.save_check.set_active(True)
        self._add_typed_full(self.save_check, {"Wi-Fi"})

        self.hidden_check = Gtk.CheckButton(label="This is a hidden network")
        self._add_typed_full(self.hidden_check, {"Wi-Fi"})

        # --- Ethernet fields ---
        self.device = Gtk.ComboBoxText()
        self.device.append_text("(any device)")
        for dev in eth_devices:
            self.device.append_text(dev)
        self.device.set_active(0)
        self._add_typed("Device", self.device, {"Ethernet"})

        self.auto_check = Gtk.CheckButton(label="Connect automatically")
        self.auto_check.set_active(True)
        self._add_typed_full(self.auto_check, {"Ethernet"})

        # --- WireGuard fields ---
        self.wg_private = Gtk.Entry()
        self.wg_private.set_placeholder_text("private key (base64)")
        self._add_typed("Private key", self.wg_private, {"WireGuard"})

        self.wg_port = Gtk.Entry()
        self.wg_port.set_text("51820")
        self._add_typed("Listen port", self.wg_port, {"WireGuard"})

        self.wg_peer = Gtk.Entry()
        self.wg_peer.set_placeholder_text("peer public key (base64)")
        self._add_typed("Peer public key", self.wg_peer, {"WireGuard"})

        self.wg_endpoint = Gtk.Entry()
        self.wg_endpoint.set_placeholder_text("vpn.example.com:51820")
        self._add_typed("Endpoint", self.wg_endpoint, {"WireGuard"})

        self.wg_allowed = Gtk.Entry()
        self.wg_allowed.set_text("0.0.0.0/0")
        self._add_typed("Allowed IPs", self.wg_allowed, {"WireGuard"})

        self.wg_address = Gtk.Entry()
        self.wg_address.set_text("10.66.0.2/32")
        self._add_typed("Interface address", self.wg_address, {"WireGuard"})

        self.show_all()
        self._on_type_changed(self.ctype)

    def _add_typed(self, text: str, widget: Gtk.Widget, types: Set[str]) -> None:
        label = self.add_row(text, widget)
        self._typed_rows.append((label, widget, types))

    def _add_typed_full(self, widget: Gtk.Widget, types: Set[str]) -> None:
        self.add_full(widget)
        self._typed_rows.append((widget, widget, types))

    def _on_type_changed(self, combo: Gtk.ComboBoxText) -> None:
        current = combo.get_active_text() or "Wi-Fi"
        for label, widget, types in self._typed_rows:
            visible = current in types
            label.set_visible(visible)
            widget.set_visible(visible)

    def get_values(self) -> Dict[str, Any]:
        ctype = self.ctype.get_active_text() or "Wi-Fi"
        values: Dict[str, Any] = {"ctype": ctype, "name": self.name.get_text().strip()}
        if ctype == "Wi-Fi":
            values.update(
                {
                    "ssid": self.ssid.get_text().strip(),
                    "key_mgmt": SECURITY_COMBO_KM[max(0, self.security.get_active())],
                    "password": self.password.get_text(),
                    "save": self.save_check.get_active(),
                    "hidden": self.hidden_check.get_active(),
                }
            )
        elif ctype == "Ethernet":
            dev = self.device.get_active_text() or "(any device)"
            values["ifname"] = "" if dev == "(any device)" else dev
            values["autoconnect"] = self.auto_check.get_active()
        else:
            values.update(
                {
                    "private_key": self.wg_private.get_text().strip(),
                    "listen_port": self.wg_port.get_text().strip() or "51820",
                    "peer_key": self.wg_peer.get_text().strip(),
                    "endpoint": self.wg_endpoint.get_text().strip(),
                    "allowed_ips": self.wg_allowed.get_text().strip() or "0.0.0.0/0",
                    "address": self.wg_address.get_text().strip(),
                }
            )
        return values


class EditConnectionDialog(FormDialog):
    """Edit the most common profile properties via `nmcli connection modify`."""

    def __init__(
        self,
        parent: Gtk.Window,
        profile: ConnectionProfile,
        details: Dict[str, Any],
    ) -> None:
        super().__init__(parent, f"Edit \u201c{profile.name}\u201d", ok_label="Save")
        self.profile = profile
        self.details = details
        self.is_wifi = "wireless" in profile.ctype.lower() or "wifi" in profile.ctype.lower()

        self.name = Gtk.Entry()
        self.name.set_text(details.get("name") or profile.name)
        self.add_row("Name", self.name)

        self.auto_check = Gtk.CheckButton(label="Connect automatically")
        self.auto_check.set_active(bool(details.get("autoconnect")))
        self.add_full(self.auto_check)

        self.ssid: Optional[Gtk.Entry] = None
        if self.is_wifi:
            self.ssid = Gtk.Entry()
            self.ssid.set_text(details.get("ssid") or "")
            self.add_row("SSID", self.ssid)

        self.method = Gtk.ComboBoxText()
        for m in ("auto", "manual", "disabled"):
            self.method.append_text(m)
        current = details.get("ipv4_method") or "auto"
        self.method.set_active(("auto", "manual", "disabled").index(current)
                               if current in ("auto", "manual", "disabled") else 0)
        self.add_row("IPv4 method", self.method)

        self.addresses = Gtk.Entry()
        self.addresses.set_text(details.get("ipv4_addresses") or "")
        self.addresses.set_placeholder_text("192.168.1.10/24, 10.0.0.5/16")
        self.add_row("Addresses", self.addresses)

        self.gateway = Gtk.Entry()
        self.gateway.set_text(details.get("gateway") or "")
        self.add_row("Gateway", self.gateway)

        self.dns = Gtk.Entry()
        self.dns.set_text(details.get("dns") or "")
        self.dns.set_placeholder_text("8.8.8.8, 1.1.1.1")
        self.add_row("DNS", self.dns)

        self.psk_entry: Optional[Gtk.Entry] = None
        if self.is_wifi:
            reveal_btn = Gtk.Button(label="Reveal stored password")
            reveal_btn.connect("clicked", self._on_reveal)
            self.add_row("Secret", reveal_btn)
            self.psk_entry = Gtk.Entry()
            self.psk_entry.set_editable(False)
            self.psk_entry.set_visibility(False)
            self.psk_entry.set_placeholder_text("shown after reveal")
            self.add_row("", self.psk_entry)

        self.show_all()

    def _on_reveal(self, _btn: Gtk.Button) -> None:
        # Explicit user request only: this is the --show-secrets path.
        # The returned secret is displayed but never logged.
        psk = NmcliBackend.get_wifi_psk(self.profile.uuid)
        if self.psk_entry is not None:
            self.psk_entry.set_text(psk or "(no stored password)")
            self.psk_entry.set_visibility(True)

    def get_pairs(self) -> List[str]:
        pairs: List[str] = [
            "connection.id", self.name.get_text().strip() or self.profile.name,
            "connection.autoconnect", "yes" if self.auto_check.get_active() else "no",
        ]
        method = self.method.get_active_text() or "auto"
        pairs += ["ipv4.method", method]
        manual = method == "manual"
        pairs += ["ipv4.addresses", self.addresses.get_text().strip() if manual else ""]
        pairs += ["ipv4.gateway", self.gateway.get_text().strip() if manual else ""]
        pairs += ["ipv4.dns", self.dns.get_text().strip() if manual else ""]
        if self.is_wifi and self.ssid is not None:
            ssid = self.ssid.get_text().strip()
            if ssid and ssid != (self.details.get("ssid") or ""):
                pairs += ["802-11-wireless.ssid", ssid]
        return pairs


class NameDialog(FormDialog):
    """Ask for a single name (used by Clone)."""

    def __init__(self, parent: Gtk.Window, title: str, default: str) -> None:
        super().__init__(parent, title, ok_label="OK")
        self.entry = Gtk.Entry()
        self.entry.set_text(default)
        self.entry.connect("activate", lambda _e: self.response(Gtk.ResponseType.OK))
        self.add_row("Name", self.entry)
        self.show_all()

    def get_value(self) -> str:
        return self.entry.get_text().strip()


class ImportVpnDialog(FormDialog):
    """Choose a VPN type and configuration file to import."""

    def __init__(self, parent: Gtk.Window) -> None:
        super().__init__(parent, "Import VPN Configuration", ok_label="Import")
        self.vtype = Gtk.ComboBoxText()
        for t in VPN_IMPORT_TYPES:
            self.vtype.append_text(t)
        self.vtype.set_active(0)
        self.add_row("VPN type", self.vtype)

        self.chooser = Gtk.FileChooserButton(title="Select configuration file")
        self.chooser.set_action(Gtk.FileChooserAction.OPEN)
        self.add_row("File", self.chooser)
        self.show_all()

    def get_values(self) -> Tuple[str, Optional[str]]:
        return self.vtype.get_active_text() or "openvpn", self.chooser.get_filename()


def confirm_yes_no(parent: Gtk.Window, title: str, text: str) -> bool:
    dlg = Gtk.MessageDialog(
        transient_for=parent,
        modal=True,
        message_type=Gtk.MessageType.QUESTION,
        buttons=Gtk.ButtonsType.YES_NO,
        text=title,
    )
    dlg.format_secondary_text(text)
    response = dlg.run()
    dlg.destroy()
    return response == Gtk.ResponseType.YES


def choose_save_file(parent: Gtk.Window, title: str, default_name: str) -> Optional[str]:
    dlg = Gtk.FileChooserDialog(
        title=title,
        transient_for=parent,
        action=Gtk.FileChooserAction.SAVE,
    )
    dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
    dlg.add_button("Save", Gtk.ResponseType.OK)
    dlg.set_current_name(default_name)
    dlg.set_do_overwrite_confirmation(True)
    response = dlg.run()
    path = dlg.get_filename() if response == Gtk.ResponseType.OK else None
    dlg.destroy()
    return path


# =============================================================================
# Widgets
# =============================================================================
class DeviceRow(Gtk.EventBox):
    """One network device: icon, name, type/state/connection."""

    def __init__(self, device: DeviceState, main_window: "NetworkOverview") -> None:
        super().__init__()
        self.device = device
        self.main_window = main_window
        self.set_visible_window(True)
        self.set_can_focus(False)

        self.box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.box.get_style_context().add_class("row-card")

        self.icon = Gtk.Image()
        self.title = Gtk.Label()
        self.title.set_halign(Gtk.Align.START)
        self.title.set_ellipsize(Pango.EllipsizeMode.END)
        self.title.get_style_context().add_class("row-title")

        self.sub = Gtk.Label()
        self.sub.set_halign(Gtk.Align.START)
        self.sub.set_ellipsize(Pango.EllipsizeMode.END)
        self.sub.get_style_context().add_class("sub-label")

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_box.set_hexpand(True)
        text_box.pack_start(self.title, False, False, 0)
        text_box.pack_start(self.sub, False, False, 0)

        self.box.pack_start(self.icon, False, False, 0)
        self.box.pack_start(text_box, True, True, 0)
        self.add(self.box)
        self._render()

    def _render(self) -> None:
        icon_name = self.main_window.icon_provider.device_icon(self.device.dtype)
        self.icon.set_from_icon_name(icon_name, Gtk.IconSize.MENU)
        self.icon.set_pixel_size(self.main_window.icon_pixel_size)
        self.title.set_text(self.device.device)
        conn = self.device.connection or "not connected"
        self.sub.set_text(f"{display_type(self.device.dtype)} \u2022 {self.device.state} \u2022 {conn}")

    def update(self, new_device: DeviceState) -> None:
        if self.device != new_device:
            self.device = new_device
            self._render()


class AccessPointRow(Gtk.EventBox):
    """One Wi-Fi network: signal bars, lock, SSID, band, connect action."""

    def __init__(self, ap: AccessPoint, main_window: "NetworkOverview") -> None:
        super().__init__()
        self.ap = ap
        self.main_window = main_window
        self.set_visible_window(True)
        self.set_can_focus(False)
        self.connect("button-release-event", self.on_button_release)

        self.box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.box.get_style_context().add_class("row-card")

        self.signal_icon = Gtk.Image()
        self.lock_icon = Gtk.Image()

        self.title = Gtk.Label()
        self.title.set_halign(Gtk.Align.START)
        self.title.set_ellipsize(Pango.EllipsizeMode.END)
        self.title.get_style_context().add_class("row-title")

        self.sub = Gtk.Label()
        self.sub.set_halign(Gtk.Align.START)
        self.sub.set_ellipsize(Pango.EllipsizeMode.END)
        self.sub.get_style_context().add_class("sub-label")

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_box.set_hexpand(True)
        text_box.pack_start(self.title, False, False, 0)
        text_box.pack_start(self.sub, False, False, 0)

        self.badge = Gtk.Label(label="Connected")
        self.badge.get_style_context().add_class("badge")
        self.badge.get_style_context().add_class("badge-connected")

        self.connect_btn = Gtk.Button(label="Connect")
        self.connect_btn.get_style_context().add_class("btn-primary")
        self.connect_btn.set_valign(Gtk.Align.CENTER)
        self.connect_btn.connect("clicked", self._on_connect_clicked)

        self.box.pack_start(self.signal_icon, False, False, 0)
        self.box.pack_start(self.lock_icon, False, False, 0)
        self.box.pack_start(text_box, True, True, 0)
        self.box.pack_start(self.badge, False, False, 0)
        self.box.pack_start(self.connect_btn, False, False, 0)
        self.add(self.box)
        self._render()

    def _render(self) -> None:
        provider = self.main_window.icon_provider
        self.signal_icon.set_from_icon_name(
            provider.signal_icon(self.ap.signal), Gtk.IconSize.MENU
        )
        self.signal_icon.set_pixel_size(self.main_window.icon_pixel_size)

        secured = bool(key_mgmt_for_security(self.ap.security))
        self.lock_icon.set_visible(secured)
        if secured:
            self.lock_icon.set_from_icon_name(provider.lock_icon(), Gtk.IconSize.MENU)
            self.lock_icon.set_pixel_size(max(12, self.main_window.icon_pixel_size - 8))

        self.title.set_text(self.ap.ssid or "(Hidden network)")
        bits = [self.ap.security or "Open"]
        if self.ap.freq:
            bits.append(self.ap.freq)
        if self.ap.rate:
            bits.append(self.ap.rate)
        bits.append(f"BSSID {self.ap.bssid}")
        self.sub.set_text(" \u2022 ".join(bits))

        ctx = self.box.get_style_context()
        if self.ap.in_use:
            ctx.add_class("row-active")
            self.badge.set_visible(True)
            self.connect_btn.set_visible(False)
        else:
            ctx.remove_class("row-active")
            self.badge.set_visible(False)
            self.connect_btn.set_visible(True)

        self.set_tooltip_text(
            f"Signal {self.ap.signal}% \u2022 {self.ap.security or 'Open'}"
        )

    def update(self, new_ap: AccessPoint) -> None:
        if self.ap != new_ap:
            self.ap = new_ap
            self._render()

    def _on_connect_clicked(self, _btn: Gtk.Button) -> None:
        self.main_window.connect_to_ap(self.ap)

    def on_button_release(self, _widget: Gtk.Widget, event: Gdk.EventButton) -> bool:
        if event.button != 1:
            return False
        if is_instance_or_ancestor(Gtk.get_event_widget(event), Gtk.Button):
            return False
        self.main_window.connect_to_ap(self.ap)
        return True


class ConnectionRow(Gtk.EventBox):
    """One connection profile with state badge, autoconnect and actions."""

    def __init__(self, profile: ConnectionProfile, main_window: "NetworkOverview") -> None:
        super().__init__()
        self.profile = profile
        self.main_window = main_window
        self.set_visible_window(True)
        self.set_can_focus(False)
        # Context menu lifetime is owned by this row. A popup Gtk.Menu with
        # no surviving reference is garbage-collected by PyGObject before it
        # becomes visible, which is why the menu must be stored on the row.
        self._menu: Optional[Gtk.Menu] = None
        self.connect("button-press-event", self.on_button_press)
        self.connect("button-release-event", self.on_button_release)
        self.connect("destroy", self._on_row_destroy)

        self.box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.box.get_style_context().add_class("row-card")

        self.icon = Gtk.Image()
        self.title = Gtk.Label()
        self.title.set_halign(Gtk.Align.START)
        self.title.set_ellipsize(Pango.EllipsizeMode.END)
        self.title.get_style_context().add_class("row-title")

        self.sub = Gtk.Label()
        self.sub.set_halign(Gtk.Align.START)
        self.sub.set_ellipsize(Pango.EllipsizeMode.END)
        self.sub.get_style_context().add_class("sub-label")

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_box.set_hexpand(True)
        text_box.pack_start(self.title, False, False, 0)
        text_box.pack_start(self.sub, False, False, 0)

        self.badge = Gtk.Label()
        self.badge.get_style_context().add_class("badge")

        self.auto_switch = Gtk.Switch()
        self.auto_switch.set_valign(Gtk.Align.CENTER)
        self.auto_switch.set_tooltip_text("Connect automatically")
        self._auto_hid = self.auto_switch.connect("state-set", self._on_auto_set)

        self.action_btn = Gtk.Button()
        self.action_btn.set_valign(Gtk.Align.CENTER)
        self.action_btn.connect("clicked", self._on_action_clicked)

        self.menu_btn = Gtk.Button()
        self.menu_btn.set_valign(Gtk.Align.CENTER)
        self.menu_btn.set_tooltip_text("More actions")
        menu_image = Gtk.Image()
        menu_image.set_from_icon_name(
            main_window.icon_provider.action_icon(
                ["view-more-symbolic", "open-menu-symbolic", "view-more"]
            ),
            Gtk.IconSize.MENU,
        )
        self.menu_btn.add(menu_image)
        self.menu_btn.connect("clicked", self._on_menu_clicked)

        self.box.pack_start(self.icon, False, False, 0)
        self.box.pack_start(text_box, True, True, 0)
        self.box.pack_start(self.badge, False, False, 0)
        self.box.pack_start(self.auto_switch, False, False, 0)
        self.box.pack_start(self.action_btn, False, False, 0)
        self.box.pack_start(self.menu_btn, False, False, 0)
        self.add(self.box)
        self._render()

    def _render(self) -> None:
        p = self.profile
        self.icon.set_from_icon_name(
            self.main_window.icon_provider.type_icon(p.ctype), Gtk.IconSize.MENU
        )
        self.icon.set_pixel_size(self.main_window.icon_pixel_size)
        self.title.set_text(p.name)

        device = p.active_device or p.device or "no device"
        bits = [display_type(p.ctype), device]
        if p.active and p.ip_summary:
            bits.append(p.ip_summary)
        self.sub.set_text(" \u2022 ".join(bits))

        badge_ctx = self.badge.get_style_context()
        box_ctx = self.box.get_style_context()
        action_ctx = self.action_btn.get_style_context()
        if p.active:
            self.badge.set_text("Connected")
            badge_ctx.remove_class("badge-idle")
            badge_ctx.add_class("badge-connected")
            box_ctx.add_class("row-active")
            self.action_btn.set_label("Disconnect")
            action_ctx.remove_class("btn-primary")
        else:
            self.badge.set_text("Idle")
            badge_ctx.remove_class("badge-connected")
            badge_ctx.add_class("badge-idle")
            box_ctx.remove_class("row-active")
            self.action_btn.set_label("Connect")
            action_ctx.add_class("btn-primary")

        self.auto_switch.handler_block(self._auto_hid)
        self.auto_switch.set_active(p.autoconnect)
        self.auto_switch.handler_unblock(self._auto_hid)

        self.set_tooltip_text(f"{p.name} \u2022 {p.uuid}")

    def update(self, new_profile: ConnectionProfile) -> None:
        if self.profile != new_profile:
            self.profile = new_profile
            self._render()

    # -------------------------------------------------------------------------
    # Context menu
    # -------------------------------------------------------------------------
    def _on_row_destroy(self, _widget: Gtk.Widget) -> None:
        if self._menu is not None:
            try:
                self._menu.destroy()
            except Exception:
                pass
            self._menu = None

    def popup_menu(self, event: Optional[Gdk.EventButton] = None) -> None:
        """
        Build and show the context menu.

        The menu is kept alive on self._menu: a Gtk.Menu created inside a
        callback with no surviving reference is garbage-collected by
        PyGObject before GTK can display it (this is exactly why
        right-click appeared to do nothing before).
        """
        if self._menu is not None:
            try:
                self._menu.destroy()
            except Exception:
                pass
        menu = self.build_menu()
        self._menu = menu
        try:
            if event is not None:
                menu.popup_at_pointer(event)
            else:
                # Opened via the "more actions" button: anchor to it.
                menu.popup_at_widget(
                    self.menu_btn,
                    Gdk.Gravity.SOUTH_EAST,
                    Gdk.Gravity.NORTH_EAST,
                    None,
                )
        except Exception:
            # Very old GTK without popup_at_pointer/popup_at_widget.
            button = event.button if event is not None else 0
            etime = event.time if event is not None else Gtk.get_current_event_time()
            menu.popup(None, None, None, None, button, etime)

    def on_button_press(self, _widget: Gtk.Widget, event: Gdk.EventButton) -> bool:
        # Show the context menu on button press: this is the standard GTK
        # behavior and feels immediate (no waiting for the release).
        if event.button == 3:
            self.popup_menu(event)
            return True
        return False

    def _on_menu_clicked(self, _btn: Gtk.Button) -> None:
        self.popup_menu(None)

    # -------------------------------------------------------------------------
    # Events
    # -------------------------------------------------------------------------
    def _on_auto_set(self, _switch: Gtk.Switch, active: bool) -> bool:
        self.main_window.set_autoconnect(self.profile, active)
        return False

    def _on_action_clicked(self, _btn: Gtk.Button) -> None:
        self.main_window.activate_profile(self.profile)

    def on_button_release(self, _widget: Gtk.Widget, event: Gdk.EventButton) -> bool:
        if event.button == 3:
            # Already handled on press; consume the release as well.
            return True
        if event.button == 1:
            if is_instance_or_ancestor(
                Gtk.get_event_widget(event), (Gtk.Button, Gtk.Switch)
            ):
                return False
            self.main_window.activate_profile(self.profile)
            return True
        return False

    def build_menu(self) -> Gtk.Menu:
        menu = Gtk.Menu()
        p = self.profile
        main = self.main_window

        def add_item(label: str, callback: Callable[[], None], danger: bool = False) -> None:
            item = Gtk.MenuItem(label=label)
            if danger:
                item.get_style_context().add_class("menu-danger")
            item.connect("activate", lambda _mi: callback())
            menu.append(item)

        add_item(
            "Disconnect" if p.active else "Connect",
            lambda: main.activate_profile(p),
        )
        add_item("Edit\u2026", lambda: main.edit_profile(p))
        add_item("Clone\u2026", lambda: main.clone_profile(p))

        auto_item = Gtk.CheckMenuItem(label="Connect automatically")
        auto_item.set_active(p.autoconnect)
        auto_item.connect("activate", lambda mi: main.set_autoconnect(p, mi.get_active()))
        menu.append(auto_item)

        if "vpn" in p.ctype.lower():
            add_item("Export\u2026", lambda: main.export_profile(p))

        menu.append(Gtk.SeparatorMenuItem())
        add_item("Delete\u2026", lambda: main.delete_profile(p), danger=True)
        menu.show_all()
        return menu


class HiddenNetworkRow(Gtk.EventBox):
    """Pseudo-row that opens the hidden-network dialog."""

    def __init__(self, main_window: "NetworkOverview") -> None:
        super().__init__()
        self.main_window = main_window
        self.set_visible_window(True)
        self.set_can_focus(False)
        self.connect("button-release-event", self.on_button_release)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.get_style_context().add_class("row-card")
        icon = Gtk.Image()
        icon.set_from_icon_name(
            main_window.icon_provider.action_icon(["list-add-symbolic", "list-add"]),
            Gtk.IconSize.MENU,
        )
        icon.set_pixel_size(main_window.icon_pixel_size)
        label = Gtk.Label(label="Connect to a hidden network\u2026")
        label.set_halign(Gtk.Align.START)
        label.get_style_context().add_class("row-title")
        box.pack_start(icon, False, False, 0)
        box.pack_start(label, True, True, 0)
        self.add(box)

    def on_button_release(self, _widget: Gtk.Widget, event: Gdk.EventButton) -> bool:
        if event.button == 1:
            self.main_window.connect_hidden_network()
            return True
        return False


# =============================================================================
# Main window
# =============================================================================
class NetworkOverview(Gtk.Window):
    def __init__(self, single_instance: SingleInstance) -> None:
        super().__init__(title=WINDOW_TITLE)
        self.single_instance = single_instance

        self.set_decorated(False)
        self.set_resizable(True)
        self.set_skip_taskbar_hint(False)
        self.set_skip_pager_hint(False)

        # State flags.
        self._closed = False
        self._pending_mutations = 0
        self._sync_in_flight = False
        self._sync_again = False
        self._sync_source = 0
        self._poll_source = 0
        self._ipc_watch_source = 0
        self._fade_source = 0
        self._fade_step = 0
        self._status_source = 0
        self._status_override: Optional[Tuple[str, bool]] = None
        self._last_state: Optional[OverviewState] = None
        self.nm_available = True
        self._search_text = ""
        self._type_filter = "All"

        # UI metrics (DPI-aware, computed once).
        self.icon_pixel_size = self._compute_icon_pixel_size()

        self.icon_provider = IconProvider()
        self._ipc_worker = IPCWorker()
        self._ipc_worker.start()
        self._monitor_worker: Optional[NmcliMonitorWorker] = None

        self.device_rows: Dict[str, DeviceRow] = {}
        self.ap_rows: Dict[str, AccessPointRow] = {}
        self.conn_rows: Dict[str, ConnectionRow] = {}

        self._setup_window_backend()
        self._load_css_provider()
        self._setup_ui()
        self._setup_instance_socket_watch()
        self._start_monitor_worker()

        self.connect("destroy", self._on_destroy)
        self.connect("key-press-event", self.on_key_press)

        self.show_all()
        self.present()
        GLib.idle_add(self._center_window_idle)
        self._update_hint()
        self.request_sync(immediate=True)
        self._schedule_poll(self._current_poll_interval())

        # Fade-in animation.
        self.set_opacity(0.0)
        self._fade_source = GLib.timeout_add(FADE_IN_STEP_MS, self._fade_in_step)

    # -------------------------------------------------------------------------
    # Metrics / CSS
    # -------------------------------------------------------------------------
    def _compute_icon_pixel_size(self) -> int:
        """Compute icon pixel size from DPI for a natural physical size."""
        dpi = 96.0
        screen = self.get_screen()
        if screen is None:
            screen = Gdk.Screen.get_default()
        if screen is not None:
            try:
                resolution = screen.get_resolution()
                if resolution and resolution > 0:
                    dpi = float(resolution)
            except Exception:
                dpi = 96.0
        scale = max(1.0, min(2.0, dpi / 96.0))
        size = int(22 * scale)
        return max(16, min(48, size))

    def _load_css_provider(self) -> None:
        screen = Gdk.Screen.get_default()
        if screen is None:
            logger.error("Cannot get default Gdk.Screen; skipping CSS.")
            return
        provider = Gtk.CssProvider()
        try:
            provider.load_from_data(CSS_TEXT.encode("utf-8"))
        except Exception as exc:
            logger.error("Cannot load CSS: %s", exc)
            return
        if getattr(self, "_css_provider", None) is not None:
            try:
                Gtk.StyleContext.remove_provider_for_screen(screen, self._css_provider)
            except Exception as exc:
                logger.debug("Cannot remove old CSS provider: %s", exc)
        try:
            Gtk.StyleContext.add_provider_for_screen(
                screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
            self._css_provider = provider
        except Exception as exc:
            logger.error("Cannot add CSS provider: %s", exc)

    # -------------------------------------------------------------------------
    # Setup
    # -------------------------------------------------------------------------
    def _setup_window_backend(self) -> None:
        self.set_title(WINDOW_TITLE)
        try:
            self.set_role(WINDOW_ROLE)
        except Exception as exc:
            logger.debug("Cannot set window role: %s", exc)
        try:
            self.set_wmclass(APP_NAME, APP_NAME)
        except Exception as exc:
            logger.debug("Cannot set WM class: %s", exc)
        try:
            GLib.set_application_name(APP_NAME)
            GLib.set_prgname(APP_NAME)
        except Exception as exc:
            logger.debug("Cannot set GLib application name: %s", exc)

        self.set_type_hint(Gdk.WindowTypeHint.NORMAL)
        self.set_modal(False)
        self.set_keep_above(False)
        self.set_accept_focus(True)
        self.set_focus_on_map(True)
        self.set_position(Gtk.WindowPosition.CENTER)

        default_w, default_h = 920, 740
        screen = self.get_screen()
        if screen is not None:
            rgba = screen.get_rgba_visual()
            if rgba is not None:
                try:
                    self.set_visual(rgba)
                except Exception as exc:
                    logger.debug("Cannot set RGBA visual: %s", exc)
            if screen.get_n_monitors() > 0:
                monitor = screen.get_primary_monitor()
                if monitor < 0 or monitor >= screen.get_n_monitors():
                    monitor = 0
                geom = screen.get_monitor_geometry(monitor)
                default_w = max(720, min(1080, geom.width - 140))
                default_h = max(540, min(860, geom.height - 140))
        self.set_default_size(default_w, default_h)
        self.set_size_request(680, 480)

    def _setup_ui(self) -> None:
        self.bg_eventbox = Gtk.EventBox()
        self.bg_eventbox.set_visible_window(True)
        self.bg_eventbox.set_can_focus(False)
        self.bg_eventbox.get_style_context().add_class("bg-overlay")
        # Background click intentionally does nothing.
        self.add(self.bg_eventbox)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.get_style_context().add_class("outer-box")
        self.bg_eventbox.add(outer)

        outer.pack_start(self._build_header(), False, False, 0)

        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroll.set_shadow_type(Gtk.ShadowType.NONE)
        self.scroll.set_vexpand(True)
        try:
            self.scroll.set_overlay_scrolling(True)
        except Exception:
            pass

        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.content.get_style_context().add_class("content-box")
        self.scroll.add(self.content)
        outer.pack_start(self.scroll, True, True, 0)

        self._build_sections()

        self.hint_label = Gtk.Label()
        self.hint_label.get_style_context().add_class("hint-label")
        self.hint_label.set_halign(Gtk.Align.CENTER)
        self.hint_label.set_justify(Gtk.Justification.CENTER)
        self.hint_label.set_max_width_chars(130)
        self.hint_label.set_line_wrap(True)
        outer.pack_start(self.hint_label, False, False, 0)

    def _build_header(self) -> Gtk.Widget:
        header = Gtk.EventBox()
        header.set_visible_window(True)
        header.get_style_context().add_class("header")
        header.connect("button-press-event", self._on_header_press)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        # Gtk.EventBox does not apply CSS padding to its child layout, so
        # the breathing room between the header content and the border is
        # provided by real margins on this inner box. This keeps the title
        # text and the buttons comfortably off the header border.
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(16)
        box.set_margin_end(16)
        header.add(box)

        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title_box.set_hexpand(True)
        title = Gtk.Label(label=WINDOW_TITLE)
        title.set_halign(Gtk.Align.START)
        title.get_style_context().add_class("title-label")
        self.subtitle = Gtk.Label(label="Starting\u2026")
        self.subtitle.set_halign(Gtk.Align.START)
        self.subtitle.get_style_context().add_class("subtitle-label")
        title_box.pack_start(title, False, False, 0)
        title_box.pack_start(self.subtitle, False, False, 0)
        box.pack_start(title_box, True, True, 0)

        wifi_label = Gtk.Label(label="Wi-Fi")
        wifi_label.get_style_context().add_class("subtitle-label")
        box.pack_start(wifi_label, False, False, 0)

        self.wifi_switch = Gtk.Switch()
        self.wifi_switch.set_valign(Gtk.Align.CENTER)
        self.wifi_switch.set_tooltip_text("Toggle Wi-Fi radio")
        self._wifi_hid = self.wifi_switch.connect("state-set", self._on_wifi_switch)
        box.pack_start(self.wifi_switch, False, False, 0)

        self.scan_btn = Gtk.Button(label="Scan")
        self.scan_btn.set_tooltip_text("Rescan Wi-Fi networks (S)")
        self.scan_btn.connect("clicked", lambda _b: self.scan_now())
        box.pack_start(self.scan_btn, False, False, 0)

        close_btn = Gtk.Button()
        close_icon = Gtk.Image()
        close_icon.set_from_icon_name(
            self.icon_provider.action_icon(["window-close-symbolic", "window-close"]),
            Gtk.IconSize.MENU,
        )
        close_btn.add(close_icon)
        close_btn.set_tooltip_text("Close (ESC)")
        close_btn.connect("clicked", lambda _b: self.close_app())
        box.pack_start(close_btn, False, False, 0)
        return header

    def _build_sections(self) -> None:
        # --- Devices ---
        self._section_label(self.content, "Devices")
        self.devices_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.content.pack_start(self.devices_box, False, False, 0)
        self.devices_empty = self._empty_label(self.content, "No network devices found.")

        # --- Wi-Fi networks ---
        self._section_label(self.content, "Wi-Fi Networks")
        self.ap_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.content.pack_start(self.ap_box, False, False, 0)
        self.aps_empty = self._empty_label(
            self.content, "No Wi-Fi networks found. Press S or use Scan to rescan."
        )
        hidden_row = HiddenNetworkRow(self)
        self.content.pack_start(hidden_row, False, False, 0)

        # --- Connections ---
        self._section_label(self.content, "Connections")

        filter_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Search connections\u2026")
        self.search.set_hexpand(True)
        self.search.connect("search-changed", self._on_search_changed)
        filter_bar.pack_start(self.search, True, True, 0)

        self.type_combo = Gtk.ComboBoxText()
        for label in TYPE_FILTERS.keys():
            self.type_combo.append_text(label)
        self.type_combo.set_active(0)
        self.type_combo.connect("changed", self._on_type_changed)
        filter_bar.pack_start(self.type_combo, False, False, 0)

        new_btn = Gtk.Button(label="+ New")
        new_btn.get_style_context().add_class("btn-primary")
        new_btn.set_tooltip_text("Create a new connection")
        new_btn.connect("clicked", lambda _b: self.create_connection())
        filter_bar.pack_start(new_btn, False, False, 0)

        import_btn = Gtk.Button(label="Import VPN\u2026")
        import_btn.set_tooltip_text("Import a VPN configuration file")
        import_btn.connect("clicked", lambda _b: self.import_vpn())
        filter_bar.pack_start(import_btn, False, False, 0)

        reload_btn = Gtk.Button(label="Reload")
        reload_btn.set_tooltip_text("Reload connection files from disk")
        reload_btn.connect("clicked", lambda _b: self.reload_connections())
        filter_bar.pack_start(reload_btn, False, False, 0)

        self.content.pack_start(filter_bar, False, False, 6)

        self.conn_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.content.pack_start(self.conn_box, False, False, 0)
        self.conn_empty = self._empty_label(
            self.content, "No connection profiles match the current filter."
        )

    def _section_label(self, parent: Gtk.Box, text: str) -> Gtk.Label:
        label = Gtk.Label(label=text)
        label.set_halign(Gtk.Align.START)
        label.get_style_context().add_class("section-label")
        parent.pack_start(label, False, False, 0)
        return label

    def _empty_label(self, parent: Gtk.Box, text: str) -> Gtk.Label:
        label = Gtk.Label(label=text)
        label.set_halign(Gtk.Align.START)
        label.get_style_context().add_class("empty-label")
        label.set_no_show_all(True)
        label.hide()
        parent.pack_start(label, False, False, 0)
        return label

    def _setup_instance_socket_watch(self) -> None:
        if self.single_instance.server_socket is None:
            return
        try:
            self._ipc_watch_source = GLib.io_add_watch(
                self.single_instance.server_socket.fileno(),
                GLib.IOCondition.IN,
                self._on_instance_message,
            )
        except Exception as exc:
            logger.error("Cannot watch single-instance socket: %s", exc)

    def _start_monitor_worker(self) -> None:
        if self._closed or self._monitor_worker is not None:
            return
        worker = NmcliMonitorWorker(self._notify_monitor_event)
        worker.start()
        self._monitor_worker = worker

    def _stop_monitor_worker(self) -> None:
        if self._monitor_worker is not None:
            try:
                self._monitor_worker.stop()
            except Exception as exc:
                logger.debug("Cannot stop monitor worker: %s", exc)
            self._monitor_worker = None

    # -------------------------------------------------------------------------
    # Animation / placement
    # -------------------------------------------------------------------------
    def _fade_in_step(self) -> bool:
        if self._closed:
            return False
        self._fade_step += 1
        opacity = self._fade_step / float(FADE_IN_STEPS)
        if opacity >= 1.0:
            self.set_opacity(1.0)
            self._fade_source = 0
            return False
        self.set_opacity(opacity)
        return True

    def _center_window_idle(self) -> bool:
        if self._closed:
            return False
        self._center_window()
        return False

    def _center_window(self) -> None:
        screen = self.get_screen()
        if screen is None or screen.get_n_monitors() <= 0:
            return
        monitor = screen.get_primary_monitor()
        if monitor < 0 or monitor >= screen.get_n_monitors():
            monitor = 0
        geom = screen.get_monitor_geometry(monitor)
        width, height = self.get_size()
        x = geom.x + max(0, (geom.width - width) // 2)
        y = geom.y + max(0, (geom.height - height) // 2)
        try:
            self.move(x, y)
        except Exception as exc:
            logger.debug("Cannot center window: %s", exc)

    def _on_header_press(
        self, _widget: Gtk.Widget, event: Gdk.EventButton
    ) -> bool:
        """Allow dragging the undecorated window by its header."""
        if event.button != 1:
            return False
        if is_instance_or_ancestor(Gtk.get_event_widget(event), (Gtk.Button, Gtk.Switch)):
            return False
        self.begin_move_drag(event.button, event.x_root, event.y_root, event.time)
        return True

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------
    def _on_destroy(self, _widget: Gtk.Widget) -> None:
        self.close_app()

    def close_app(self) -> None:
        if self._closed:
            return
        self._closed = True
        for source_attr in (
            "_sync_source",
            "_poll_source",
            "_ipc_watch_source",
            "_fade_source",
            "_status_source",
        ):
            source = getattr(self, source_attr, 0)
            if source:
                try:
                    GLib.source_remove(source)
                except Exception as exc:
                    logger.debug("Cannot remove GLib source %s: %s", source_attr, exc)
                setattr(self, source_attr, 0)
        self._stop_monitor_worker()
        try:
            self._ipc_worker.stop()
        except Exception as exc:
            logger.debug("Cannot stop IPC worker: %s", exc)
        try:
            self.single_instance.release()
        except Exception as exc:
            logger.debug("Cannot release single-instance resources: %s", exc)
        css_provider = getattr(self, "_css_provider", None)
        if css_provider is not None:
            screen = Gdk.Screen.get_default()
            if screen is not None:
                try:
                    Gtk.StyleContext.remove_provider_for_screen(screen, css_provider)
                except Exception as exc:
                    logger.debug("Cannot remove CSS provider: %s", exc)
        try:
            self.hide()
        except Exception as exc:
            logger.debug("Cannot hide window before quit: %s", exc)
        Gtk.main_quit()

    # -------------------------------------------------------------------------
    # Monitor events
    # -------------------------------------------------------------------------
    def _notify_monitor_event(self) -> None:
        """Called from the monitor worker thread."""
        if self._closed:
            return
        try:
            GLib.idle_add(self._on_monitor_event)
        except Exception as exc:
            logger.debug("Cannot schedule monitor event sync: %s", exc)

    def _on_monitor_event(self) -> bool:
        if self._closed:
            return False
        self.request_sync(delay_ms=SYNC_DEBOUNCE_MS)
        return False

    # -------------------------------------------------------------------------
    # Single-instance IPC
    # -------------------------------------------------------------------------
    def _on_instance_message(self, _fd: int, _condition: GLib.IOCondition) -> bool:
        if self._closed or self.single_instance.server_socket is None:
            return False
        while True:
            try:
                conn, _ = self.single_instance.server_socket.accept()
            except BlockingIOError:
                break
            except OSError as exc:
                logger.debug("Cannot accept instance connection: %s", exc)
                break
            try:
                conn.settimeout(0.2)
                raw = conn.recv(64)
            except OSError:
                raw = b""
            message = raw.decode("utf-8", "ignore").strip()
            if message:
                try:
                    conn.sendall(b"ok")
                except OSError:
                    pass
                if message == IPC_MESSAGE_SHOW:
                    GLib.idle_add(self._bring_to_front)
                elif message == IPC_MESSAGE_QUIT:
                    GLib.idle_add(self.close_app)
            try:
                conn.close()
            except OSError:
                pass
        return True

    def _bring_to_front(self) -> bool:
        if self._closed:
            return False
        self.show()
        self.present()
        self.request_sync(immediate=True)
        return False

    # -------------------------------------------------------------------------
    # Polling & sync
    # -------------------------------------------------------------------------
    def _current_poll_interval(self) -> int:
        return POLL_INTERVAL_MS if self.nm_available else POLL_INTERVAL_NM_DOWN_MS

    def _schedule_poll(self, interval_ms: int) -> None:
        if self._closed:
            return
        if self._poll_source:
            try:
                GLib.source_remove(self._poll_source)
            except Exception as exc:
                logger.debug("Cannot remove old poll source: %s", exc)
        self._poll_source = GLib.timeout_add(interval_ms, self._on_poll)

    def _on_poll(self) -> bool:
        if self._closed:
            return False
        self._poll_source = 0
        if self._pending_mutations == 0 and not self._sync_in_flight:
            self.request_sync(delay_ms=0)
        self._schedule_poll(self._current_poll_interval())
        return False

    def request_sync(
        self,
        delay_ms: Optional[int] = None,
        immediate: bool = False,
    ) -> None:
        if self._closed:
            return
        if delay_ms is None:
            delay_ms = SYNC_DEBOUNCE_MS
        if self._sync_source:
            try:
                GLib.source_remove(self._sync_source)
            except Exception as exc:
                logger.debug("Cannot remove old sync source: %s", exc)
            self._sync_source = 0
        if immediate or delay_ms <= 0:
            self._sync_source = GLib.idle_add(self._start_sync)
        else:
            self._sync_source = GLib.timeout_add(delay_ms, self._start_sync)

    def _start_sync(self) -> bool:
        if self._closed:
            return False
        self._sync_source = 0
        self._begin_async_sync()
        return False

    def _begin_async_sync(self) -> None:
        if self._closed:
            return
        if self._sync_in_flight:
            self._sync_again = True
            return
        if self._pending_mutations > 0:
            return
        self._sync_in_flight = True
        self._ipc_worker.submit(self._fetch_state_task)

    def _fetch_state_task(self) -> None:
        if self._closed:
            return
        state: Optional[OverviewState] = None
        try:
            state = NmcliBackend.get_overview_state()
        except Exception as exc:
            logger.error("Cannot fetch overview state: %s", exc)
        finally:
            if not self._closed:
                GLib.idle_add(self._apply_state, state)

    def _apply_state(self, state: Optional[OverviewState]) -> bool:
        if self._closed:
            return False
        self._sync_in_flight = False
        if state is not None:
            self._update_ui(state)
        else:
            self.nm_available = False
            self._update_hint()
        if self._sync_again:
            self._sync_again = False
            self.request_sync(delay_ms=80)
        return False

    # -------------------------------------------------------------------------
    # UI updates
    # -------------------------------------------------------------------------
    def _sync_container(
        self,
        container: Gtk.Box,
        widgets: Dict[str, Any],
        items: List[Any],
        key_of: Callable[[Any], str],
        make_row: Callable[[Any], Gtk.Widget],
        empty_label: Optional[Gtk.Label] = None,
    ) -> None:
        """Diff-based container synchronization (no flicker, no rebuilds)."""
        new_keys = [key_of(it) for it in items]
        keyset = set(new_keys)
        for dead in set(widgets) - keyset:
            widget = widgets.pop(dead)
            try:
                container.remove(widget)
                widget.destroy()
            except Exception as exc:
                logger.debug("Cannot destroy row: %s", exc)
        for item in items:
            key = key_of(item)
            widget = widgets.get(key)
            if widget is None:
                widget = make_row(item)
                widgets[key] = widget
                container.pack_start(widget, False, False, 0)
                widget.show_all()
            else:
                widget.update(item)
        for index, item in enumerate(items):
            widget = widgets[key_of(item)]
            try:
                container.reorder_child(widget, index)
            except Exception as exc:
                logger.debug("Cannot reorder row: %s", exc)
        if empty_label is not None:
            empty_label.set_visible(not items)

    def _matches_filter(self, profile: ConnectionProfile) -> bool:
        tokens = TYPE_FILTERS.get(self._type_filter)
        if tokens is not None and not any(t in profile.ctype.lower() for t in tokens):
            return False
        if self._search_text:
            hay = f"{profile.name} {profile.ssid} {profile.ctype}".lower()
            if self._search_text not in hay:
                return False
        return True

    def _update_ui(self, state: OverviewState) -> None:
        if self._closed:
            return
        self._last_state = state
        self.nm_available = state.nm_available

        if state.general is not None:
            self.subtitle.set_text(
                f"{state.general.state.capitalize()} \u2022 "
                f"Connectivity: {state.general.connectivity or 'unknown'} \u2022 "
                f"Wi-Fi radio: {'on' if state.wifi_enabled else 'off'}"
            )
        else:
            self.subtitle.set_text("NetworkManager unavailable")

        self.wifi_switch.handler_block(self._wifi_hid)
        self.wifi_switch.set_active(state.wifi_enabled)
        self.wifi_switch.handler_unblock(self._wifi_hid)

        devices = sorted(state.devices, key=lambda d: d.device)
        self._sync_container(
            self.devices_box,
            self.device_rows,
            devices,
            lambda d: d.device,
            lambda d: DeviceRow(d, self),
            self.devices_empty,
        )

        aps = sorted(
            state.access_points,
            key=lambda a: (not a.in_use, -a.signal, (a.ssid or "~").lower()),
        )
        self._sync_container(
            self.ap_box,
            self.ap_rows,
            aps,
            lambda a: a.key,
            lambda a: AccessPointRow(a, self),
            self.aps_empty,
        )
        if not state.wifi_enabled and self.nm_available:
            self.aps_empty.set_text("Wi-Fi is disabled. Enable it with the switch above.")
        else:
            self.aps_empty.set_text(
                "No Wi-Fi networks found. Press S or use Scan to rescan."
            )

        visible = [p for p in state.profiles if self._matches_filter(p)]
        visible.sort(key=lambda p: (not p.active, p.name.lower()))
        self._sync_container(
            self.conn_box,
            self.conn_rows,
            visible,
            lambda p: p.uuid,
            lambda p: ConnectionRow(p, self),
            self.conn_empty,
        )
        self._update_hint()

    def _update_hint(self) -> None:
        if self._closed:
            return
        ctx = self.hint_label.get_style_context()
        for cls in ("hint-error", "hint-ok"):
            ctx.remove_class(cls)
        if self._status_override is not None:
            text, ok = self._status_override
            ctx.add_class("hint-ok" if ok else "hint-error")
        elif not self.nm_available:
            text = (
                "`nmcli` is unavailable or NetworkManager is not running \u2014 "
                "check that NetworkManager is active and `nmcli` is in PATH"
            )
            ctx.add_class("hint-error")
        else:
            text = (
                "Click a network to connect \u2022 Right-click a connection for "
                "more actions \u2022 Keys: S scan \u00b7 F5 refresh \u00b7 ESC close"
            )
        self.hint_label.set_text(text)

    def flash_status(self, text: str, ok: bool = True) -> None:
        """Show a transient status message in the hint label."""
        self._status_override = (text, ok)
        self._update_hint()
        if self._status_source:
            try:
                GLib.source_remove(self._status_source)
            except Exception:
                pass
        self._status_source = GLib.timeout_add(STATUS_FLASH_MS, self._clear_status)

    def _clear_status(self) -> bool:
        self._status_override = None
        self._status_source = 0
        self._update_hint()
        return False

    # -------------------------------------------------------------------------
    # Filter events
    # -------------------------------------------------------------------------
    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self._search_text = entry.get_text().strip().lower()
        self.request_sync(delay_ms=120)

    def _on_type_changed(self, combo: Gtk.ComboBoxText) -> None:
        self._type_filter = combo.get_active_text() or "All"
        self.request_sync(delay_ms=120)

    def _on_wifi_switch(self, _switch: Gtk.Switch, active: bool) -> bool:
        self._run_task(
            lambda: NmcliBackend.set_wifi_radio(active),
            on_done=lambda res: self._flash_result(
                res,
                f"Wi-Fi radio {'enabled' if active else 'disabled'}",
                "Could not toggle Wi-Fi radio",
            ),
        )
        return False

    # -------------------------------------------------------------------------
    # Task runner
    # -------------------------------------------------------------------------
    def _run_task(self, fn: Callable[[], Any], on_done: Optional[Callable[[Any], None]] = None) -> None:
        """Run fn() in the worker thread; deliver the result on the UI thread."""
        if self._closed:
            return
        self._pending_mutations += 1

        def task() -> None:
            try:
                result = fn()
            except Exception as exc:
                logger.error("Task failed: %s", exc)
                result = (False, str(exc))
            finally:
                if not self._closed:
                    GLib.idle_add(self._on_task_done, on_done, result)

        self._ipc_worker.submit(task)

    def _on_task_done(self, on_done: Optional[Callable[[Any], None]], result: Any) -> bool:
        if self._closed:
            return False
        if self._pending_mutations > 0:
            self._pending_mutations -= 1
        self.request_sync(delay_ms=SYNC_AFTER_ACTION_MS)
        if on_done is not None:
            try:
                on_done(result)
            except Exception as exc:
                logger.error("Task completion handler failed: %s", exc)
        return False

    def _flash_result(self, result: Any, ok_text: str, err_prefix: str) -> None:
        if not isinstance(result, tuple) or len(result) != 2:
            return
        ok, err = result
        if ok:
            self.flash_status(ok_text, ok=True)
        else:
            self.flash_status(f"{err_prefix}: {err}" if err else err_prefix, ok=False)

    # -------------------------------------------------------------------------
    # User actions
    # -------------------------------------------------------------------------
    def _find_wifi_profile(self, ssid: str) -> Optional[ConnectionProfile]:
        """Find an existing Wi-Fi profile by SSID, falling back to name."""
        if self._last_state is None or not ssid:
            return None
        for p in self._last_state.profiles:
            if "wireless" not in p.ctype.lower():
                continue
            if p.ssid == ssid or p.name == ssid:
                return p
        return None

    def _best_bssid_for(self, ssid: str) -> Optional[str]:
        if self._last_state is None or not ssid:
            return None
        best: Optional[AccessPoint] = None
        for ap in self._last_state.access_points:
            if ap.ssid == ssid and (best is None or ap.signal > best.signal):
                best = ap
        return best.bssid if best else None

    def activate_profile(self, profile: ConnectionProfile) -> None:
        if profile.active:
            self.disconnect_profile(profile)
        else:
            self.connect_profile(profile)

    def connect_profile(self, profile: ConnectionProfile) -> None:
        bssid = (
            self._best_bssid_for(profile.ssid)
            if "wireless" in profile.ctype.lower()
            else None
        )
        self._run_task(
            lambda: NmcliBackend.connect_profile(
                profile.uuid, ifname=profile.device, ap=bssid
            ),
            on_done=lambda res: self._on_profile_connect_done(res, profile),
        )

    def _on_profile_connect_done(self, result: Any, profile: ConnectionProfile) -> None:
        ok, err = result if isinstance(result, tuple) else (False, "unknown error")
        if ok:
            self.flash_status(f"Connected to \u201c{profile.name}\u201d", ok=True)
            return
        low = (err or "").lower()
        is_wifi = "wireless" in profile.ctype.lower()
        if is_wifi and ("secret" in low or "password" in low or "psk" in low or "802.1x" in low):
            self._prompt_and_retry(profile)
        else:
            self.flash_status(
                f"Could not activate \u201c{profile.name}\u201d: {err}", ok=False
            )

    def _prompt_and_retry(self, profile: ConnectionProfile) -> None:
        km = "wpa-psk"
        if self._last_state is not None:
            for ap in self._last_state.access_points:
                if ap.ssid == profile.ssid:
                    km = key_mgmt_for_security(ap.security) or km
                    break
        dlg = PasswordDialog(self, profile.ssid or profile.name, km)
        response = dlg.run()
        values = dlg.get_values() if response == Gtk.ResponseType.OK else None
        dlg.destroy()
        if values is None:
            return
        bssid = self._best_bssid_for(profile.ssid)
        self._run_task(
            lambda: NmcliBackend.connect_wifi(
                profile.ssid or profile.name,
                values["password"],
                km,
                values["identity"],
                bssid,
                False,
                values["save"],
                profile.uuid,
            ),
            on_done=lambda res: self._flash_result(
                res,
                f"Connected to \u201c{profile.name}\u201d",
                f"Could not activate \u201c{profile.name}\u201d",
            ),
        )

    def disconnect_profile(self, profile: ConnectionProfile) -> None:
        self._run_task(
            lambda: NmcliBackend.disconnect_profile(profile.uuid),
            on_done=lambda res: self._flash_result(
                res,
                f"Disconnected \u201c{profile.name}\u201d",
                f"Could not disconnect \u201c{profile.name}\u201d",
            ),
        )

    def connect_to_ap(self, ap: AccessPoint) -> None:
        if not ap.ssid:
            self.connect_hidden_network()
            return
        km = key_mgmt_for_security(ap.security)
        existing = self._find_wifi_profile(ap.ssid)

        if existing is not None and km != "wpa-eap":
            # Try stored credentials first; prompt only when NM asks for secrets.
            self._run_task(
                lambda: NmcliBackend.connect_profile(existing.uuid, ap=ap.bssid),
                on_done=lambda res: self._on_ap_connect_done(res, ap, existing),
            )
            return

        dlg = PasswordDialog(self, ap.ssid, km)
        response = dlg.run()
        values = dlg.get_values() if response == Gtk.ResponseType.OK else None
        dlg.destroy()
        if values is None:
            return
        self._run_task(
            lambda: NmcliBackend.connect_wifi(
                ap.ssid,
                values["password"],
                km,
                values["identity"],
                ap.bssid,
                False,
                values["save"],
                existing.uuid if existing else None,
            ),
            on_done=lambda res: self._flash_result(
                res,
                f"Connected to \u201c{ap.ssid}\u201d",
                f"Could not connect to \u201c{ap.ssid}\u201d",
            ),
        )

    def _on_ap_connect_done(
        self, result: Any, ap: AccessPoint, existing: ConnectionProfile
    ) -> None:
        ok, err = result if isinstance(result, tuple) else (False, "unknown error")
        if ok:
            self.flash_status(f"Connected to \u201c{ap.ssid}\u201d", ok=True)
            return
        low = (err or "").lower()
        if "secret" in low or "password" in low or "psk" in low or "802.1x" in low:
            self._prompt_and_retry(existing)
        else:
            self.flash_status(
                f"Could not connect to \u201c{ap.ssid}\u201d: {err}", ok=False
            )

    def connect_hidden_network(self) -> None:
        dlg = HiddenNetworkDialog(self)
        response = dlg.run()
        values = dlg.get_values() if response == Gtk.ResponseType.OK else None
        dlg.destroy()
        if values is None or not values["ssid"]:
            return
        existing = self._find_wifi_profile(values["ssid"])
        self._run_task(
            lambda: NmcliBackend.connect_wifi(
                values["ssid"],
                values["password"],
                values["key_mgmt"],
                "",
                None,
                True,
                values["save"],
                existing.uuid if existing else None,
            ),
            on_done=lambda res: self._flash_result(
                res,
                f"Connected to \u201c{values['ssid']}\u201d",
                f"Could not connect to \u201c{values['ssid']}\u201d",
            ),
        )

    def set_autoconnect(self, profile: ConnectionProfile, on: bool) -> None:
        self._run_task(
            lambda: NmcliBackend.set_autoconnect(profile.uuid, on),
            on_done=lambda res: self._flash_result(
                res,
                f"Autoconnect {'enabled' if on else 'disabled'} for \u201c{profile.name}\u201d",
                f"Could not change autoconnect for \u201c{profile.name}\u201d",
            ),
        )

    def edit_profile(self, profile: ConnectionProfile) -> None:
        self._run_task(
            lambda: NmcliBackend.get_profile_details(profile.uuid),
            on_done=lambda res: self._open_edit_dialog(profile, res),
        )

    def _open_edit_dialog(self, profile: ConnectionProfile, details: Any) -> None:
        if not isinstance(details, dict):
            self.flash_status(f"Could not read \u201c{profile.name}\u201d", ok=False)
            return
        dlg = EditConnectionDialog(self, profile, details)
        response = dlg.run()
        pairs = dlg.get_pairs() if response == Gtk.ResponseType.OK else None
        dlg.destroy()
        if pairs is None:
            return
        self._run_task(
            lambda: NmcliBackend.modify_profile(profile.uuid, pairs),
            on_done=lambda res: self._flash_result(
                res,
                f"Saved changes to \u201c{profile.name}\u201d",
                f"Could not save \u201c{profile.name}\u201d",
            ),
        )

    def clone_profile(self, profile: ConnectionProfile) -> None:
        dlg = NameDialog(self, "Clone Connection", f"{profile.name} copy")
        response = dlg.run()
        name = dlg.get_value() if response == Gtk.ResponseType.OK else ""
        dlg.destroy()
        if not name:
            return
        self._run_task(
            lambda: NmcliBackend.clone_profile(profile.uuid, name),
            on_done=lambda res: self._flash_result(
                res, f"Cloned as \u201c{name}\u201d", "Could not clone connection"
            ),
        )

    def delete_profile(self, profile: ConnectionProfile) -> None:
        if not confirm_yes_no(
            self,
            f"Delete \u201c{profile.name}\u201d?",
            "The connection profile will be removed permanently from disk.",
        ):
            return
        self._run_task(
            lambda: NmcliBackend.delete_profile(profile.uuid),
            on_done=lambda res: self._flash_result(
                res,
                f"Deleted \u201c{profile.name}\u201d (network forgotten)",
                f"Could not delete \u201c{profile.name}\u201d",
            ),
        )

    def export_profile(self, profile: ConnectionProfile) -> None:
        path = choose_save_file(self, "Export VPN Configuration", f"{profile.name}.conf")
        if not path:
            return
        self._run_task(
            lambda: NmcliBackend.export_vpn(profile.uuid, path),
            on_done=lambda res: self._flash_result(
                res, f"Exported to {path}", "Could not export connection"
            ),
        )

    def import_vpn(self) -> None:
        dlg = ImportVpnDialog(self)
        response = dlg.run()
        vtype, path = dlg.get_values() if response == Gtk.ResponseType.OK else ("", None)
        dlg.destroy()
        if not path:
            return
        self._run_task(
            lambda: NmcliBackend.import_vpn(vtype, path),
            on_done=lambda res: self._flash_result(
                res, f"Imported {os.path.basename(path)}", "Could not import configuration"
            ),
        )

    def create_connection(self) -> None:
        eth_devices = []
        if self._last_state is not None:
            eth_devices = [d.device for d in self._last_state.devices
                           if d.dtype == "ethernet"]
        dlg = CreateConnectionDialog(self, eth_devices)
        response = dlg.run()
        values = dlg.get_values() if response == Gtk.ResponseType.OK else None
        dlg.destroy()
        if values is None:
            return

        ctype = values["ctype"]
        if ctype == "Wi-Fi":
            if not values["ssid"]:
                self.flash_status("SSID is required", ok=False)
                return
            self._run_task(
                lambda: NmcliBackend.connect_wifi(
                    values["ssid"],
                    values["password"],
                    values["key_mgmt"],
                    "",
                    None,
                    values["hidden"],
                    values["save"],
                    None,
                ),
                on_done=lambda res: self._flash_result(
                    res,
                    f"Created and connected \u201c{values['ssid']}\u201d",
                    "Could not create Wi-Fi connection",
                ),
            )
        elif ctype == "Ethernet":
            if not values["name"]:
                self.flash_status("A connection name is required", ok=False)
                return
            self._run_task(
                lambda: NmcliBackend.add_ethernet(
                    values["name"], values["ifname"], values["autoconnect"]
                ),
                on_done=lambda res: self._flash_result(
                    res,
                    f"Created \u201c{values['name']}\u201d",
                    "Could not create Ethernet connection",
                ),
            )
        else:
            missing = [
                k for k in ("private_key", "peer_key", "address")
                if not values.get(k)
            ]
            if not values.get("name") or missing:
                self.flash_status(
                    "Name, private key, peer key and address are required", ok=False
                )
                return
            self._run_task(
                lambda: NmcliBackend.add_wireguard(values),
                on_done=lambda res: self._flash_result(
                    res,
                    f"Created WireGuard profile \u201c{values['name']}\u201d",
                    "Could not create WireGuard connection",
                ),
            )

    def reload_connections(self) -> None:
        self._run_task(
            lambda: NmcliBackend.reload_connections(),
            on_done=lambda res: self._flash_result(
                res, "Connection files reloaded from disk", "Could not reload connections"
            ),
        )

    def scan_now(self) -> None:
        self.flash_status("Scanning for Wi-Fi networks\u2026", ok=True)

        def task() -> Tuple[bool, str]:
            NmcliBackend.rescan()
            time.sleep(SCAN_SETTLE_MS / 1000.0)
            return True, ""

        self._run_task(
            task,
            on_done=lambda _res: self.request_sync(immediate=True),
        )

    # -------------------------------------------------------------------------
    # Events
    # -------------------------------------------------------------------------
    def on_key_press(self, _widget: Gtk.Widget, event: Gdk.EventKey) -> bool:
        if event.keyval == Gdk.KEY_F5:
            self.request_sync(immediate=True)
            return True
        key = Gdk.keyval_to_lower(event.keyval)
        if key == Gdk.KEY_Escape:
            self.close_app()
            return True
        if event.state & (
            Gdk.ModifierType.CONTROL_MASK
            | Gdk.ModifierType.MOD1_MASK
            | Gdk.ModifierType.SUPER_MASK
        ):
            return False
        if key == Gdk.KEY_s:
            self.scan_now()
            return True
        return False


# =============================================================================
# Entry point
# =============================================================================
def _install_signal_handlers() -> None:
    def handler(signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def main() -> int:
    _install_signal_handlers()
    single = SingleInstance()
    if not single.ensure_single_instance():
        return 0
    app: Optional[NetworkOverview] = None
    try:
        app = NetworkOverview(single)
        Gtk.main()
    except KeyboardInterrupt:
        if app is not None:
            app.close_app()
    finally:
        single.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
