#!/usr/bin/env python3
#
# MangoWM Grid Overview Workspace
# Production-ready GTK3/PyGObject implementation.
#
# Runtime dependencies:
#   - Python 3.9+
#   - GTK3
#   - PyGObject
#   - MangoWM
#   - mmsg (MangoWM IPC client, must be available in PATH)
#   - A valid icon theme (Adwaita or any desktop icon theme)
#
# Package hints:
#   Arch:
#     sudo pacman -S python pygobject gtk3
#     # MangoWM/mmsg: install from MangoWM source/package
#   Fedora:
#     sudo dnf install python3-gobject gtk3
#     # MangoWM/mmsg: install from MangoWM source/package
#   Debian/Ubuntu:
#     sudo apt install python3-gi gir1.2-gtk-3.0
#     # MangoWM/mmsg: install from MangoWM source/package
#
# Main behavior:
#   - Shows 9 workspaces/tags in a 3x3 grid.
#   - Drag & drop only moves a client to another workspace.
#   - Drag & drop prefers MangoWM `tagsilent` to avoid focus/view flicker.
#   - Click app: switch to its workspace and focus it, but DO NOT exit.
#   - Click workspace:
#       * switch to that workspace,
#       * move the overview itself to that workspace,
#       * keep overview focused,
#       * DO NOT exit.
#   - ESC exits the overview.
#   - Global ESC:
#       * While this script is running, it attempts to install a temporary
#         MangoWM `bindp=NONE,Escape,...` binding.
#       * This allows ESC to close the overview even when another app is focused.
#       * The binding is removed when the script exits.
#
# Important mmsg syntax notes from MangoWM docs:
#   dispatch <func>[,arg...] [client,<id>]
#
#   Relevant dispatchers:
#     view <tag> [,synctag]
#     tag <tag> [,synctag]
#     tagsilent <tag>
#     focusid            (target via client,<id>)
#
# =============================================================================

from __future__ import annotations

import errno
import fcntl
import gi
import json
import logging
import os
import queue
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Pango

logging.basicConfig(
    level=logging.INFO,
    format="[GridOverview] %(levelname)s: %(message)s",
)
logger = logging.getLogger("GridOverview")


# =============================================================================
# Constants
# =============================================================================

NUM_WORKSPACES = 9
GRID_COLUMNS = 3
GRID_ROWS = (NUM_WORKSPACES + GRID_COLUMNS - 1) // GRID_COLUMNS

POLL_INTERVAL_MS = 2500
POLL_INTERVAL_IPC_DOWN_MS = 5000

SYNC_DEBOUNCE_MS = 160
WATCH_EVENT_SYNC_DEBOUNCE_MS = 120
SYNC_AFTER_DROP_MS = 140
SYNC_AFTER_DRAG_END_MS = 220

DRAG_RELEASE_SUPPRESS_SECONDS = 0.28
DRAG_FAILSAFE_SECONDS = 10

IPC_COMMAND_TIMEOUT = 0.90
IPC_DISPATCH_TIMEOUT = 0.60

MOVE_VERIFY_SLEEP_SECONDS = 0.015
MOVE_LATE_CHECK_SLEEP_SECONDS = 0.020
WATCH_RESTART_DELAY_SECONDS = 2.0

MAX_CACHED_ICONS = 512

APP_NAME = "mangowm-grid-overview"
WINDOW_TITLE = "MangoWM Grid Overview"
WINDOW_ROLE = "mangowm-grid-overview"

LOCK_FILE_NAME = "mangowm-grid-overview.lock"
SOCKET_FILE_NAME = "mangowm-grid-overview.sock"
GLOBAL_ESC_HELPER_FILE_NAME = "mangowm-grid-overview-esc.py"

IPC_MESSAGE_SHOW = "show"
IPC_MESSAGE_QUIT = "quit"

SILENT_MOVE_ENV_VAR = "GRID_OVERVIEW_SILENT_MOVE"

CLIENT_ID_KEYS = ["id", "client_id", "address", "window", "window_id"]
CLIENT_TITLE_KEYS = ["title", "name", "window_title"]
CLIENT_APP_KEYS = ["appid", "app_id", "class", "wm_class", "instance", "command"]
CLIENT_ROLE_KEYS = ["role", "window_role", "instance", "startup_id"]

GLOBAL_ESC_BEGIN_MARKER = "# mangowm-grid-overview-global-esc-begin"
GLOBAL_ESC_END_MARKER = "# mangowm-grid-overview-global-esc-end"

DND_TARGETS = [
    Gtk.TargetEntry.new("text/plain", 0, 0),
    Gtk.TargetEntry.new("STRING", 0, 0),
]

CSS = b"""
window {
    background-color: rgba(25, 23, 36, 0.92);
}

.bg-overlay {
    background-color: transparent;
}

.outer-box {
    background-color: transparent;
    padding: 24px;
}

.grid {
    background-color: transparent;
    border-radius: 18px;
}

.ws-btn {
    background-color: rgba(38, 35, 58, 0.62);
    border: 2px solid #6e6a86;
    border-radius: 16px;
    padding: 12px;
}

.ws-btn:hover {
    border-color: #ebbcba;
    background-color: rgba(235, 188, 186, 0.14);
}

.ws-active {
    border-color: #c4a7e7;
    background-color: rgba(196, 167, 231, 0.16);
}

.ws-drop {
    border: 3px dashed #eb6f92;
    background-color: rgba(235, 110, 146, 0.28);
}

.ws-label {
    color: #ebbcba;
    font-size: 15px;
    font-weight: bold;
    margin-bottom: 8px;
}

.app-container {
    background-color: transparent;
}

.app-box {
    background-color: rgba(110, 106, 134, 0.38);
    border-radius: 8px;
    padding: 8px;
    margin-bottom: 6px;
}

.app-box:hover {
    background-color: #ebbcba;
}

.app-box:hover label {
    color: #191724;
}

.app-box-dragging {
    background-color: #eb6f92;
    opacity: 0.5;
}

.app-label {
    color: #e0def4;
    font-size: 13px;
    margin-left: 8px;
}

.hint-label {
    color: #908caa;
    font-size: 13px;
    margin-top: 18px;
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


def is_instance_or_ancestor(widget: Optional[Gtk.Widget], cls: type) -> bool:
    """Return True if widget is an instance of cls or a descendant of one."""
    current = widget
    while current is not None:
        if isinstance(current, cls):
            return True
        current = current.get_parent()
    return False


def parse_drag_payload(raw: Optional[str]) -> Tuple[str, int]:
    """
    Parse drag payload.

    Payload: JSON {"id": "...", "source_ws": int}
    Fallback: legacy plain text client id.
    """
    if not raw:
        return "", 0

    text = raw.strip()
    if not text:
        return "", 0

    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            client_id = str(obj.get("id", "")).strip()
            source_ws_raw = obj.get("source_ws", 0)
            try:
                source_ws = int(source_ws_raw)
            except (TypeError, ValueError):
                source_ws = 0
            return client_id, source_ws
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.debug("Drag payload is not JSON, falling back to plain id: %s", exc)

    return text, 0


def get_runtime_dir() -> str:
    """
    Return a per-user runtime directory for lock/socket/helper files.

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
    fallback = os.path.join(tempfile.gettempdir(), f"mangowm-grid-overview-{uid}")
    os.makedirs(fallback, mode=0o700, exist_ok=True)
    return fallback


# =============================================================================
# Models
# =============================================================================


@dataclass(frozen=True)
class ClientState:
    id: str
    title: str
    app_id: str
    ws: int


@dataclass
class OverviewState:
    current_ws: int
    clients_by_ws: Dict[int, List[ClientState]]
    ipc_available: bool


# =============================================================================
# MangoWM IPC
# =============================================================================


