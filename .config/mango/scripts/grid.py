#!/usr/bin/env python3
#
# MangoWM Grid Overview Workspace
# Production-ready GTK3/PyGObject implementation.
# UX-polished edition.
#
# Runtime dependencies:
#   - Python 3.9+
#   - GTK3
#   - PyGObject
#   - MangoWM
#   - mmsg (MangoWM IPC client, must be available in PATH)
#   - A valid icon theme (Adwaita or any desktop icon theme)
#
# Exit behavior:
#   - ESC inside overview: exit.
#   - Click a workspace tile: switch to that workspace and exit.
#   - Drag & drop apps between workspaces: move app only, DO NOT exit.
#   - Click an app:
#       * By default: focus app and close overview if EXIT_ON_APP_CLICK=True.
#       * Set EXIT_ON_APP_CLICK = False if you want click-app to keep overview open.
#
# UX keys:
#   - c: Compact density
#   - l: Large density
#   - p: Toggle low-power mode
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
import signal
import socket
import string
import subprocess
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

LOW_POWER_POLL_INTERVAL_MS = 4000
LOW_POWER_POLL_INTERVAL_IPC_DOWN_MS = 8000

SYNC_DEBOUNCE_MS = 160
SYNC_DEBOUNCE_LOW_POWER_MS = 240

WATCH_EVENT_SYNC_DEBOUNCE_MS = 120
WATCH_EVENT_SYNC_DEBOUNCE_LOW_POWER_MS = 180

SYNC_AFTER_DROP_MS = 140
SYNC_AFTER_DRAG_END_MS = 220

DRAG_RELEASE_SUPPRESS_SECONDS = 0.28
DRAG_FAILSAFE_SECONDS = 10

IPC_COMMAND_TIMEOUT = 0.90
IPC_DISPATCH_TIMEOUT = 0.60
IPC_CLIENT_QUERY_TIMEOUT = 0.50

MOVE_VERIFY_SLEEP_SECONDS = 0.015
MOVE_LATE_CHECK_SLEEP_SECONDS = 0.020
WATCH_RESTART_DELAY_SECONDS = 2.0

MAX_CACHED_ICONS = 512

APP_NAME = "mangowm-grid-overview"
WINDOW_TITLE = "MangoWM Grid Overview"
WINDOW_ROLE = "mangowm-grid-overview"

APP_NAME_LOWER = APP_NAME.lower()
WINDOW_TITLE_LOWER = WINDOW_TITLE.lower()
WINDOW_ROLE_LOWER = WINDOW_ROLE.lower()
OWN_PID = os.getpid()

LOCK_FILE_NAME = "mangowm-grid-overview.lock"
SOCKET_FILE_NAME = "mangowm-grid-overview.sock"
UI_SETTINGS_FILE_NAME = "mangowm-grid-overview-ui.json"

IPC_MESSAGE_SHOW = "show"
IPC_MESSAGE_QUIT = "quit"

SILENT_MOVE_ENV_VAR = "GRID_OVERVIEW_SILENT_MOVE"

# If True, clicking an app will focus it and close the overview.
# If False, clicking an app will focus it but keep the overview open.
EXIT_ON_APP_CLICK = True

CLIENT_ID_KEYS = ["id", "client_id", "address", "window", "window_id"]
CLIENT_TITLE_KEYS = ["title", "name", "window_title"]
CLIENT_APP_KEYS = ["appid", "app_id", "class", "wm_class", "instance", "command"]
CLIENT_ROLE_KEYS = ["role", "window_role", "instance", "startup_id"]

FADE_IN_STEP_MS = 16
FADE_IN_STEPS = 8

DND_TARGETS = [
    Gtk.TargetEntry.new("text/plain", 0, 0),
    Gtk.TargetEntry.new("STRING", 0, 0),
]


# =============================================================================
# CSS template
# =============================================================================

CSS_TEMPLATE = string.Template("""
window {
    background-color: rgba(25, 23, 36, $window_alpha);
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
    border-radius: $ws_radius;
    padding: $ws_padding;
    transition: $transition_ws;
}

.ws-btn:hover {
    border-color: #ebbcba;
    background-color: rgba(235, 188, 186, 0.14);
    box-shadow: $shadow_hover;
}

.ws-active {
    border-color: #c4a7e7;
    background-color: rgba(196, 167, 231, 0.16);
    box-shadow: $shadow_active;
}

.ws-drop {
    border: 2px dashed #eb6f92;
    background-color: rgba(235, 110, 146, 0.32);
    box-shadow: $shadow_drop;
}

.ws-label {
    color: #ebbcba;
    font-size: $title_font;
    font-weight: bold;
    margin-bottom: 8px;
}

.app-container {
    background-color: transparent;
}

.app-box {
    background-color: rgba(110, 106, 134, 0.38);
    border: 1px solid transparent;
    border-radius: $app_radius;
    padding: $app_padding;
    margin-bottom: $app_margin;
    transition: $transition_app;
}

.app-box:hover {
    background-color: #ebbcba;
    border-color: rgba(25, 23, 36, 0.25);
}

.app-box:hover label {
    color: #191724;
}

.app-box-dragging {
    background-color: #eb6f92;
    opacity: 0.45;
}

.app-label {
    color: #e0def4;
    font-size: $label_font;
    margin-left: 8px;
    transition: $transition_label;
}

.hint-label {
    color: #908caa;
    font-size: $hint_font;
    margin-top: 18px;
}
""")


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
    Return a per-user runtime directory for lock/socket/settings files.

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


def coerce_bool(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# =============================================================================
# UI settings
# =============================================================================


class UiSettings:
    """
    Persistent UI settings.

    density:
      - "compact"
      - "large"

    low_power:
      - True: disable expensive visual effects and reduce background work.
    """

    def __init__(self) -> None:
        self.path = os.path.join(get_runtime_dir(), UI_SETTINGS_FILE_NAME)
        self.density = "large"
        self.low_power = False

        self._load()
        self._apply_env_overrides()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except Exception as exc:
            logger.debug("Cannot load UI settings: %s", exc)
            return

        if not isinstance(data, dict):
            return

        density = str(data.get("density", "")).strip().lower()
        if density in ("compact", "large"):
            self.density = density

        self.low_power = bool(data.get("low_power", False))

    def _apply_env_overrides(self) -> None:
        density = os.environ.get("GRID_OVERVIEW_DENSITY", "").strip().lower()
        if density in ("compact", "large"):
            self.density = density

        low_power = os.environ.get("GRID_OVERVIEW_LOW_POWER")
        if low_power is not None:
            self.low_power = coerce_bool(low_power)

    def save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "density": self.density,
                        "low_power": self.low_power,
                    },
                    f,
                    indent=2,
                )
        except Exception as exc:
            logger.debug("Cannot save UI settings: %s", exc)


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
    focused_client_id: Optional[str] = None
    own_client_id: Optional[str] = None


# =============================================================================
# MangoWM IPC
# =============================================================================