class MangoWM_IPC:
    """
    IPC layer for MangoWM through `mmsg`.

    All commands are intended to be executed from a worker thread, never
    directly from the GTK main thread.
    """

    # -------------------------------------------------------------------------
    # Low-level subprocess helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _query(
        args: List[str],
        timeout: float = IPC_COMMAND_TIMEOUT,
        log_errors: bool = True,
    ) -> Optional[Any]:
        """
        Run `mmsg <args>` and parse JSON stdout.

        Returns None on error. Returns {} when the command succeeds but stdout
        is empty.
        """
        cmd_str = " ".join(args)

        try:
            proc = subprocess.run(
                ["mmsg"] + args,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            if log_errors:
                log_throttled(
                    logging.ERROR,
                    "mmsg-missing",
                    "Cannot find `mmsg`. Make sure MangoWM IPC client is installed in PATH.",
                )
            return None
        except subprocess.TimeoutExpired:
            if log_errors:
                log_throttled(
                    logging.WARNING,
                    f"timeout:mmsg {cmd_str}",
                    "Timeout while running `mmsg %s` after %.2fs.",
                    cmd_str,
                    timeout,
                )
            return None
        except Exception as exc:
            if log_errors:
                log_throttled(
                    logging.ERROR,
                    f"exec:mmsg {cmd_str}",
                    "Unexpected error while running `mmsg %s`: %s",
                    cmd_str,
                    exc,
                )
            return None

        if proc.returncode != 0:
            if log_errors:
                stderr = (proc.stderr or "").strip()
                stdout = (proc.stdout or "").strip()
                detail = stderr or stdout or f"returncode={proc.returncode}"
                log_throttled(
                    logging.WARNING,
                    f"rc:mmsg {cmd_str}:{detail[:120]}",
                    "`mmsg %s` returned error: %s",
                    cmd_str,
                    detail,
                )
            return None

        stdout = (proc.stdout or "").strip()
        if not stdout:
            return {}

        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            if log_errors:
                log_throttled(
                    logging.ERROR,
                    f"json:mmsg {cmd_str}",
                    "Cannot parse JSON from `mmsg %s`: %s. stdout=%r",
                    cmd_str,
                    exc,
                    stdout[:200],
                )
            return None

    @staticmethod
    def _dispatch(
        args: List[str],
        timeout: float = IPC_DISPATCH_TIMEOUT,
        log_errors: bool = False,
    ) -> bool:
        """Run `mmsg dispatch <args>` and return True when returncode == 0."""
        cmd_str = " ".join(args)

        try:
            proc = subprocess.run(
                ["mmsg", "dispatch"] + args,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            if log_errors:
                log_throttled(
                    logging.ERROR,
                    "mmsg-missing",
                    "Cannot find `mmsg` while dispatching `%s`.",
                    cmd_str,
                )
            return False
        except subprocess.TimeoutExpired:
            if log_errors:
                log_throttled(
                    logging.WARNING,
                    f"timeout:dispatch {cmd_str}",
                    "Timeout while dispatching `mmsg dispatch %s`.",
                    cmd_str,
                )
            return False
        except Exception as exc:
            if log_errors:
                log_throttled(
                    logging.ERROR,
                    f"exec:dispatch {cmd_str}",
                    "Error while dispatching `mmsg dispatch %s`: %s",
                    cmd_str,
                    exc,
                )
            return False

        if proc.returncode != 0:
            if log_errors:
                stderr = (proc.stderr or "").strip()
                stdout = (proc.stdout or "").strip()
                detail = stderr or stdout or f"returncode={proc.returncode}"
                log_throttled(
                    logging.WARNING,
                    f"rc:dispatch {cmd_str}:{detail[:120]}",
                    "Dispatch `mmsg dispatch %s` failed: %s",
                    cmd_str,
                    detail,
                )
            return False

        return True

    @staticmethod
    def _dispatch_variants(variants: List[List[str]], action_name: str) -> bool:
        """Try multiple dispatch syntax variants and log when all fail."""
        for args in variants:
            if MangoWM_IPC._dispatch(args, log_errors=False):
                return True

        log_throttled(
            logging.ERROR,
            f"dispatch-failed:{action_name}",
            "Cannot dispatch action `%s` after %s command variant(s).",
            action_name,
            len(variants),
        )
        return False

    @staticmethod
    def _try_dispatch_variants(variants: List[List[str]]) -> bool:
        """Try multiple dispatch variants without logging all-failure."""
        for args in variants:
            if MangoWM_IPC._dispatch(args, log_errors=False):
                return True
        return False

    # -------------------------------------------------------------------------
    # Parsing helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _first_str(obj: Dict[str, Any], keys: List[str]) -> Optional[str]:
        for key in keys:
            value = obj.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return None

    @staticmethod
    def _extract_client_list(data: Any) -> List[Any]:
        if isinstance(data, dict):
            for key in ("clients", "data", "windows", "items"):
                value = data.get(key)
                if isinstance(value, (list, tuple)):
                    return list(value)
            return []

        if isinstance(data, (list, tuple)):
            return list(data)

        return []

    @staticmethod
    def _extract_pid(obj: Any) -> Optional[int]:
        if isinstance(obj, (list, tuple)) and obj:
            obj = obj[0]

        if not isinstance(obj, dict):
            return None

        for key in ("pid", "process_id", "client_pid"):
            value = obj.get(key)
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue

        nested = obj.get("client")
        if isinstance(nested, dict):
            return MangoWM_IPC._extract_pid(nested)

        return None

    @staticmethod
    def parse_tags(raw: Any) -> List[int]:
        """
        Parse client tags.

        Supported forms:
          - int bitmask: 0b101 -> [1, 3]
          - list of workspace indexes: [1, 3]
          - string: "1,3"
          - dict containing workspace/tag metadata
        """
        result: List[int] = []

        if raw is None or isinstance(raw, bool):
            return result

        if isinstance(raw, int):
            # Bitmask.
            for ws in range(1, 33):
                if raw & (1 << (ws - 1)):
                    result.append(ws)
            return result

        if isinstance(raw, str):
            for part in raw.replace(";", ",").split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    ws = int(part)
                except ValueError:
                    continue
                if 1 <= ws <= 32:
                    result.append(ws)
            return result

        if isinstance(raw, (list, tuple)):
            for item in raw:
                if isinstance(item, bool):
                    continue

                if isinstance(item, int):
                    if 1 <= item <= 32:
                        result.append(item)
                    continue

                if isinstance(item, str):
                    try:
                        ws = int(item.strip())
                    except ValueError:
                        continue
                    if 1 <= ws <= 32:
                        result.append(ws)
                    continue

                if isinstance(item, dict):
                    ws = MangoWM_IPC._extract_ws_index(item)
                    if ws:
                        result.append(ws)

            return result

        if isinstance(raw, dict):
            ws = MangoWM_IPC._extract_ws_index(raw)
            if ws:
                result.append(ws)

        return result

    @staticmethod
    def _extract_ws_index(obj: Any) -> Optional[int]:
        """
        Extract a workspace index from heterogeneous JSON structures.

        This tries to be safe for both indexes and bitmasks. Some ambiguous
        cases remain, for example int 4 can mean workspace 4 or bitmask for
        workspace 3. For current-workspace endpoints, index interpretation is
        preferred.
        """
        if obj is None or isinstance(obj, bool):
            return None

        if isinstance(obj, int):
            if 1 <= obj <= NUM_WORKSPACES:
                return obj

            # Accept power-of-two values as bitmask only outside index range.
            if obj > 0 and (obj & (obj - 1)) == 0:
                tags = MangoWM_IPC.parse_tags(obj)
                if tags:
                    return tags[0]

            return None

        if isinstance(obj, str):
            try:
                value = int(obj.strip())
            except ValueError:
                return None
            return MangoWM_IPC._extract_ws_index(value)

        if isinstance(obj, (list, tuple)):
            # Prefer objects explicitly marked focused/active/current.
            for item in obj:
                if isinstance(item, dict):
                    flags = ("focused", "active", "current", "selected")
                    if any(bool(item.get(flag)) for flag in flags):
                        value = MangoWM_IPC._extract_ws_index(item)
                        if value:
                            return value

            if obj:
                return MangoWM_IPC._extract_ws_index(obj[0])

            return None

        if isinstance(obj, dict):
            for key in (
                "current",
                "active",
                "focused",
                "current_workspace",
                "active_workspace",
                "focused_workspace",
                "selected_workspace",
                "current_tag",
                "active_tag",
                "focused_tag",
                "selected_tag",
                "workspace",
                "tag",
                "index",
                "id",
            ):
                if key in obj:
                    value = MangoWM_IPC._extract_ws_index(obj[key])
                    if value:
                        return value

            # The `tags` key is usually a bitmask.
            if "tags" in obj:
                tags = MangoWM_IPC.parse_tags(obj["tags"])
                if tags:
                    return tags[0]

        return None

    @staticmethod
    def _is_own_client(item: Dict[str, Any], exclude_client_id: Optional[str]) -> bool:
        """Return True when this client item looks like this overview window."""
        client_id = MangoWM_IPC._first_str(item, CLIENT_ID_KEYS)
        if exclude_client_id and client_id and client_id == exclude_client_id:
            return True

        pid = MangoWM_IPC._extract_pid(item)
        if pid is not None and pid == os.getpid():
            return True

        title = MangoWM_IPC._first_str(item, CLIENT_TITLE_KEYS) or ""
        if title.strip().lower() == WINDOW_TITLE.lower():
            return True

        role = MangoWM_IPC._first_str(item, CLIENT_ROLE_KEYS) or ""
        if WINDOW_ROLE.lower() in role.lower():
            return True

        app = MangoWM_IPC._first_str(item, CLIENT_APP_KEYS) or ""
        if APP_NAME.lower() in app.lower():
            return True

        return False

    # -------------------------------------------------------------------------
    # State queries
    # -------------------------------------------------------------------------

    @staticmethod
    def get_current_ws() -> int:
        """
        Get current workspace.

        Strategy:
          1. focusing-client.
          2. all-monitors.
          3. all-tags.
          4. fallback to workspace 1 when unknown.
        """
        data = MangoWM_IPC._query(["get", "focusing-client"], log_errors=False)
        if isinstance(data, (list, tuple)) and data:
            data = data[0]

        if isinstance(data, dict):
            tags = MangoWM_IPC.parse_tags(data.get("tags"))
            if tags:
                return tags[0]

            nested = data.get("client")
            if isinstance(nested, dict):
                tags = MangoWM_IPC.parse_tags(nested.get("tags"))
                if tags:
                    return tags[0]

            ws = MangoWM_IPC._extract_ws_index(data.get("workspace"))
            if ws:
                return ws

        data = MangoWM_IPC._query(["get", "all-monitors"], log_errors=False)
        ws = MangoWM_IPC._extract_ws_index(data)
        if ws:
            return ws

        data = MangoWM_IPC._query(["get", "all-tags"], log_errors=False)
        ws = MangoWM_IPC._extract_ws_index(data)
        if ws:
            return ws

        return 1

    @staticmethod
    def get_focused_client_id() -> Optional[str]:
        data = MangoWM_IPC._query(["get", "focusing-client"], log_errors=False)

        if isinstance(data, (list, tuple)) and data:
            data = data[0]

        if not isinstance(data, dict):
            return None

        direct = MangoWM_IPC._first_str(data, CLIENT_ID_KEYS)
        if direct:
            return direct

        nested = data.get("client")
        if isinstance(nested, dict):
            return MangoWM_IPC._first_str(nested, CLIENT_ID_KEYS)

        return None

    @staticmethod
    def get_overview_state(
        exclude_client_id: Optional[str] = None,
        exclude_own: bool = True,
    ) -> OverviewState:
        current_ws = MangoWM_IPC.get_current_ws()
        data = MangoWM_IPC._query(["get", "all-clients"], log_errors=True)

        ipc_available = data is not None
        clients_by_ws: Dict[int, List[ClientState]] = {
            i: [] for i in range(1, NUM_WORKSPACES + 1)
        }
        seen: Dict[int, Set[str]] = {i: set() for i in range(1, NUM_WORKSPACES + 1)}

        raw_clients = MangoWM_IPC._extract_client_list(data)

        for item in raw_clients:
            if not isinstance(item, dict):
                continue

            if exclude_own and MangoWM_IPC._is_own_client(item, exclude_client_id):
                continue

            client_id = MangoWM_IPC._first_str(item, CLIENT_ID_KEYS)
            if not client_id:
                continue

            app_id = MangoWM_IPC._first_str(item, CLIENT_APP_KEYS) or ""
            title = (
                MangoWM_IPC._first_str(item, CLIENT_TITLE_KEYS)
                or app_id
                or "Unknown Window"
            )
            title = title.strip().replace("\n", " ")

            ws_list = MangoWM_IPC.parse_tags(item.get("tags"))

            if not ws_list:
                ws = MangoWM_IPC._extract_ws_index(
                    item.get("workspace", item.get("tag"))
                )
                if ws:
                    ws_list = [ws]

            for ws in ws_list:
                if not (1 <= ws <= NUM_WORKSPACES):
                    continue

                if client_id in seen[ws]:
                    continue

                seen[ws].add(client_id)
                clients_by_ws[ws].append(
                    ClientState(
                        id=client_id,
                        title=title,
                        app_id=app_id.strip(),
                        ws=ws,
                    )
                )

        return OverviewState(
            current_ws=current_ws,
            clients_by_ws=clients_by_ws,
            ipc_available=ipc_available,
        )

    @staticmethod
    def find_own_client_id(pid: int, title: str, role: str, app_name: str) -> Optional[str]:
        """
        Find this GUI's own MangoWM client id.

        Preference order:
          1. PID match, if MangoWM exposes it.
          2. Exact window title match.
          3. Role/app-id/class match.
        """
        data = MangoWM_IPC._query(["get", "all-clients"], log_errors=False)
        raw_clients = MangoWM_IPC._extract_client_list(data)

        # Pass 1: PID.
        for item in raw_clients:
            if not isinstance(item, dict):
                continue

            item_pid = MangoWM_IPC._extract_pid(item)
            if item_pid == pid:
                client_id = MangoWM_IPC._first_str(item, CLIENT_ID_KEYS)
                if client_id:
                    return client_id

        # Pass 2: metadata.
        title_l = title.strip().lower()
        role_l = role.strip().lower()
        app_l = app_name.strip().lower()

        for item in raw_clients:
            if not isinstance(item, dict):
                continue

            client_id = MangoWM_IPC._first_str(item, CLIENT_ID_KEYS)
            if not client_id:
                continue

            item_title = (MangoWM_IPC._first_str(item, CLIENT_TITLE_KEYS) or "").strip()
            item_role = (MangoWM_IPC._first_str(item, CLIENT_ROLE_KEYS) or "").strip()
            item_app = (MangoWM_IPC._first_str(item, CLIENT_APP_KEYS) or "").strip()

            if title_l and item_title.lower() == title_l:
                return client_id

            if role_l and role_l in item_role.lower():
                return client_id

            if app_l and app_l in item_app.lower():
                return client_id

        return None

    # -------------------------------------------------------------------------
    # Basic dispatch actions
    # -------------------------------------------------------------------------

    @staticmethod
    def dispatch_view(ws: int) -> bool:
        if not (1 <= ws <= NUM_WORKSPACES):
            return False

        variants: List[List[str]] = [
            [f"view,{ws},0"],
            [f"view,{ws}"],
        ]
        return MangoWM_IPC._dispatch_variants(variants, f"view workspace {ws}")

    @staticmethod
    def dispatch_focus(client_id: str) -> bool:
        client_id = (client_id or "").strip()
        if not client_id:
            return False

        # MangoWM docs: focusid can target any window via `client,<id>`.
        variants: List[List[str]] = [
            ["focusid", f"client,{client_id}"],
            [f"client,{client_id}", "focusid"],
            # Legacy fallbacks for older revisions.
            [f"focus,{client_id}"],
            ["focus", f"client,{client_id}"],
        ]
        return MangoWM_IPC._dispatch_variants(variants, f"focus client {client_id}")

    @staticmethod
    def tag_client_to_workspace(client_id: str, ws: int) -> bool:
        """
        Tag a client into a workspace using normal `tag`.

        MangoWM docs:
          dispatch tag,<tag> [,synctag] [client,<id>]
        """
        client_id = (client_id or "").strip()
        if not client_id or not (1 <= ws <= NUM_WORKSPACES):
            return False

        variants: List[List[str]] = [
            [f"tag,{ws}", f"client,{client_id}"],
            [f"client,{client_id}", f"tag,{ws}"],
            [f"tag,{ws},0", f"client,{client_id}"],
            [f"client,{client_id}", f"tag,{ws},0"],
        ]

        return MangoWM_IPC._dispatch_variants(
            variants,
            f"tag client {client_id} -> workspace {ws}",
        )

    # -------------------------------------------------------------------------
    # Silent move support
    # -------------------------------------------------------------------------

    @staticmethod
    def _custom_silent_move_variants(client_id: str, ws: int) -> List[List[str]]:
        """
        Allow user to define exact silent-move syntax if needed.

        Example:
          GRID_OVERVIEW_SILENT_MOVE="tagsilent,{ws} client,{client_id}"
        """
        template = os.environ.get(SILENT_MOVE_ENV_VAR, "").strip()
        if not template:
            return []

        try:
            expanded = template.format(ws=ws, client_id=client_id)
        except Exception as exc:
            logger.debug("Cannot expand %s template: %s", SILENT_MOVE_ENV_VAR, exc)
            return []

        args = expanded.split()
        if not args:
            return []

        return [args]

    @staticmethod
    def _silent_tag_variants(client_id: str, ws: int) -> List[List[str]]:
        """
        Build silent-tag variants.

        MangoWM docs define `tagsilent` as:
          tagsilent 1-9 : Move window to tag without focusing it.
        """
        client_id = (client_id or "").strip()
        if not client_id or not (1 <= ws <= NUM_WORKSPACES):
            return []

        variants = MangoWM_IPC._custom_silent_move_variants(client_id, ws)

        variants.extend(
            [
                [f"tagsilent,{ws}", f"client,{client_id}"],
                [f"client,{client_id}", f"tagsilent,{ws}"],
                # Some builds may still accept an optional trailing argument.
                [f"tagsilent,{ws},0", f"client,{client_id}"],
                [f"client,{client_id}", f"tagsilent,{ws},0"],
            ]
        )

        return variants

    @staticmethod
    def _verify_client_on_ws(client_id: str, ws: int) -> bool:
        """Verify that a client is present on a workspace."""
        try:
            state = MangoWM_IPC.get_overview_state(exclude_own=False)
        except Exception as exc:
            logger.error("Cannot verify client workspace presence: %s", exc)
            return False

        return any(c.id == client_id for c in state.clients_by_ws.get(ws, []))

    @staticmethod
    def tag_client_silent(client_id: str, ws: int, verify: bool = False) -> bool:
        """
        Try silent tag command.

        When verify=True, only return True if the client is actually seen on
        target workspace after dispatch.
        """
        client_id = (client_id or "").strip()
        if not client_id or not (1 <= ws <= NUM_WORKSPACES):
            return False

        variants = MangoWM_IPC._silent_tag_variants(client_id, ws)

        for args in variants:
            if not MangoWM_IPC._dispatch(args, log_errors=False):
                continue

            if not verify:
                return True

            time.sleep(MOVE_VERIFY_SLEEP_SECONDS)
            if MangoWM_IPC._verify_client_on_ws(client_id, ws):
                return True

        return False

    @staticmethod
    def tag_client_silent_or_normal(client_id: str, ws: int) -> bool:
        """Try silent tag first, then fallback to normal tag."""
        if MangoWM_IPC.tag_client_silent(client_id, ws, verify=True):
            return True
        return MangoWM_IPC.tag_client_to_workspace(client_id, ws)

    @staticmethod
    def _move_client_silent_with_verify(
        client_id: str,
        target_ws: int,
        source_ws: int,
    ) -> bool:
        """
        Try silent move variants and verify expected tag state.
        """
        variants = MangoWM_IPC._silent_tag_variants(client_id, target_ws)

        for args in variants:
            if not MangoWM_IPC._dispatch(args, log_errors=False):
                continue

            if MangoWM_IPC._verify_move_state(client_id, source_ws, target_ws):
                return True

            time.sleep(MOVE_VERIFY_SLEEP_SECONDS)

            if MangoWM_IPC._verify_move_state(client_id, source_ws, target_ws):
                return True

        return False

    @staticmethod
    def _verify_move_state(client_id: str, source_ws: int, target_ws: int) -> bool:
        """
        Verify that the client is present on target workspace.

        If source_ws is known and different from target_ws, also verify that
        the client is no longer present on source workspace.
        """
        try:
            state = MangoWM_IPC.get_overview_state(exclude_own=False)
        except Exception as exc:
            logger.error("Cannot verify move state: %s", exc)
            return False

        on_target = any(
            c.id == client_id for c in state.clients_by_ws.get(target_ws, [])
        )
        if not on_target:
            return False

        if 1 <= source_ws <= NUM_WORKSPACES and source_ws != target_ws:
            on_source = any(
                c.id == client_id for c in state.clients_by_ws.get(source_ws, [])
            )
            if on_source:
                return False

        return True

    # -------------------------------------------------------------------------
    # Anti-flicker move implementation
    # -------------------------------------------------------------------------

    @staticmethod
    def _choose_anchor_focus_from_state(
        state: OverviewState,
        prev_focus: Optional[str],
        moved_id: str,
        source_ws: int,
        own_client_id: Optional[str],
    ) -> Optional[str]:
        """
        Choose a focus anchor on the current workspace.

        The anchor is used to keep focus on the current workspace while the
        moved client is tagged away.

        Important:
          - The overview itself may be used as a last-resort anchor.
          - The overview is never the moved client.
        """
        prev_ws = state.current_ws

        # If another client is already focused, keep it.
        if prev_focus and prev_focus != moved_id:
            return prev_focus

        # If the moved client is focused, or it belongs to the current
        # workspace, try to choose another real client first.
        if prev_focus == moved_id or (
            1 <= source_ws <= NUM_WORKSPACES and source_ws == prev_ws
        ):
            for client in state.clients_by_ws.get(prev_ws, []):
                if client.id == moved_id:
                    continue
                if own_client_id is not None and client.id == own_client_id:
                    continue
                return client.id

            # Last resort: focus the overview itself to keep focus on current
            # workspace and avoid focus-follows-move behavior.
            if own_client_id and own_client_id != moved_id:
                return own_client_id

        return None

    @staticmethod
    def _find_first_client_id_on_ws(
        ws: int,
        exclude_client_id: str,
        exclude_own_client_id: Optional[str] = None,
    ) -> Optional[str]:
        """Find first client on workspace, excluding given clients."""
        if not (1 <= ws <= NUM_WORKSPACES):
            return None

        try:
            state = MangoWM_IPC.get_overview_state(
                exclude_client_id=exclude_own_client_id,
                exclude_own=True,
            )
        except Exception as exc:
            logger.error("Cannot query state for fallback focus: %s", exc)
            return None

        for client in state.clients_by_ws.get(ws, []):
            if client.id == exclude_client_id:
                continue
            if exclude_own_client_id is not None and client.id == exclude_own_client_id:
                continue
            return client.id

        return None

    @staticmethod
    def _late_stay_check(
        prev_ws: int,
        desired_focus: Optional[str],
        moved_id: str,
        own_client_id: Optional[str],
    ) -> None:
        """
        Late safety check.

        This is intentionally short. It only dispatches corrections when the
        WM state is actually wrong.
        """
        time.sleep(MOVE_LATE_CHECK_SLEEP_SECONDS)

        try:
            current_ws = MangoWM_IPC.get_current_ws()
            current_focus = MangoWM_IPC.get_focused_client_id()

            if current_ws != prev_ws:
                MangoWM_IPC.dispatch_view(prev_ws)

            if desired_focus:
                if current_focus != desired_focus:
                    MangoWM_IPC.dispatch_focus(desired_focus)
            elif current_focus == moved_id:
                fallback_focus = MangoWM_IPC._find_first_client_id_on_ws(
                    prev_ws,
                    moved_id,
                    own_client_id,
                )
                if not fallback_focus and own_client_id and own_client_id != moved_id:
                    fallback_focus = own_client_id

                if fallback_focus:
                    MangoWM_IPC.dispatch_focus(fallback_focus)

        except Exception as exc:
            logger.error("Late workspace/focus safety check failed: %s", exc)

    @staticmethod
    def move_client_to_workspace(
        client_id: str,
        target_ws: int,
        source_ws: int,
        own_client_id: Optional[str] = None,
    ) -> bool:
        """
        Move a client to target workspace while trying to keep the user on the
        current workspace.
        """
        client_id = (client_id or "").strip()
        if not client_id or not (1 <= target_ws <= NUM_WORKSPACES):
            return False

        if own_client_id is None:
            try:
                own_client_id = MangoWM_IPC.find_own_client_id(
                    os.getpid(),
                    WINDOW_TITLE,
                    WINDOW_ROLE,
                    APP_NAME,
                )
            except Exception as exc:
                logger.error("Cannot find own client id before move: %s", exc)
                own_client_id = None

        try:
            state = MangoWM_IPC.get_overview_state(
                exclude_client_id=own_client_id,
                exclude_own=True,
            )
            prev_focus = MangoWM_IPC.get_focused_client_id()
        except Exception as exc:
            logger.error("Cannot prepare move context: %s", exc)
            return False

        prev_ws = state.current_ws

        # Preferred path: silent move using `tagsilent`.
        if MangoWM_IPC._move_client_silent_with_verify(
            client_id=client_id,
            target_ws=target_ws,
            source_ws=source_ws,
        ):
            desired_focus: Optional[str] = None
            if prev_focus and prev_focus != client_id:
                desired_focus = prev_focus

            MangoWM_IPC._late_stay_check(
                prev_ws=prev_ws,
                desired_focus=desired_focus,
                moved_id=client_id,
                own_client_id=own_client_id,
            )
            return True

        # Fallback path: anchor focus + normal tag + immediate restore.
        anchor_focus = MangoWM_IPC._choose_anchor_focus_from_state(
            state=state,
            prev_focus=prev_focus,
            moved_id=client_id,
            source_ws=source_ws,
            own_client_id=own_client_id,
        )

        if anchor_focus and anchor_focus != prev_focus:
            MangoWM_IPC.dispatch_focus(anchor_focus)

        moved = MangoWM_IPC.tag_client_to_workspace(client_id, target_ws)
        if not moved:
            return False

        MangoWM_IPC.dispatch_view(prev_ws)

        desired_focus = None
        if anchor_focus:
            desired_focus = anchor_focus
        elif prev_focus and prev_focus != client_id:
            desired_focus = prev_focus

        if desired_focus:
            MangoWM_IPC.dispatch_focus(desired_focus)

        MangoWM_IPC._late_stay_check(
            prev_ws=prev_ws,
            desired_focus=desired_focus,
            moved_id=client_id,
            own_client_id=own_client_id,
        )

        return True


# =============================================================================
# Async IPC worker
# =============================================================================


class IPCWorker(threading.Thread):
    """
    Daemon worker thread for all IPC commands.

    - Avoids blocking the GTK main thread.
    - Keeps command order deterministic.
    - Daemonized so it cannot block process exit.
    """

    def __init__(self) -> None:
        super().__init__(daemon=True, name="mangowm-ipc-worker")
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
                logger.error("IPC worker task failed: %s", exc)
            finally:
                self._queue.task_done()


# =============================================================================
# mmsg watch worker
# =============================================================================


class MmsgWatchWorker(threading.Thread):
    """
    Watch a persistent mmsg stream and request UI sync on events.

    This uses `mmsg watch ...` as documented by `mmsg --help`.
    The actual event payload is not strictly parsed; any line is treated as
    a change notification, then the GUI re-queries consistent state.
    """

    def __init__(self, watch_args: List[str], on_event: Callable[[], None]) -> None:
        name = "mmsg-watch"
        if len(watch_args) >= 2:
            name = f"mmsg-watch-{watch_args[1]}"

        super().__init__(daemon=True, name=name)

        self._args = ["mmsg"] + watch_args
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
                        self._args,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        text=True,
                        bufsize=1,
                    )
                    proc = self._proc

                if proc is None or proc.stdout is None:
                    raise RuntimeError("Cannot open watch stream")

                for line in proc.stdout:
                    if self._stop_event.is_set():
                        break

                    if line.strip():
                        try:
                            self._on_event()
                        except Exception as exc:
                            logger.debug("Watch event callback failed: %s", exc)

            except FileNotFoundError:
                log_throttled(
                    logging.ERROR,
                    "mmsg-missing-watch",
                    "Cannot find `mmsg` for watch stream %s.",
                    " ".join(self._args),
                )
            except Exception as exc:
                logger.debug("Watch stream %s error: %s", " ".join(self._args), exc)
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
                time.sleep(WATCH_RESTART_DELAY_SECONDS)

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
    Ensures only one Grid Overview process is active.

    Behavior:
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
            # Lock infrastructure unavailable. Prefer allowing the app to run
            # instead of hard-blocking the user.
            logger.warning("Single-instance lock unavailable; continuing without lock.")
            return True

        # Another instance holds the lock.
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
        True: lock acquired.
        False: lock held by another process.
        None: lock infrastructure error.
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
# Global ESC binder
# =============================================================================


class GlobalEscBinder:
    """
    Installs a temporary MangoWM global ESC binding while this overview runs.

    It appends a marked block to the MangoWM config, then reloads config.
    On release, it removes the marked block and reloads config again.

    Binding uses `bindp` so ESC is also passed through to the focused client.
    """

    def __init__(self, sock_path: str) -> None:
        self.sock_path = sock_path
        self.helper_path = os.path.join(get_runtime_dir(), GLOBAL_ESC_HELPER_FILE_NAME)
        self.config_path = self._find_config_path()
        self.installed = False

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def install(self) -> bool:
        if not self.config_path:
            logger.warning(
                "Cannot find writable MangoWM config path; global ESC disabled."
            )
            return False

        try:
            self._write_helper()
            self._remove_block()
            self._append_block()
        except Exception as exc:
            logger.error("Cannot install global ESC bind: %s", exc)
            try:
                self._remove_block()
            except Exception:
                pass
            return False

        if MangoWM_IPC._dispatch(["reload_config"], log_errors=False):
            self.installed = True
            logger.info("Global ESC bind installed.")
            return True

        logger.error("reload_config failed; removing global ESC bind.")
        try:
            self._remove_block()
            MangoWM_IPC._dispatch(["reload_config"], log_errors=False)
        except Exception:
            pass

        return False

    def release(self) -> None:
        if not self.config_path:
            return

        try:
            changed = self._remove_block()
            if changed or self.installed:
                MangoWM_IPC._dispatch(["reload_config"], log_errors=False)
                logger.info("Global ESC bind removed.")
        except Exception as exc:
            logger.error("Cannot remove global ESC bind: %s", exc)

        self.installed = False

        try:
            if os.path.exists(self.helper_path):
                os.unlink(self.helper_path)
        except OSError as exc:
            logger.debug("Cannot remove global ESC helper: %s", exc)

    # -------------------------------------------------------------------------
    # Config discovery and manipulation
    # -------------------------------------------------------------------------

    def _find_config_path(self) -> Optional[str]:
        xdg_config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser(
            "~/.config"
        )

        dir_names = ("mango", "mangowm", "MangoWM", "mango-wm")
        file_names = ("config", "mango.conf", "mangowm.conf", "config.conf")

        candidates: List[str] = []

        for dir_name in dir_names:
            for file_name in file_names:
                candidates.append(os.path.join(xdg_config, dir_name, file_name))

        candidates.append(os.path.expanduser("~/.mangowmrc"))
        candidates.append(os.path.expanduser("~/.mango.conf"))

        # Prefer an existing writable config file.
        for path in candidates:
            if os.path.isfile(path) and os.access(path, os.W_OK):
                return path

        # If a config directory exists, choose a default config path inside it.
        for dir_name in dir_names:
            directory = os.path.join(xdg_config, dir_name)
            if os.path.isdir(directory) and os.access(directory, os.W_OK):
                return os.path.join(directory, "config")

        return None

    def _write_helper(self) -> None:
        content = "\n".join(
            [
                "#!/usr/bin/env python3",
                "import socket",
                "",
                f"SOCK_PATH = {self.sock_path!r}",
                "",
                "try:",
                "    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)",
                "    s.settimeout(0.3)",
                "    s.connect(SOCK_PATH)",
                "    s.sendall(b'quit')",
                "    s.recv(16)",
                "    s.close()",
                "except Exception:",
                "    pass",
                "",
            ]
        )

        with open(self.helper_path, "w", encoding="utf-8") as f:
            f.write(content)

        try:
            os.chmod(self.helper_path, 0o755)
        except OSError as exc:
            logger.debug("Cannot chmod helper script: %s", exc)

    def _read_config_lines(self) -> List[str]:
        if not self.config_path:
            return []

        if not os.path.exists(self.config_path):
            return []

        with open(self.config_path, "r", encoding="utf-8") as f:
            return f.readlines()

    def _write_config_lines(self, lines: List[str]) -> None:
        if not self.config_path:
            return

        directory = os.path.dirname(self.config_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        tmp_path = self.config_path + ".tmp"

        with open(tmp_path, "w", encoding="utf-8") as f:
            f.writelines(lines)

        if os.path.exists(self.config_path):
            try:
                st = os.stat(self.config_path)
                os.chmod(tmp_path, st.st_mode)
            except OSError as exc:
                logger.debug("Cannot preserve config permissions: %s", exc)

        os.replace(tmp_path, self.config_path)

    def _remove_block(self) -> bool:
        lines = self._read_config_lines()

        out: List[str] = []
        in_block = False
        changed = False

        for line in lines:
            stripped = line.strip()

            if stripped == GLOBAL_ESC_BEGIN_MARKER:
                in_block = True
                changed = True
                continue

            if stripped == GLOBAL_ESC_END_MARKER:
                in_block = False
                changed = True
                continue

            if not in_block:
                out.append(line)

        if changed:
            self._write_config_lines(out)

        return changed

    def _append_block(self) -> None:
        lines = self._read_config_lines()

        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"

        python_exe = sys.executable or "python3"
        command = f"{shlex.quote(python_exe)} {shlex.quote(self.helper_path)}"

        lines.append("\n")
        lines.append(GLOBAL_ESC_BEGIN_MARKER + "\n")
        lines.append("keymode=common\n")
        lines.append(f"bindp=NONE,Escape,spawn_shell,{command}\n")
        lines.append(GLOBAL_ESC_END_MARKER + "\n")

        self._write_config_lines(lines)


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

    def icon_name(self, app_id: str, title: str) -> str:
        fallback = "application-x-executable"

        raw = (app_id or title or "").strip()
        if not raw:
            return fallback

        key = raw.lower()
        cached = self._cache.get(key)
        if cached:
            return cached

        base = raw
        if base.lower().endswith(".desktop"):
            base = base[: -len(".desktop")]

        candidates: List[str] = []

        def add_candidate(candidate: str) -> None:
            candidate = candidate.strip()
            if candidate and candidate not in candidates:
                candidates.append(candidate)

        add_candidate(base)
        add_candidate(base.lower())
        add_candidate(base.lower().replace("-", "_"))
        add_candidate(base.lower().replace(" ", "-"))
        add_candidate(base.lower().replace(" ", "_"))

        if "." in base:
            parts = [p.strip() for p in base.split(".") if p.strip()]
            if parts:
                add_candidate(parts[-1])
                add_candidate(parts[0])

        known_tokens = (
            "kitty",
            "firefox",
            "discord",
            "code",
            "vscode",
            "thunderbird",
            "spotify",
            "slack",
            "telegram",
            "terminal",
            "files",
            "nautilus",
            "dolphin",
            "chromium",
            "chrome",
            "brave",
        )
        base_lower = base.lower()
        for token in known_tokens:
            if token in base_lower:
                add_candidate(token)

        add_candidate(fallback)

        result = fallback

        if self._theme is not None:
            for candidate in candidates:
                try:
                    if self._theme.has_icon(candidate):
                        result = candidate
                        break
                except Exception as exc:
                    logger.debug("Icon lookup failed for candidate %s: %s", candidate, exc)

        if len(self._cache) > MAX_CACHED_ICONS:
            self._cache.clear()

        self._cache[key] = result
        return result


# =============================================================================
# Widgets
# =============================================================================


class AppWidget(Gtk.EventBox):
    """Widget representing one application/client inside a workspace."""

    def __init__(self, client: ClientState, main_window: "GridOverview") -> None:
        super().__init__()

        self.client = client
        self.main_window = main_window

        self.set_visible_window(True)
        self.set_tooltip_text(client.title)

        self.connect("button-release-event", self.on_button_release)

        self.drag_source_set(
            Gdk.ModifierType.BUTTON1_MASK,
            DND_TARGETS,
            Gdk.DragAction.MOVE,
        )
        self.connect("drag-data-get", self.on_drag_data_get)
        self.connect("drag-begin", self.on_drag_begin)
        self.connect("drag-end", self.on_drag_end)
        self.connect("drag-failed", self.on_drag_failed)

        self.box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.box.get_style_context().add_class("app-box")

        self.icon = Gtk.Image()
        self.label = Gtk.Label()
        self.label.set_halign(Gtk.Align.START)
        self.label.set_valign(Gtk.Align.CENTER)
        self.label.set_hexpand(True)

        # Wrap long titles without allowing workspace boxes to resize unevenly.
        self.label.set_line_wrap(True)
        self.label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.label.set_lines(2)
        self.label.set_ellipsize(Pango.EllipsizeMode.END)
        self.label.set_max_width_chars(24)
        self.label.get_style_context().add_class("app-label")

        self.box.pack_start(self.icon, False, False, 0)
        self.box.pack_start(self.label, True, True, 0)

        self.add(self.box)
        self._render()

    def _render(self) -> None:
        icon_name = self.main_window.icon_provider.icon_name(
            self.client.app_id,
            self.client.title,
        )
        self.icon.set_from_icon_name(icon_name, Gtk.IconSize.MENU)

        title = self.client.title.strip().replace("\n", " ")
        if not title:
            title = self.client.app_id.strip() or "Unknown Window"

        self.label.set_text(title)
        self.set_tooltip_text(title)

    def update(self, new_client: ClientState) -> None:
        if self.client != new_client:
            self.client = new_client
            self._render()

    # -------------------------------------------------------------------------
    # Click
    # -------------------------------------------------------------------------

    def on_button_release(
        self,
        _widget: Gtk.Widget,
        event: Gdk.EventButton,
    ) -> bool:
        if event.button != 1:
            return False

        if self.main_window.should_suppress_click():
            return False

        # Click app: focus app, but DO NOT exit.
        self.main_window.activate_app(self.client)
        return True

    # -------------------------------------------------------------------------
    # Drag source
    # -------------------------------------------------------------------------

    def on_drag_data_get(
        self,
        _widget: Gtk.Widget,
        _context: Gdk.DragContext,
        data: Gtk.SelectionData,
        _info: int,
        _time: int,
    ) -> None:
        payload = json.dumps(
            {
                "id": self.client.id,
                "source_ws": self.client.ws,
            }
        )
        data.set_text(payload, -1)

    def on_drag_begin(
        self,
        _widget: Gtk.Widget,
        context: Gdk.DragContext,
    ) -> None:
        self.main_window.notify_drag_begin()
        self.box.get_style_context().add_class("app-box-dragging")

        icon_name = self.main_window.icon_provider.icon_name(
            self.client.app_id,
            self.client.title,
        )

        try:
            Gtk.drag_set_icon_name(context, icon_name, 0, 0)
        except Exception as exc:
            logger.debug("Cannot set drag icon %s: %s", icon_name, exc)
            try:
                Gtk.drag_set_icon_default(context)
            except Exception as exc2:
                logger.debug("Cannot set default drag icon: %s", exc2)

    def on_drag_end(
        self,
        _widget: Gtk.Widget,
        _context: Gdk.DragContext,
    ) -> None:
        self.box.get_style_context().remove_class("app-box-dragging")
        self.main_window.notify_drag_end()

    def on_drag_failed(
        self,
        _widget: Gtk.Widget,
        _context: Gdk.DragContext,
        _result: Gdk.DragResult,
    ) -> bool:
        # Return True to suppress unnecessary failure beep.
        return True


class WorkspaceWidget(Gtk.EventBox):
    """Widget representing one workspace/tag and acting as a drop target."""

    def __init__(self, ws_id: int, main_window: "GridOverview") -> None:
        super().__init__()

        self.ws_id = ws_id
        self.main_window = main_window

        self.app_widgets: Dict[str, AppWidget] = {}
        self._active = False
        self._drag_leave_source = 0

        self.set_visible_window(True)

        self.connect("button-release-event", self.on_button_release)
        self.connect("destroy", self._on_destroy)

        self.drag_dest_set(
            Gtk.DestDefaults.ALL,
            DND_TARGETS,
            Gdk.DragAction.MOVE,
        )
        self.connect("drag-data-received", self.on_drag_data_received)
        self.connect("drag-motion", self.on_drag_motion)
        self.connect("drag-leave", self.on_drag_leave)

        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.box.get_style_context().add_class("ws-btn")
        self.box.set_size_request(280, 210)

        self.label = Gtk.Label(label=f"Workspace {self.ws_id}")
        self.label.set_halign(Gtk.Align.START)
        self.label.get_style_context().add_class("ws-label")

        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroll.set_vexpand(True)

        self.app_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.app_container.get_style_context().add_class("app-container")

        self.scroll.add(self.app_container)

        self.box.pack_start(self.label, False, False, 0)
        self.box.pack_start(self.scroll, True, True, 0)

        self.add(self.box)

    def _on_destroy(self, _widget: Gtk.Widget) -> None:
        if self._drag_leave_source:
            try:
                GLib.source_remove(self._drag_leave_source)
            except Exception as exc:
                logger.debug("Cannot remove drag-leave timeout: %s", exc)
            self._drag_leave_source = 0

    def set_cell_size(self, width: int, height: int) -> None:
        self.box.set_size_request(width, height)

    def set_active(self, is_active: bool) -> None:
        if self._active == is_active:
            return

        self._active = is_active
        ctx = self.box.get_style_context()

        if is_active:
            ctx.add_class("ws-active")
        else:
            ctx.remove_class("ws-active")

    def clear_drop_style(self) -> None:
        if self._drag_leave_source:
            try:
                GLib.source_remove(self._drag_leave_source)
            except Exception as exc:
                logger.debug("Cannot remove drag-leave timeout: %s", exc)
            self._drag_leave_source = 0

        self.box.get_style_context().remove_class("ws-drop")

    def sync_apps(self, ws_clients: List[ClientState]) -> None:
        """
        Synchronize applications inside this workspace.

        - Remove dead widgets.
        - Update existing widgets.
        - Create new widgets.
        - Preserve IPC order.
        """
        new_ids = {c.id for c in ws_clients}
        current_ids = set(self.app_widgets.keys())

        for dead_id in current_ids - new_ids:
            widget = self.app_widgets.pop(dead_id)
            try:
                self.app_container.remove(widget)
                widget.destroy()
            except Exception as exc:
                logger.debug("Cannot destroy app widget %s: %s", dead_id, exc)

        for index, client in enumerate(ws_clients):
            widget = self.app_widgets.get(client.id)

            if widget is None:
                widget = AppWidget(client, self.main_window)
                self.app_widgets[client.id] = widget
                self.app_container.pack_start(widget, False, False, 0)
                widget.show_all()
            else:
                widget.update(client)

            try:
                self.app_container.reorder_child(widget, index)
            except Exception as exc:
                logger.debug(
                    "Cannot reorder app widget %s to index %s: %s",
                    client.id,
                    index,
                    exc,
                )

    # -------------------------------------------------------------------------
    # Click
    # -------------------------------------------------------------------------

    def on_button_release(
        self,
        _widget: Gtk.Widget,
        event: Gdk.EventButton,
    ) -> bool:
        if event.button != 1:
            return False

        if self.main_window.should_suppress_click():
            return False

        event_widget = Gtk.get_event_widget(event)

        # Do not activate workspace when clicking an app or scrollbar.
        if is_instance_or_ancestor(event_widget, AppWidget):
            return False

        if is_instance_or_ancestor(event_widget, Gtk.Scrollbar):
            return False

        # Click workspace:
        #   - switch workspace,
        #   - move overview there,
        #   - DO NOT exit.
        self.main_window.activate_workspace(self.ws_id)
        return True

    # -------------------------------------------------------------------------
    # Drag destination
    # -------------------------------------------------------------------------

    def on_drag_motion(
        self,
        _widget: Gtk.Widget,
        context: Gdk.DragContext,
        _x: int,
        _y: int,
        drag_time: int,
    ) -> bool:
        if self._drag_leave_source:
            try:
                GLib.source_remove(self._drag_leave_source)
            except Exception as exc:
                logger.debug("Cannot remove drag-leave timeout: %s", exc)
            self._drag_leave_source = 0

        self.box.get_style_context().add_class("ws-drop")

        try:
            Gdk.drag_status(context, Gdk.DragAction.MOVE, drag_time)
        except Exception as exc:
            logger.debug("Cannot set drag status: %s", exc)

        return True

    def on_drag_leave(
        self,
        _widget: Gtk.Widget,
        _context: Gdk.DragContext,
        _time: int,
    ) -> None:
        # Small delay avoids flicker when the pointer crosses child widgets.
        if self._drag_leave_source:
            try:
                GLib.source_remove(self._drag_leave_source)
            except Exception as exc:
                logger.debug("Cannot remove drag-leave timeout: %s", exc)

        self._drag_leave_source = GLib.timeout_add(60, self._delayed_clear_drop)

    def _delayed_clear_drop(self) -> bool:
        self._drag_leave_source = 0
        self.box.get_style_context().remove_class("ws-drop")
        return False

    def on_drag_data_received(
        self,
        _widget: Gtk.Widget,
        context: Gdk.DragContext,
        _x: int,
        _y: int,
        data: Gtk.SelectionData,
        _info: int,
        drag_time: int,
    ) -> bool:
        self.clear_drop_style()

        raw = data.get_text()
        client_id, source_ws = parse_drag_payload(raw)

        if not client_id:
            context.finish(False, False, drag_time)
            return True

        # Move only. Do not view/focus target workspace here.
        self.main_window.move_client(
            client_id=client_id,
            source_ws=source_ws,
            target_ws=self.ws_id,
        )

        context.finish(True, False, drag_time)
        return True


# =============================================================================
# Main window
# =============================================================================


class GridOverview(Gtk.Window):
    def __init__(self, single_instance: SingleInstance) -> None:
        super().__init__(title=WINDOW_TITLE)

        self.single_instance = single_instance

        self.set_decorated(False)
        self.set_resizable(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)

        # State flags.
        self._closed = False

        self._drag_active_count = 0
        self._drag_failsafe_source = 0
        self._last_drag_end_monotonic = 0.0

        self._pending_mutations = 0

        self._sync_in_flight = False
        self._sync_again = False
        self._sync_source = 0

        self._poll_source = 0
        self._ipc_watch_source = 0

        self._ipc_available = True
        self._last_state: Optional[OverviewState] = None
        self._own_client_id: Optional[str] = None

        self._last_hint = ""
        self._last_cell_size: Tuple[int, int] = (0, 0)

        self.icon_provider = IconProvider()
        self._ipc_worker = IPCWorker()
        self._ipc_worker.start()

        self._watch_workers: List[MmsgWatchWorker] = []

        self.workspaces: Dict[int, WorkspaceWidget] = {}

        self._setup_window_backend()
        self._setup_css()
        self._setup_ui()
        self._setup_instance_socket_watch()
        self._setup_watch_workers()

        self.connect("destroy", self._on_destroy)
        self.connect("key-press-event", self.on_key_press)
        self.connect("size-allocate", self._on_size_allocate)

        self.show_all()
        self.present()

        GLib.idle_add(self._center_window_idle)

        self._update_hint(True)
        self.request_sync(immediate=True)
        self._schedule_poll(POLL_INTERVAL_MS)

    # -------------------------------------------------------------------------
    # Setup
    # -------------------------------------------------------------------------

    def _setup_window_backend(self) -> None:
        """
        Configure this window as a centered dialog-like overview.
        """
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
        except Exception as exc:
            logger.debug("Cannot set GLib application name: %s", exc)

        try:
            GLib.set_prgname(APP_NAME)
        except Exception as exc:
            logger.debug("Cannot set GLib program name: %s", exc)

        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.set_modal(False)
        self.set_keep_above(True)
        self.set_accept_focus(True)
        self.set_focus_on_map(True)
        self.set_position(Gtk.WindowPosition.CENTER)

        default_w, default_h = 1080, 720

        screen = self.get_screen()
        if screen is not None and screen.get_n_monitors() > 0:
            monitor = screen.get_primary_monitor()
            if monitor < 0 or monitor >= screen.get_n_monitors():
                monitor = 0

            geom = screen.get_monitor_geometry(monitor)
            default_w = max(720, min(1280, geom.width - 120))
            default_h = max(520, min(860, geom.height - 120))

        self.set_default_size(default_w, default_h)
        self.set_size_request(720, 560)

    def _setup_css(self) -> None:
        screen = Gdk.Screen.get_default()
        if screen is None:
            logger.error("Cannot get default Gdk.Screen; skipping CSS.")
            return

        provider = Gtk.CssProvider()

        try:
            provider.load_from_data(CSS)
            Gtk.StyleContext.add_provider_for_screen(
                screen,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
        except Exception as exc:
            logger.error("Cannot load CSS: %s", exc)

    def _setup_ui(self) -> None:
        self.bg_eventbox = Gtk.EventBox()
        self.bg_eventbox.set_visible_window(True)
        self.bg_eventbox.get_style_context().add_class("bg-overlay")
        self.bg_eventbox.set_events(
            self.bg_eventbox.get_events()
            | Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
        )

        # Background click intentionally does nothing.
        # ESC is the exit trigger.
        self.bg_eventbox.connect("button-press-event", self.on_bg_clicked)

        self.add(self.bg_eventbox)

        self.outer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.outer_box.get_style_context().add_class("outer-box")
        self.outer_box.set_halign(Gtk.Align.CENTER)
        self.outer_box.set_valign(Gtk.Align.CENTER)

        self.bg_eventbox.add(self.outer_box)

        self.grid = Gtk.Grid()
        self.grid.set_column_homogeneous(True)
        self.grid.set_row_homogeneous(True)
        self.grid.set_column_spacing(24)
        self.grid.set_row_spacing(24)
        self.grid.set_halign(Gtk.Align.CENTER)
        self.grid.set_valign(Gtk.Align.CENTER)

        self.outer_box.pack_start(self.grid, False, False, 0)

        self.hint_label = Gtk.Label()
        self.hint_label.get_style_context().add_class("hint-label")
        self.hint_label.set_justify(Gtk.Justification.CENTER)
        self.hint_label.set_max_width_chars(110)

        self.outer_box.pack_start(self.hint_label, False, False, 0)

        for i in range(1, NUM_WORKSPACES + 1):
            ws_widget = WorkspaceWidget(ws_id=i, main_window=self)
            ws_widget.set_cell_size(280, 210)
            self.workspaces[i] = ws_widget

            col = (i - 1) % GRID_COLUMNS
            row = (i - 1) // GRID_COLUMNS
            self.grid.attach(ws_widget, col, row, 1, 1)

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

    def _setup_watch_workers(self) -> None:
        watch_commands: List[List[str]] = [
            ["watch", "all-clients"],
            ["watch", "focusing-client"],
            ["watch", "all-tags"],
        ]

        for cmd in watch_commands:
            worker = MmsgWatchWorker(cmd, self._notify_watch_event)
            worker.start()
            self._watch_workers.append(worker)

    # -------------------------------------------------------------------------
    # Window placement
    # -------------------------------------------------------------------------

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
            "_drag_failsafe_source",
            "_ipc_watch_source",
        ):
            source = getattr(self, source_attr, 0)
            if source:
                try:
                    GLib.source_remove(source)
                except Exception as exc:
                    logger.debug("Cannot remove GLib source %s: %s", source_attr, exc)
                setattr(self, source_attr, 0)

        for worker in self._watch_workers:
            try:
                worker.stop()
            except Exception as exc:
                logger.debug("Cannot stop watch worker: %s", exc)

        try:
            self._ipc_worker.stop()
        except Exception as exc:
            logger.debug("Cannot stop IPC worker: %s", exc)

        try:
            self.single_instance.release()
        except Exception as exc:
            logger.debug("Cannot release single-instance resources: %s", exc)

        try:
            self.hide()
        except Exception as exc:
            logger.debug("Cannot hide window before quit: %s", exc)

        Gtk.main_quit()

    # -------------------------------------------------------------------------
    # Watch events
    # -------------------------------------------------------------------------

    def _notify_watch_event(self) -> None:
        """Called from watch worker threads."""
        if self._closed:
            return

        try:
            GLib.idle_add(self._on_watch_event)
        except Exception as exc:
            logger.debug("Cannot schedule watch event sync: %s", exc)

    def _on_watch_event(self) -> bool:
        if self._closed:
            return False

        self.request_sync(delay_ms=WATCH_EVENT_SYNC_DEBOUNCE_MS)
        return False

    # -------------------------------------------------------------------------
    # Single-instance IPC
    # -------------------------------------------------------------------------

    def _on_instance_message(
        self,
        _fd: int,
        _condition: GLib.IOCondition,
    ) -> bool:
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
                    GLib.idle_add(self.bring_to_current_workspace)
                elif message == IPC_MESSAGE_QUIT:
                    GLib.idle_add(self.close_app)

            try:
                conn.close()
            except OSError:
                pass

        return True

    def bring_to_current_workspace(self) -> bool:
        """
        Called when another instance asks this instance to show itself.

        It presents the dialog and asks MangoWM to move/focus this window on
        the current workspace.
        """
        if self._closed:
            return False

        self.show()
        self.present()

        # Do not interrupt an active drag & drop operation.
        if self._drag_active_count > 0 or self._pending_mutations > 0:
            GLib.idle_add(self._on_brought)
            return False

        def task() -> None:
            if self._closed:
                return

            try:
                current_ws = MangoWM_IPC.get_current_ws()
                self._move_overview_to_ws_blocking(current_ws, focus_self=True)
            except Exception as exc:
                logger.error("Failed to bring overview to current workspace: %s", exc)
            finally:
                if not self._closed:
                    GLib.idle_add(self._on_brought)

        self._ipc_worker.submit(task)
        return False

    def _on_brought(self) -> bool:
        if self._closed:
            return False

        self.present()
        self.request_sync(immediate=True)
        return False

    # -------------------------------------------------------------------------
    # Own window movement
    # -------------------------------------------------------------------------

    def _set_own_client_id(self, own_id: str) -> bool:
        if self._closed:
            return False

        if own_id and self._own_client_id != own_id:
            self._own_client_id = own_id

        return False

    def _get_own_client_id_blocking(self) -> Optional[str]:
        """
        Get own client id, discovering it if needed.

        This runs in worker thread.
        """
        if self._own_client_id:
            return self._own_client_id

        try:
            own_id = MangoWM_IPC.find_own_client_id(
                os.getpid(),
                WINDOW_TITLE,
                WINDOW_ROLE,
                APP_NAME,
            )
        except Exception as exc:
            logger.error("Cannot find own client id: %s", exc)
            own_id = None

        if own_id:
            GLib.idle_add(self._set_own_client_id, own_id)

        return own_id

    def _move_overview_to_ws_blocking(self, ws: int, focus_self: bool) -> bool:
        """
        Move overview window to a workspace and optionally focus it.

        This runs in worker thread.
        """
        if self._closed:
            return False

        if not (1 <= ws <= NUM_WORKSPACES):
            return False

        own_id = self._get_own_client_id_blocking()

        if own_id:
            MangoWM_IPC.tag_client_silent_or_normal(own_id, ws)

        view_ok = MangoWM_IPC.dispatch_view(ws)

        if focus_self and own_id:
            MangoWM_IPC.dispatch_focus(own_id)

        return view_ok

    # -------------------------------------------------------------------------
    # Responsive layout
    # -------------------------------------------------------------------------

    def _on_size_allocate(
        self,
        _widget: Gtk.Widget,
        allocation: Gdk.Rectangle,
    ) -> None:
        if self._closed:
            return

        if allocation.width < 100 or allocation.height < 100:
            return

        spacing_x = 24
        spacing_y = 24

        # Estimated chrome/padding overhead.
        chrome_w = 110
        chrome_h = 160

        cell_w = (
            allocation.width - chrome_w - spacing_x * (GRID_COLUMNS - 1)
        ) // GRID_COLUMNS
        cell_h = (
            allocation.height - chrome_h - spacing_y * (GRID_ROWS - 1)
        ) // GRID_ROWS

        cell_w = max(180, min(380, cell_w))
        cell_h = max(120, min(300, cell_h))

        new_size = (cell_w, cell_h)
        if new_size == self._last_cell_size:
            return

        self._last_cell_size = new_size

        for ws in self.workspaces.values():
            ws.set_cell_size(cell_w, cell_h)

    # -------------------------------------------------------------------------
    # Polling & sync
    # -------------------------------------------------------------------------

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

        if (
            self._drag_active_count == 0
            and self._pending_mutations == 0
            and not self._sync_in_flight
        ):
            self.request_sync(delay_ms=0)

        interval = POLL_INTERVAL_MS if self._ipc_available else POLL_INTERVAL_IPC_DOWN_MS
        self._schedule_poll(interval)

        return False

    def request_sync(self, delay_ms: int = SYNC_DEBOUNCE_MS, immediate: bool = False) -> None:
        if self._closed:
            return

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

        if self._drag_active_count > 0 or self._pending_mutations > 0:
            return

        self._sync_in_flight = True
        self._ipc_worker.submit(self._fetch_state_task)

    def _fetch_state_task(self) -> None:
        if self._closed:
            return

        own_id = self._own_client_id

        if own_id is None:
            try:
                own_id = MangoWM_IPC.find_own_client_id(
                    os.getpid(),
                    WINDOW_TITLE,
                    WINDOW_ROLE,
                    APP_NAME,
                )
            except Exception as exc:
                logger.error("Cannot find own client id during sync: %s", exc)
                own_id = None

        state: Optional[OverviewState] = None

        try:
            state = MangoWM_IPC.get_overview_state(
                exclude_client_id=own_id,
                exclude_own=True,
            )
        except Exception as exc:
            logger.error("Cannot fetch overview state: %s", exc)
        finally:
            if not self._closed:
                GLib.idle_add(self._apply_state, state, own_id)

    def _apply_state(self, state: Optional[OverviewState], own_id: Optional[str]) -> bool:
        if self._closed:
            return False

        if own_id and self._own_client_id != own_id:
            self._own_client_id = own_id

        self._sync_in_flight = False

        if state is not None:
            self._update_ui(state)
        else:
            self._ipc_available = False
            self._update_hint(False)

        if self._sync_again:
            self._sync_again = False
            self.request_sync(delay_ms=80)

        return False

    def _update_ui(self, state: OverviewState) -> None:
        if self._closed:
            return

        self._last_state = state
        self._ipc_available = state.ipc_available

        for ws_id in range(1, NUM_WORKSPACES + 1):
            ws_widget = self.workspaces.get(ws_id)
            if ws_widget is None:
                continue

            ws_widget.set_active(ws_id == state.current_ws)
            ws_widget.sync_apps(state.clients_by_ws.get(ws_id, []))

        self._update_hint(state.ipc_available)

    def _update_hint(self, ipc_available: bool) -> None:
        if self._closed:
            return

        if ipc_available:
            text = (
                "Drag app to move • Click app to focus • "
                "Click workspace to switch and move overview there • "
                "ESC to close (global if MangoWM config binding is available)"
            )
        else:
            text = (
                "MangoWM IPC (mmsg) unavailable or command rejected • "
                "Check MangoWM is running and `mmsg` exists in PATH"
            )

        if text != self._last_hint:
            self._last_hint = text
            self.hint_label.set_text(text)

    # -------------------------------------------------------------------------
    # Drag state
    # -------------------------------------------------------------------------

    def notify_drag_begin(self) -> None:
        if self._closed:
            return

        self._drag_active_count += 1

        if self._drag_failsafe_source:
            try:
                GLib.source_remove(self._drag_failsafe_source)
            except Exception as exc:
                logger.debug("Cannot remove old drag failsafe: %s", exc)

        self._drag_failsafe_source = GLib.timeout_add_seconds(
            DRAG_FAILSAFE_SECONDS,
            self._on_drag_failsafe,
        )

    def notify_drag_end(self) -> None:
        if self._closed:
            return

        if self._drag_active_count > 0:
            self._drag_active_count -= 1

        self._last_drag_end_monotonic = time.monotonic()

        if self._drag_active_count == 0:
            if self._drag_failsafe_source:
                try:
                    GLib.source_remove(self._drag_failsafe_source)
                except Exception as exc:
                    logger.debug("Cannot remove drag failsafe: %s", exc)
                self._drag_failsafe_source = 0

            self.clear_all_drop_styles()
            self.request_sync(delay_ms=SYNC_AFTER_DRAG_END_MS)

    def _on_drag_failsafe(self) -> bool:
        """
        Failsafe in case drag-end is not emitted for any reason.

        Prevents the GUI from being permanently click-locked.
        """
        if self._closed:
            return False

        self._drag_failsafe_source = 0

        if self._drag_active_count > 0:
            logger.warning("Drag failsafe triggered: resetting drag state.")
            self._drag_active_count = 0
            self._last_drag_end_monotonic = time.monotonic()
            self.clear_all_drop_styles()
            self.request_sync(delay_ms=SYNC_AFTER_DRAG_END_MS)

        return False

    def clear_all_drop_styles(self) -> None:
        for ws in self.workspaces.values():
            ws.clear_drop_style()

    def should_suppress_click(self) -> bool:
        """
        Return True when the current click may be a side effect of drag & drop
        or when an IPC mutation is still pending.
        """
        if self._closed:
            return True

        if self._drag_active_count > 0:
            return True

        if self._pending_mutations > 0:
            return True

        elapsed = time.monotonic() - self._last_drag_end_monotonic
        if elapsed < DRAG_RELEASE_SUPPRESS_SECONDS:
            return True

        return False

    # -------------------------------------------------------------------------
    # User actions
    # -------------------------------------------------------------------------

    def activate_app(self, client: ClientState) -> None:
        """Click app: switch workspace + focus app, but do not exit."""
        if self._closed:
            return

        if self.should_suppress_click():
            return

        def task() -> None:
            if self._closed:
                return

            success = False
            try:
                view_ok = MangoWM_IPC.dispatch_view(client.ws)
                MangoWM_IPC.dispatch_focus(client.id)
                success = view_ok
            except Exception as exc:
                logger.error("Error while activating app %s: %s", client.id, exc)
            finally:
                if not self._closed:
                    GLib.idle_add(self._on_user_action_done, success)

        self._ipc_worker.submit(task)

    def activate_workspace(self, ws_id: int) -> None:
        """
        Click workspace:
          - move overview to that workspace,
          - switch view,
          - focus overview,
          - do not exit.
        """
        if self._closed:
            return

        if self.should_suppress_click():
            return

        if not (1 <= ws_id <= NUM_WORKSPACES):
            return

        def task() -> None:
            if self._closed:
                return

            success = False
            try:
                success = self._move_overview_to_ws_blocking(ws_id, focus_self=True)
            except Exception as exc:
                logger.error("Error while activating workspace %s: %s", ws_id, exc)
            finally:
                if not self._closed:
                    GLib.idle_add(self._on_user_action_done, success)

        self._ipc_worker.submit(task)

    def _on_user_action_done(self, success: bool) -> bool:
        if self._closed:
            return False

        if not success:
            self._ipc_available = False
            self._update_hint(False)
        else:
            # Watch streams will often update state already; this is a fallback.
            self.request_sync(delay_ms=SYNC_AFTER_DROP_MS)

        return False

    def move_client(self, client_id: str, source_ws: int, target_ws: int) -> None:
        if self._closed:
            return

        client_id = (client_id or "").strip()
        if not client_id:
            return

        if not (1 <= target_ws <= NUM_WORKSPACES):
            return

        if source_ws == target_ws:
            self.request_sync(delay_ms=SYNC_AFTER_DROP_MS)
            return

        # Never allow moving the overview itself via drag & drop.
        if self._own_client_id and client_id == self._own_client_id:
            logger.warning("Refusing to move the overview window itself.")
            return

        self._pending_mutations += 1

        def task() -> None:
            try:
                if self._closed:
                    return

                MangoWM_IPC.move_client_to_workspace(
                    client_id=client_id,
                    target_ws=target_ws,
                    source_ws=source_ws,
                    own_client_id=self._own_client_id,
                )
            except Exception as exc:
                logger.error("Error while moving client %s: %s", client_id, exc)
            finally:
                if not self._closed:
                    GLib.idle_add(self._on_mutation_done)

        self._ipc_worker.submit(task)

    def _on_mutation_done(self) -> bool:
        if self._closed:
            return False

        if self._pending_mutations > 0:
            self._pending_mutations -= 1

        self.request_sync(delay_ms=SYNC_AFTER_DROP_MS)
        return False

    # -------------------------------------------------------------------------
    # Events
    # -------------------------------------------------------------------------

    def on_bg_clicked(
        self,
        _widget: Gtk.Widget,
        event: Gdk.EventButton,
    ) -> bool:
        # Intentionally no close. ESC is the exit trigger.
        return False

    def on_key_press(
        self,
        _widget: Gtk.Widget,
        event: Gdk.EventKey,
    ) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            self.close_app()
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

    esc_binder = GlobalEscBinder(single.sock_path)
    esc_binder.install()

    app: Optional[GridOverview] = None

    try:
        app = GridOverview(single)
        Gtk.main()
    except KeyboardInterrupt:
        if app is not None:
            app.close_app()
    finally:
        esc_binder.release()
        single.release()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