class MangoWM_IPC:
    """
    IPC layer for MangoWM through `mmsg`.

    All commands are intended to be executed from a worker thread, never
    directly from the GTK main thread.
    """

    # Cache successful dispatch variant indexes to avoid retrying invalid
    # syntaxes repeatedly.
    _DISPATCH_CACHE: Dict[str, int] = {}

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
    def _ordered_indexed_variants(
        cache_key: str,
        variants: List[List[str]],
    ) -> List[Tuple[int, List[str]]]:
        """
        Return variants ordered so that the last successful variant is tried
        first.
        """
        indices = list(range(len(variants)))
        cached_idx = MangoWM_IPC._DISPATCH_CACHE.get(cache_key)

        if cached_idx is not None and cached_idx in indices:
            indices.remove(cached_idx)
            indices.insert(0, cached_idx)

        return [(idx, variants[idx]) for idx in indices]

    @staticmethod
    def _dispatch_variants_cached(
        cache_key: str,
        variants: List[List[str]],
        action_name: str,
    ) -> bool:
        """Try multiple dispatch syntax variants, caching successful syntax."""
        if not variants:
            log_throttled(
                logging.ERROR,
                f"dispatch-empty:{action_name}",
                "No dispatch variants available for `%s`.",
                action_name,
            )
            return False

        for idx, args in MangoWM_IPC._ordered_indexed_variants(cache_key, variants):
            if MangoWM_IPC._dispatch(args, log_errors=False):
                MangoWM_IPC._DISPATCH_CACHE[cache_key] = idx
                return True

        log_throttled(
            logging.ERROR,
            f"dispatch-failed:{action_name}",
            "Cannot dispatch action `%s` after %s command variant(s).",
            action_name,
            len(variants),
        )
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
    def _parse_focus_info(data: Any) -> Tuple[Optional[str], Optional[int]]:
        """
        Parse `get focusing-client` output.

        Returns:
          (focused_client_id, current_workspace_guess)
        """
        if isinstance(data, (list, tuple)) and data:
            data = data[0]

        if not isinstance(data, dict):
            return None, None

        client_id = MangoWM_IPC._first_str(data, CLIENT_ID_KEYS)

        tags = MangoWM_IPC.parse_tags(data.get("tags"))
        ws = tags[0] if tags else None

        nested = data.get("client")
        if isinstance(nested, dict):
            if client_id is None:
                client_id = MangoWM_IPC._first_str(nested, CLIENT_ID_KEYS)

            if ws is None:
                nested_tags = MangoWM_IPC.parse_tags(nested.get("tags"))
                if nested_tags:
                    ws = nested_tags[0]

        if ws is None:
            ws = MangoWM_IPC._extract_ws_index(data.get("workspace"))

        return client_id, ws

    @staticmethod
    def _is_own_client(item: Dict[str, Any], exclude_client_id: Optional[str]) -> bool:
        """Return True when this client item looks like this overview window."""
        client_id = MangoWM_IPC._first_str(item, CLIENT_ID_KEYS)
        if exclude_client_id and client_id and client_id == exclude_client_id:
            return True

        pid = MangoWM_IPC._extract_pid(item)
        if pid is not None and pid == OWN_PID:
            return True

        title = MangoWM_IPC._first_str(item, CLIENT_TITLE_KEYS) or ""
        if title.strip().lower() == WINDOW_TITLE_LOWER:
            return True

        role = MangoWM_IPC._first_str(item, CLIENT_ROLE_KEYS) or ""
        if WINDOW_ROLE_LOWER in role.lower():
            return True

        app = MangoWM_IPC._first_str(item, CLIENT_APP_KEYS) or ""
        if APP_NAME_LOWER in app.lower():
            return True

        return False

    # -------------------------------------------------------------------------
    # State queries
    # -------------------------------------------------------------------------

    @staticmethod
    def get_focus_info() -> Tuple[Optional[str], Optional[int]]:
        """
        Query focusing-client once and extract both focus id and workspace.
        """
        data = MangoWM_IPC._query(["get", "focusing-client"], log_errors=False)
        return MangoWM_IPC._parse_focus_info(data)

    @staticmethod
    def _get_current_ws_fallback() -> int:
        """Fallback current workspace queries when focusing-client is absent."""
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
    def get_focus_and_current_ws() -> Tuple[Optional[str], int]:
        focus_id, ws = MangoWM_IPC.get_focus_info()
        if ws:
            return focus_id, ws
        return focus_id, MangoWM_IPC._get_current_ws_fallback()

    @staticmethod
    def get_current_ws() -> int:
        _, ws = MangoWM_IPC.get_focus_info()
        if ws:
            return ws
        return MangoWM_IPC._get_current_ws_fallback()

    @staticmethod
    def get_focused_client_id() -> Optional[str]:
        focus_id, _ = MangoWM_IPC.get_focus_info()
        return focus_id

    @staticmethod
    def get_overview_state(
        exclude_client_id: Optional[str] = None,
        exclude_own: bool = True,
    ) -> OverviewState:
        focus_id, current_ws = MangoWM_IPC.get_focus_info()
        if current_ws is None:
            current_ws = MangoWM_IPC._get_current_ws_fallback()

        data = MangoWM_IPC._query(["get", "all-clients"], log_errors=True)

        ipc_available = data is not None
        clients_by_ws: Dict[int, List[ClientState]] = {
            i: [] for i in range(1, NUM_WORKSPACES + 1)
        }
        seen: Dict[int, Set[str]] = {i: set() for i in range(1, NUM_WORKSPACES + 1)}

        raw_clients = MangoWM_IPC._extract_client_list(data)

        own_found: Optional[str] = exclude_client_id

        for item in raw_clients:
            if not isinstance(item, dict):
                continue

            if exclude_own and MangoWM_IPC._is_own_client(item, exclude_client_id):
                if own_found is None:
                    own_id = MangoWM_IPC._first_str(item, CLIENT_ID_KEYS)
                    if own_id:
                        own_found = own_id
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
            focused_client_id=focus_id,
            own_client_id=own_found,
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

    @staticmethod
    def _get_client_ws_list(client_id: str) -> Optional[List[int]]:
        """
        Lightweight query for a single client's workspace list.

        Uses `mmsg get client <id>` when available, which is much cheaper than
        querying all clients for verification.
        """
        client_id = (client_id or "").strip()
        if not client_id:
            return None

        data = MangoWM_IPC._query(
            ["get", "client", client_id],
            timeout=IPC_CLIENT_QUERY_TIMEOUT,
            log_errors=False,
        )

        if isinstance(data, (list, tuple)):
            if data:
                data = data[0]
            else:
                return []

        if not isinstance(data, dict):
            return None

        ws_list = MangoWM_IPC.parse_tags(data.get("tags"))
        if ws_list:
            return ws_list

        nested = data.get("client")
        if isinstance(nested, dict):
            ws_list = MangoWM_IPC.parse_tags(nested.get("tags"))
            if ws_list:
                return ws_list

        ws = MangoWM_IPC._extract_ws_index(data.get("workspace", data.get("tag")))
        if ws:
            return [ws]

        return []

    # -------------------------------------------------------------------------
    # Basic dispatch actions
    # -------------------------------------------------------------------------

    @staticmethod
    def dispatch_view(ws: int) -> bool:
        if not (1 <= ws <= NUM_WORKSPACES):
            return False

        variants: List[List[str]] = [
            [f"view,{ws}"],
            [f"view,{ws},0"],
        ]
        return MangoWM_IPC._dispatch_variants_cached(
            "view",
            variants,
            f"view workspace {ws}",
        )

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
        return MangoWM_IPC._dispatch_variants_cached(
            "focus",
            variants,
            f"focus client {client_id}",
        )

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

        return MangoWM_IPC._dispatch_variants_cached(
            "tag",
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
        ws_list = MangoWM_IPC._get_client_ws_list(client_id)

        if ws_list is not None:
            return ws in ws_list

        # Fallback: heavier all-clients query.
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

        for idx, args in MangoWM_IPC._ordered_indexed_variants("tagsilent", variants):
            if not MangoWM_IPC._dispatch(args, log_errors=False):
                continue

            if not verify:
                MangoWM_IPC._DISPATCH_CACHE["tagsilent"] = idx
                return True

            time.sleep(MOVE_VERIFY_SLEEP_SECONDS)
            if MangoWM_IPC._verify_client_on_ws(client_id, ws):
                MangoWM_IPC._DISPATCH_CACHE["tagsilent"] = idx
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

        for idx, args in MangoWM_IPC._ordered_indexed_variants("tagsilent", variants):
            if not MangoWM_IPC._dispatch(args, log_errors=False):
                continue

            if MangoWM_IPC._verify_move_state(client_id, source_ws, target_ws):
                MangoWM_IPC._DISPATCH_CACHE["tagsilent"] = idx
                return True

            time.sleep(MOVE_VERIFY_SLEEP_SECONDS)

            if MangoWM_IPC._verify_move_state(client_id, source_ws, target_ws):
                MangoWM_IPC._DISPATCH_CACHE["tagsilent"] = idx
                return True

        return False

    @staticmethod
    def _verify_move_state(client_id: str, source_ws: int, target_ws: int) -> bool:
        """
        Verify that the client is present on target workspace.

        If source_ws is known and different from target_ws, also verify that
        the client is no longer present on source workspace.
        """
        ws_list = MangoWM_IPC._get_client_ws_list(client_id)

        if ws_list is not None:
            on_target = target_ws in ws_list
            if not on_target:
                return False

            if 1 <= source_ws <= NUM_WORKSPACES and source_ws != target_ws:
                if source_ws in ws_list:
                    return False

            return True

        # Fallback: heavier all-clients query.
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
            current_focus, current_ws = MangoWM_IPC.get_focus_and_current_ws()

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

        try:
            state = MangoWM_IPC.get_overview_state(
                exclude_client_id=own_client_id,
                exclude_own=True,
            )
        except Exception as exc:
            logger.error("Cannot prepare move context: %s", exc)
            return False

        if not state.ipc_available:
            logger.warning("IPC unavailable; refusing move.")
            return False

        if own_client_id is None and state.own_client_id:
            own_client_id = state.own_client_id

        if own_client_id and client_id == own_client_id:
            logger.warning("Refusing to move the overview window itself.")
            return False

        prev_ws = state.current_ws
        prev_focus = state.focused_client_id

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
        self.set_can_focus(False)

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
        self.label.set_ellipsize(Pango.EllipsizeMode.END)
        self.label.get_style_context().add_class("app-label")

        # Apply current density metrics.
        self.label.set_lines(self.main_window.label_lines)
        self.label.set_max_width_chars(self.main_window.label_max_chars)

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
        self.icon.set_pixel_size(self.main_window.icon_pixel_size)

        title = self.client.title.strip().replace("\n", " ")
        if not title:
            title = self.client.app_id.strip() or "Unknown Window"

        self.label.set_text(title)

        if self.main_window.settings.low_power:
            self.set_has_tooltip(False)
        else:
            self.set_has_tooltip(True)
            self.set_tooltip_text(title)

    def update(self, new_client: ClientState) -> None:
        if self.client != new_client:
            self.client = new_client
            self._render()

    def apply_metrics(self) -> None:
        """Re-apply density/DPI metrics after settings change."""
        self.label.set_lines(self.main_window.label_lines)
        self.label.set_max_width_chars(self.main_window.label_max_chars)
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

        # Click app:
        #   - focus app
        #   - exit only if EXIT_ON_APP_CLICK is True
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
        self._current_order: List[str] = []
        self._active = False
        self._drag_leave_source = 0

        self.set_visible_window(True)
        self.set_can_focus(False)

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
        self.scroll.set_shadow_type(Gtk.ShadowType.NONE)

        try:
            self.scroll.set_overlay_scrolling(True)
        except Exception:
            pass

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
        - Reorder only when order actually changes.
        """
        new_order = [c.id for c in ws_clients]
        new_ids = set(new_order)
        current_ids = set(self.app_widgets.keys())

        for dead_id in current_ids - new_ids:
            widget = self.app_widgets.pop(dead_id)
            try:
                self.app_container.remove(widget)
                widget.destroy()
            except Exception as exc:
                logger.debug("Cannot destroy app widget %s: %s", dead_id, exc)

        for client in ws_clients:
            widget = self.app_widgets.get(client.id)

            if widget is None:
                widget = AppWidget(client, self.main_window)
                self.app_widgets[client.id] = widget
                self.app_container.pack_start(widget, False, False, 0)
                widget.show_all()
            else:
                widget.update(client)

        if new_order != self._current_order:
            for index, client_id in enumerate(new_order):
                widget = self.app_widgets.get(client_id)
                if widget is None:
                    continue

                try:
                    self.app_container.reorder_child(widget, index)
                except Exception as exc:
                    logger.debug(
                        "Cannot reorder app widget %s to index %s: %s",
                        client_id,
                        index,
                        exc,
                    )

            self._current_order = new_order

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
        #   - switch workspace
        #   - exit overview
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
        # Drag & drop must never close the overview.
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
        self.settings = UiSettings()

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
        self._fade_source = 0
        self._fade_step = 0

        self._ipc_available = True
        self._last_state: Optional[OverviewState] = None
        self._own_client_id: Optional[str] = None

        self._last_hint = ""
        self._last_cell_size: Tuple[int, int] = (0, 0)
        self._last_allocation_size: Tuple[int, int] = (0, 0)

        self._css_provider: Optional[Gtk.CssProvider] = None

        # UI metrics, updated by settings/DPI.
        self._grid_spacing = 24
        self.icon_pixel_size = 24
        self.label_lines = 2
        self.label_max_chars = 24

        self.icon_provider = IconProvider()
        self._ipc_worker = IPCWorker()
        self._ipc_worker.start()

        self._watch_workers: List[MmsgWatchWorker] = []

        self.workspaces: Dict[int, WorkspaceWidget] = {}

        self._update_metrics()
        self._setup_window_backend()
        self._load_css_provider()
        self._setup_ui()
        self._setup_instance_socket_watch()
        self._start_watch_workers()

        self.connect("destroy", self._on_destroy)
        self.connect("key-press-event", self.on_key_press)
        self.connect("size-allocate", self._on_size_allocate)

        self.show_all()
        self.present()

        GLib.idle_add(self._center_window_idle)

        self._update_hint(True)
        self.request_sync(immediate=True)
        self._schedule_poll(self._current_poll_interval())

        # Fade-in animation, disabled in low-power mode.
        if self.settings.low_power:
            self.set_opacity(1.0)
        else:
            self.set_opacity(0.0)
            self._fade_source = GLib.timeout_add(FADE_IN_STEP_MS, self._fade_in_step)

    # -------------------------------------------------------------------------
    # Settings / metrics / CSS
    # -------------------------------------------------------------------------

    def _update_metrics(self) -> None:
        compact = self.settings.density == "compact"

        self._grid_spacing = 14 if compact else 24
        self.label_lines = 1 if (compact or self.settings.low_power) else 2
        self.label_max_chars = 18 if compact else 24
        self.icon_pixel_size = self._compute_icon_pixel_size()

    def _compute_icon_pixel_size(self) -> int:
        """
        Compute icon pixel size from density and DPI.

        GTK usually handles HiDPI scaling internally, but using screen
        resolution gives a more natural physical size across displays.
        """
        compact = self.settings.density == "compact"
        low_power = self.settings.low_power

        if compact:
            base = 16
        else:
            base = 22 if low_power else 28

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

        scale = dpi / 96.0
        scale = max(1.0, min(2.0, scale))

        size = int(base * scale)

        if low_power:
            size = min(size, 16 if compact else 24)

        return max(16, min(64, size))

    def _load_css_provider(self) -> None:
        screen = Gdk.Screen.get_default()
        if screen is None:
            logger.error("Cannot get default Gdk.Screen; skipping CSS.")
            return

        compact = self.settings.density == "compact"
        low_power = self.settings.low_power

        if low_power:
            transition_ws = "none"
            transition_app = "none"
            transition_label = "none"
            shadow_hover = "none"
            shadow_active = "none"
            shadow_drop = "none"
        else:
            transition_ws = (
                "background-color 140ms ease, "
                "border-color 140ms ease, "
                "box-shadow 140ms ease"
            )
            transition_app = (
                "background-color 110ms ease, "
                "border-color 110ms ease, "
                "opacity 110ms ease"
            )
            transition_label = "color 110ms ease"

            shadow_hover = "0 2px 10px rgba(0, 0, 0, 0.25)"
            shadow_active = (
                "0 0 0 1px rgba(196, 167, 231, 0.45), "
                "0 0 18px rgba(196, 167, 231, 0.28), "
                "0 6px 18px rgba(0, 0, 0, 0.22)"
            )
            shadow_drop = (
                "inset 0 0 0 2px rgba(235, 111, 146, 0.55), "
                "0 0 18px rgba(235, 111, 146, 0.45)"
            )

        if compact:
            css_vars = {
                "window_alpha": "0.94",
                "ws_radius": "14px",
                "ws_padding": "10px",
                "title_font": "14px",
                "label_font": "12px",
                "hint_font": "12px",
                "app_radius": "7px",
                "app_padding": "5px",
                "app_margin": "4px",
            }
        else:
            css_vars = {
                "window_alpha": "0.92",
                "ws_radius": "18px",
                "ws_padding": "14px",
                "title_font": "15px",
                "label_font": "13px",
                "hint_font": "13px",
                "app_radius": "10px",
                "app_padding": "9px",
                "app_margin": "7px",
            }

        css_vars.update(
            {
                "transition_ws": transition_ws,
                "transition_app": transition_app,
                "transition_label": transition_label,
                "shadow_hover": shadow_hover,
                "shadow_active": shadow_active,
                "shadow_drop": shadow_drop,
            }
        )

        css = CSS_TEMPLATE.substitute(css_vars)

        provider = Gtk.CssProvider()
        try:
            provider.load_from_data(css.encode("utf-8"))
        except Exception as exc:
            logger.error("Cannot load CSS: %s", exc)
            return

        if self._css_provider is not None:
            try:
                Gtk.StyleContext.remove_provider_for_screen(screen, self._css_provider)
            except Exception as exc:
                logger.debug("Cannot remove old CSS provider: %s", exc)

        try:
            Gtk.StyleContext.add_provider_for_screen(
                screen,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
            self._css_provider = provider
        except Exception as exc:
            logger.error("Cannot add CSS provider: %s", exc)

    def _apply_settings(self, restart_watch: bool = False) -> None:
        if self._closed:
            return

        self._update_metrics()
        self._load_css_provider()

        try:
            self.grid.set_column_spacing(self._grid_spacing)
            self.grid.set_row_spacing(self._grid_spacing)
        except Exception as exc:
            logger.debug("Cannot update grid spacing: %s", exc)

        for ws in self.workspaces.values():
            for app_widget in ws.app_widgets.values():
                app_widget.apply_metrics()

        self._refresh_cell_sizes()
        self._update_hint(self._ipc_available)

        if restart_watch:
            self._stop_watch_workers()
            self._start_watch_workers()

        self._schedule_poll(self._current_poll_interval())

    def set_density(self, density: str) -> None:
        density = density.strip().lower()
        if density not in ("compact", "large"):
            return

        if self.settings.density == density:
            return

        # Avoid changing layout while drag & drop is active.
        if self._drag_active_count > 0 or self._pending_mutations > 0:
            return

        self.settings.density = density
        self.settings.save()
        self._apply_settings(restart_watch=False)

    def toggle_low_power(self) -> None:
        # Avoid changing layout while drag & drop is active.
        if self._drag_active_count > 0 or self._pending_mutations > 0:
            return

        self.settings.low_power = not self.settings.low_power
        self.settings.save()
        self._apply_settings(restart_watch=True)

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
                default_w = max(720, min(1280, geom.width - 120))
                default_h = max(520, min(860, geom.height - 120))

        self.set_default_size(default_w, default_h)
        self.set_size_request(720, 560)

    def _setup_ui(self) -> None:
        self.bg_eventbox = Gtk.EventBox()
        self.bg_eventbox.set_visible_window(True)
        self.bg_eventbox.set_can_focus(False)
        self.bg_eventbox.get_style_context().add_class("bg-overlay")
        self.bg_eventbox.set_events(
            self.bg_eventbox.get_events()
            | Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
        )

        # Background click intentionally does nothing.
        # Exit is triggered by workspace click or ESC only.
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
        self.grid.set_column_spacing(self._grid_spacing)
        self.grid.set_row_spacing(self._grid_spacing)
        self.grid.set_halign(Gtk.Align.CENTER)
        self.grid.set_valign(Gtk.Align.CENTER)

        self.outer_box.pack_start(self.grid, False, False, 0)

        self.hint_label = Gtk.Label()
        self.hint_label.get_style_context().add_class("hint-label")
        self.hint_label.set_justify(Gtk.Justification.CENTER)
        self.hint_label.set_max_width_chars(120)
        self.hint_label.set_line_wrap(True)

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

    def _start_watch_workers(self) -> None:
        if self._closed:
            return

        watch_commands: List[List[str]] = [
            ["watch", "all-clients"],
            ["watch", "focusing-client"],
        ]

        # In low-power mode, skip all-tags watcher to reduce process count and
        # IPC noise. Polling still acts as a fallback.
        if not self.settings.low_power:
            watch_commands.append(["watch", "all-tags"])

        for cmd in watch_commands:
            worker = MmsgWatchWorker(cmd, self._notify_watch_event)
            worker.start()
            self._watch_workers.append(worker)

    def _stop_watch_workers(self) -> None:
        for worker in self._watch_workers:
            try:
                worker.stop()
            except Exception as exc:
                logger.debug("Cannot stop watch worker: %s", exc)

        self._watch_workers = []

    # -------------------------------------------------------------------------
    # Animation
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
            "_fade_source",
        ):
            source = getattr(self, source_attr, 0)
            if source:
                try:
                    GLib.source_remove(source)
                except Exception as exc:
                    logger.debug("Cannot remove GLib source %s: %s", source_attr, exc)
                setattr(self, source_attr, 0)

        self._stop_watch_workers()

        try:
            self._ipc_worker.stop()
        except Exception as exc:
            logger.debug("Cannot stop IPC worker: %s", exc)

        try:
            self.single_instance.release()
        except Exception as exc:
            logger.debug("Cannot release single-instance resources: %s", exc)

        if self._css_provider is not None:
            screen = Gdk.Screen.get_default()
            if screen is not None:
                try:
                    Gtk.StyleContext.remove_provider_for_screen(
                        screen,
                        self._css_provider,
                    )
                except Exception as exc:
                    logger.debug("Cannot remove CSS provider: %s", exc)

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

        delay = (
            WATCH_EVENT_SYNC_DEBOUNCE_LOW_POWER_MS
            if self.settings.low_power
            else WATCH_EVENT_SYNC_DEBOUNCE_MS
        )
        self.request_sync(delay_ms=delay)
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
                OWN_PID,
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

        self._last_allocation_size = (allocation.width, allocation.height)
        self._update_cell_sizes(allocation.width, allocation.height)

    def _refresh_cell_sizes(self) -> None:
        width, height = self._last_allocation_size

        if width < 100 or height < 100:
            width, height = self.get_size()

        self._last_cell_size = (0, 0)
        self._update_cell_sizes(width, height)

    def _update_cell_sizes(self, width: int, height: int) -> None:
        if self._closed:
            return

        if width < 100 or height < 100:
            return

        compact = self.settings.density == "compact"
        spacing = self._grid_spacing

        # Estimated chrome/padding overhead.
        chrome_w = 90 if compact else 110
        chrome_h = 120 if compact else 160

        cell_w = (
            width - chrome_w - spacing * (GRID_COLUMNS - 1)
        ) // GRID_COLUMNS
        cell_h = (
            height - chrome_h - spacing * (GRID_ROWS - 1)
        ) // GRID_ROWS

        if compact:
            min_w, max_w = 160, 320
            min_h, max_h = 100, 240
        else:
            min_w, max_w = 200, 420
            min_h, max_h = 140, 320

        cell_w = max(min_w, min(max_w, cell_w))
        cell_h = max(min_h, min(max_h, cell_h))

        new_size = (cell_w, cell_h)
        if new_size == self._last_cell_size:
            return

        self._last_cell_size = new_size

        for ws in self.workspaces.values():
            ws.set_cell_size(cell_w, cell_h)

    # -------------------------------------------------------------------------
    # Polling & sync
    # -------------------------------------------------------------------------

    def _current_poll_interval(self) -> int:
        if self._ipc_available:
            return (
                LOW_POWER_POLL_INTERVAL_MS
                if self.settings.low_power
                else POLL_INTERVAL_MS
            )

        return (
            LOW_POWER_POLL_INTERVAL_IPC_DOWN_MS
            if self.settings.low_power
            else POLL_INTERVAL_IPC_DOWN_MS
        )

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
            delay_ms = (
                SYNC_DEBOUNCE_LOW_POWER_MS
                if self.settings.low_power
                else SYNC_DEBOUNCE_MS
            )

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

        state: Optional[OverviewState] = None

        try:
            state = MangoWM_IPC.get_overview_state(
                exclude_client_id=self._own_client_id,
                exclude_own=True,
            )
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
            if state.own_client_id and self._own_client_id != state.own_client_id:
                self._own_client_id = state.own_client_id

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
            if EXIT_ON_APP_CLICK:
                actions = (
                    "Drag app to move • Click workspace or app to switch and close"
                )
            else:
                actions = (
                    "Drag app to move • Click workspace to switch and close • "
                    "Click app to focus without closing"
                )

            density = self.settings.density.capitalize()
            performance = "Low-power" if self.settings.low_power else "Normal"

            mode_line = (
                f"Density: {density} • Performance: {performance} • "
                "Keys: C compact, L large, P performance, ESC close"
            )

            text = f"{actions}\n{mode_line}"
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

        This is the key guard preventing drag & drop from accidentally exiting
        the overview.
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
        """
        Click app:
          - switch to app workspace
          - focus app
          - exit only if EXIT_ON_APP_CLICK is True
        """
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
                    GLib.idle_add(self._on_activate_app_done, success)

        self._ipc_worker.submit(task)

    def _on_activate_app_done(self, success: bool) -> bool:
        if self._closed:
            return False

        if not success:
            self._ipc_available = False
            self._update_hint(False)

        if EXIT_ON_APP_CLICK:
            self.close_app()
        else:
            self.request_sync(delay_ms=SYNC_AFTER_DROP_MS)

        return False

    def activate_workspace(self, ws_id: int) -> None:
        """
        Click workspace:
          - switch workspace
          - exit overview
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

            try:
                MangoWM_IPC.dispatch_view(ws_id)
            except Exception as exc:
                logger.error("Error while activating workspace %s: %s", ws_id, exc)
            finally:
                if not self._closed:
                    GLib.idle_add(self._on_activate_workspace_done)

        self._ipc_worker.submit(task)

    def _on_activate_workspace_done(self) -> bool:
        if self._closed:
            return False

        # Workspace selection is an intentional exit action.
        self.close_app()
        return False

    def move_client(self, client_id: str, source_ws: int, target_ws: int) -> None:
        """
        Drag & drop move handler.

        This must never close the overview.
        """
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
        # Intentionally no close.
        # Exit is triggered by workspace click or ESC only.
        return False

    def on_key_press(
        self,
        _widget: Gtk.Widget,
        event: Gdk.EventKey,
    ) -> bool:
        key = Gdk.keyval_to_lower(event.keyval)

        if key == Gdk.KEY_Escape:
            self.close_app()
            return True

        # Ignore keybindings with major modifiers.
        if event.state & (
            Gdk.ModifierType.CONTROL_MASK
            | Gdk.ModifierType.MOD1_MASK
            | Gdk.ModifierType.SUPER_MASK
        ):
            return False

        if key == Gdk.KEY_c:
            self.set_density("compact")
            return True

        if key == Gdk.KEY_l:
            self.set_density("large")
            return True

        if key == Gdk.KEY_p:
            self.toggle_low_power()
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

    app: Optional[GridOverview] = None

    try:
        app = GridOverview(single)
        Gtk.main()
    except KeyboardInterrupt:
        if app is not None:
            app.close_app()
    finally:
        single.release()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
