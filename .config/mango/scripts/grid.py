#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Grid Overview Workspace cho MangoWM
====================================
Popup GUI dạng lưới 3x3 hiển thị 9 workspace của MangoWM.

Tính năng
---------
- Hiển thị toàn bộ 9 workspace dạng lưới 3x3, kích thước đồng nhất.
- Mỗi workspace hiển thị: tên workspace + danh sách application (icon + tên).
- Drag & Drop application giữa các workspace:
    * Chỉ di chuyển application, KHÔNG chuyển view, KHÔNG focus, KHÔNG đóng GUI.
- Single-click application:
    * Chuyển sang workspace chứa app + focus app + đóng GUI.
- Single-click workspace background:
    * Chuyển workspace + đóng GUI.
- ESC: đóng GUI.

Dependencies
------------
Cần cài đặt trước khi chạy:

| Dependency      | Arch                          | Fedora                  | Debian                  |
|-----------------|-------------------------------|-------------------------|-------------------------|
| Python >= 3.8   | python                        | python3                 | python3                 |
| GTK3            | gtk3                          | gtk3                    | libgtk-3-0              |
| PyGObject       | python-gobject                | python3-gobject         | python3-gi              |
| gtk-layer-shell | gtk-layer-shell               | gtk-layer-shell         | libgtk-layer-shell0     |
| MangoWM + mmsg  | (AUR: mango-wm / from source) | (build from source)     | (build from source)     |
| Icon theme      | adwaita-icon-theme            | adwaita-icon-theme      | adwaita-icon-theme      |

Fallback
--------
- Nếu gtk-layer-shell không có: GUI vẫn chạy ở chế độ Dialog window thường.
- Nếu mmsg không có trong PATH: GUI vẫn mở nhưng hiển thị workspace trống.
- Nếu icon không tìm thấy: hiển thị tên application (text only).

Architecture
------------
- MangoIPC     : wrapper an toàn quanh binary `mmsg` (sync + async).
- IconCache    : cache icon theo appid để tránh lookup lặp lại.
- DragState    : quản lý trạng thái drag, phân biệt click thật vs release-sau-drag.
- ClientInfo   : dataclass cho thông tin client từ IPC.
- GridOverview : window chính, build/rebuild lưới, xử lý event.
"""

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Pango, GdkPixbuf

import subprocess
import json
import logging
import shutil
import threading
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass

# === Optional: GtkLayerShell ===
try:
    gi.require_version('GtkLayerShell', '1.0')
    from gi.repository import GtkLayerShell
    HAS_LAYER_SHELL: bool = True
except (ValueError, ImportError):
    HAS_LAYER_SHELL = False


# =============================================================================
# Constants
# =============================================================================

NUM_WORKSPACES: int = 9
GRID_COLUMNS: int = 3

WORKSPACE_MIN_WIDTH: int = 180
WORKSPACE_MIN_HEIGHT: int = 130

APP_ICON_SIZE: int = 24
DRAG_ICON_SIZE: int = 32
APP_TITLE_MAX_CHARS: int = 14

REBUILD_DEBOUNCE_MS: int = 250
REBUILD_RETRY_MS: int = 50
DRAG_RELEASE_SUPPRESS_MS: int = 250
PRESSED_CLIENT_TIMEOUT_MS: int = 500
REFRESH_INTERVAL_MS: int = 5000

IPC_TIMEOUT_S: int = 2
MMSG_BIN: str = "mmsg"
MAX_TAG_BITS: int = 32


# =============================================================================
# Logging
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
log = logging.getLogger("grid-overview")


# =============================================================================
# CSS
# =============================================================================

CSS = """
window {
    background-color: rgba(25, 23, 36, 0.85);
}
.outer-box {
    background-color: transparent;
    padding: 40px;
}
.grid {
    background-color: transparent;
    border-radius: 20px;
}
.ws-btn {
    background-color: rgba(38, 35, 58, 0.6);
    border: 2px solid #6e6a86;
    border-radius: 16px;
    padding: 12px;
    color: #ebbcba;
    font-size: 15px;
    font-weight: bold;
    transition: all 150ms ease;
}
.ws-btn:hover {
    border-color: #ebbcba;
    background-color: rgba(235, 188, 186, 0.15);
}
.ws-active {
    border-color: #c4a7e7;
    background-color: rgba(196, 167, 231, 0.15);
}
.ws-drop {
    border: 3px solid #eb6f92;
    background-color: rgba(235, 111, 146, 0.25);
}
.ws-label {
    color: #c4a7e7;
    font-size: 14px;
}
.app-box {
    background-color: rgba(110, 106, 134, 0.3);
    border-radius: 8px;
    padding: 5px;
    margin: 2px;
    color: #e0def4;
    font-size: 12px;
    transition: all 100ms ease;
}
.app-box:hover {
    background-color: #ebbcba;
    color: #191724;
}
.app-box-dragging {
    background-color: #eb6f92;
    color: #e0def4;
}
.hint-label {
    color: #908caa;
    font-size: 12px;
    margin-top: 20px;
}
"""


# =============================================================================
# Data Models
# =============================================================================

@dataclass
class ClientInfo:
    """Thông tin một client/window từ MangoWM IPC."""
    id: str
    title: str
    appid: str
    tags: List[int]  # workspace IDs this client is on


# =============================================================================
# Tag Parsing
# =============================================================================

def parse_tags(tags_raw: Any) -> List[int]:
    """
    Chuyển tags từ IPC response sang list workspace ID.
    Hỗ trợ 2 dạng:
      - list of int/string: [1, 3, 5]
      - bitmask int: 0b10101 -> [1, 3, 5]
    """
    ws_list: List[int] = []
    if isinstance(tags_raw, list):
        for t in tags_raw:
            try:
                ws_list.append(int(t))
            except (ValueError, TypeError):
                continue
    elif isinstance(tags_raw, int):
        for i in range(1, MAX_TAG_BITS + 1):
            if tags_raw & (1 << (i - 1)):
                ws_list.append(i)
    return ws_list


# =============================================================================
# IPC Layer
# =============================================================================

class MangoIPC:
    """Wrapper an toàn quanh binary `mmsg` của MangoWM."""

    def __init__(self) -> None:
        self._available: bool = self._check_available()

    def _check_available(self) -> bool:
        if not shutil.which(MMSG_BIN):
            log.warning(
                "'%s' not found in PATH. IPC disabled; GUI will show empty workspaces.",
                MMSG_BIN
            )
            return False
        return True

    @property
    def available(self) -> bool:
        return self._available

    # --- Internal helpers ---

    def _run(self, args: List[str]) -> Optional[Any]:
        """Chạy `mmsg <args>` (blocking), trả về parsed JSON hoặc None."""
        if not self._available:
            return None
        try:
            res = subprocess.run(
                [MMSG_BIN] + args,
                capture_output=True, text=True, timeout=IPC_TIMEOUT_S
            )
        except subprocess.TimeoutExpired:
            log.error("mmsg %s timed out after %ds", args, IPC_TIMEOUT_S)
            return None
        except FileNotFoundError:
            log.error("mmsg binary disappeared. Disabling IPC.")
            self._available = False
            return None
        except Exception as e:
            log.exception("Unexpected error running mmsg %s: %s", args, e)
            return None

        if res.returncode != 0:
            log.error(
                "mmsg %s failed (rc=%d): %s",
                args, res.returncode, res.stderr.strip()
            )
            return None
        if not res.stdout.strip():
            return None
        try:
            return json.loads(res.stdout)
        except json.JSONDecodeError as e:
            log.error(
                "JSON parse error from mmsg %s: %s | raw: %r",
                args, e, res.stdout[:200]
            )
            return None

    def _dispatch_sync(self, actions: List[str]) -> bool:
        """
        Chạy `mmsg dispatch <actions...>` (blocking).
        Dùng cho user-initiated actions (click) cần đảm bảo hoàn tất trước khi close.
        """
        if not self._available:
            return False
        try:
            res = subprocess.run(
                [MMSG_BIN, "dispatch"] + actions,
                capture_output=True, text=True, timeout=IPC_TIMEOUT_S
            )
        except subprocess.TimeoutExpired:
            log.error("mmsg dispatch %s timed out", actions)
            return False
        except FileNotFoundError:
            log.error("mmsg binary disappeared. Disabling IPC.")
            self._available = False
            return False
        except Exception as e:
            log.exception("Unexpected error in mmsg dispatch %s: %s", actions, e)
            return False

        if res.returncode != 0:
            log.error(
                "mmsg dispatch %s failed (rc=%d): %s",
                actions, res.returncode, res.stderr.strip()
            )
            return False
        return True

    def _dispatch_async(self, actions: List[str]) -> None:
        """
        Chạy `mmsg dispatch <actions...>` (non-blocking).
        Dùng cho drag-drop move (tránh block GTK thread trong lúc drag).
        """
        if not self._available:
            return
        try:
            subprocess.Popen(
                [MMSG_BIN, "dispatch"] + actions,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except FileNotFoundError:
            log.error("mmsg binary disappeared. Disabling IPC.")
            self._available = False
        except Exception as e:
            log.error("mmsg dispatch async %s error: %s", actions, e)

    # --- Public API ---

    def get_current_workspace(self) -> int:
        """
        Lấy workspace hiện tại từ focusing-client.
        Trả về workspace ID (1-9), default 1 nếu không xác định được.
        """
        data = self._run(["get", "focusing-client"])
        if isinstance(data, dict) and "tags" in data:
            ws = parse_tags(data["tags"])
            if ws:
                return ws[0]
        log.warning("Cannot determine current workspace; defaulting to 1.")
        return 1

    def get_all_clients(self) -> List[ClientInfo]:
        """Lấy danh sách toàn bộ client từ MangoWM."""
        data = self._run(["get", "all-clients"])
        if isinstance(data, dict):
            raw_clients = data.get("clients", [])
        elif isinstance(data, list):
            raw_clients = data
        else:
            return []

        clients: List[ClientInfo] = []
        for c in raw_clients:
            try:
                client = ClientInfo(
                    id=str(c.get("id", "")),
                    title=str(c.get("title") or c.get("appid") or "Unknown"),
                    appid=str(c.get("appid") or ""),
                    tags=parse_tags(c.get("tags", [])),
                )
                if client.id:
                    clients.append(client)
            except Exception as e:
                log.warning("Failed to parse client %r: %s", c, e)
        return clients

    def switch_workspace(self, ws_id: int) -> None:
        """
        Chuyển view sang workspace ws_id.
        Lệnh: `mmsg dispatch view,{ws_id},0`
        Flag `0` = không "follow" client (tránh auto-focus side effect).
        """
        self._dispatch_sync([f"view,{ws_id},0"])

    def move_client_to_workspace(self, client_id: str, ws_id: int) -> None:
        """
        Di chuyển client sang workspace ws_id (non-blocking).
        Lệnh: `mmsg dispatch tag,{ws_id},0 client,{client_id}`

        Giải thích cú pháp (best-guess dựa trên pattern gốc):
          - `tag,{ws_id},0` : set tag đích, flag 0 = không tự chuyển view.
          - `client,{client_id}` : chọn client để áp dụng lệnh tag.

        QUAN TRỌNG: Lệnh này KHÔNG gọi `view`, nên workspace hiện tại của user
        không đổi sau drag. Đây là hành vi bắt buộc theo spec.
        """
        self._dispatch_async([f"tag,{ws_id},0", f"client,{client_id}"])

    def focus_client(self, client_id: str) -> None:
        """
        Focus một client (blocking, dùng sau khi switch workspace).
        NOTE: Cú pháp chính xác phụ thuộc MangoWM version.
        Dự đoán: `focus,{client_id}`.
        Nếu không hoạt động, kiểm tra `mmsg dispatch --help` hoặc source
        code MangoWM để điều chỉnh phương thức này.
        """
        self._dispatch_sync([f"focus,{client_id}"])


# =============================================================================
# Icon Cache
# =============================================================================

class IconCache:
    """Cache icon theo (appid, size) để tránh lookup lặp lại."""

    def __init__(self) -> None:
        self._cache: Dict[Tuple[str, int], Optional[GdkPixbuf.Pixbuf]] = {}
        try:
            self._theme: Optional[Gtk.IconTheme] = Gtk.IconTheme.get_default()
        except Exception as e:
            log.warning("Cannot get default IconTheme: %s", e)
            self._theme = None

    def get(self, appid: str, size: int = APP_ICON_SIZE) -> Optional[GdkPixbuf.Pixbuf]:
        if not appid or self._theme is None:
            return None
        key = (appid, size)
        if key in self._cache:
            return self._cache[key]
        pixbuf = self._lookup(appid, size)
        self._cache[key] = pixbuf
        return pixbuf

    def _lookup(self, appid: str, size: int) -> Optional[GdkPixbuf.Pixbuf]:
        # Tạo danh sách tên icon ứng viên
        candidates: List[str] = []
        candidates.append(appid)
        candidates.append(appid.lower())
        # Xử lý reverse-DNS: org.firefox.Firefox -> firefox, org -> firefox
        if '.' in appid:
            parts = appid.split('.')
            candidates.append(parts[-1].lower())
            if len(parts) >= 2:
                candidates.append(parts[1].lower())
            candidates.append(parts[0].lower())

        for name in candidates:
            if not name:
                continue
            try:
                p = self._theme.load_icon(name, size, Gtk.IconLookupFlags.USE_BUILTIN)
                if p is not None:
                    return p
            except GLib.Error:
                # Icon not found in theme for this name; try next candidate
                continue
            except Exception as e:
                log.debug("Icon lookup '%s' error: %s", name, e)
                continue
        return None


# =============================================================================
# Drag State
# =============================================================================

class DragState:
    """
    Quản lý trạng thái drag & drop để:
    1. Phân biệt click thật và button-release sau drag (spurious release).
       GTK3 fires drag-end BEFORE button-release, nên nếu chỉ check is_dragging
       trong release handler, release sau drag sẽ bị hiểu nhầm là click.
       Giải pháp: flag `just_ended` với timeout ngắn (250ms).
    2. Track client đang được kéo (để skip no-op drop cùng workspace).
    """

    def __init__(self) -> None:
        self.is_dragging: bool = False
        self.just_ended: bool = False
        self.source_client_id: Optional[str] = None
        self.source_tags: List[int] = []
        self._suppress_handler: Optional[int] = None

    def begin(self, client_id: str, tags: List[int]) -> None:
        self.is_dragging = True
        self.source_client_id = client_id
        self.source_tags = list(tags)

    def end(self) -> None:
        self.is_dragging = False
        self.just_ended = True
        if self._suppress_handler is not None:
            GLib.source_remove(self._suppress_handler)
        self._suppress_handler = GLib.timeout_add(
            DRAG_RELEASE_SUPPRESS_MS, self._reset_just_ended
        )

    def _reset_just_ended(self) -> bool:
        self.just_ended = False
        self._suppress_handler = None
        return False  # one-shot

    def should_suppress_click(self) -> bool:
        """Trả True nếu release này là tail của drag (phải bỏ qua click)."""
        return self.is_dragging or self.just_ended

    def is_noop_drop(self, client_id: str, target_ws: int) -> bool:
        """True nếu thả client lên workspace mà nó đang ở (và chỉ ở đó)."""
        return (
            client_id == self.source_client_id
            and len(self.source_tags) == 1
            and self.source_tags[0] == target_ws
        )

    def cleanup(self) -> None:
        """Dọn dẹp timer khi destroy."""
        if self._suppress_handler is not None:
            GLib.source_remove(self._suppress_handler)
            self._suppress_handler = None


# =============================================================================
# Main Window
# =============================================================================

class GridOverview(Gtk.Window):
    """Cửa sổ chính: lưới 3x3 workspace overview."""

    def __init__(self) -> None:
        super().__init__(title="Grid Overview")
        self.set_decorated(False)

        # --- Services ---
        self.ipc: MangoIPC = MangoIPC()
        self.icon_cache: IconCache = IconCache()
        self.drag_state: DragState = DragState()

        # --- State ---
        self._current_ws: int = 1
        self._closed: bool = False
        self._pressed_client: Optional[ClientInfo] = None
        self._pressed_timeout_handler: Optional[int] = None
        self._rebuild_handler: Optional[int] = None
        self._refresh_handler: Optional[int] = None
        self._last_signature: Tuple = ()
        self._ws_widgets: Dict[int, Gtk.Widget] = {}

        # --- Window setup ---
        self._setup_layer_shell()
        self._setup_css()
        self._build_root_layout()

        # --- Signals ---
        self.connect("destroy", self._on_destroy)
        self.connect("delete-event", self._on_delete)
        self.connect("key-press-event", self._on_key_press)

        # --- Initial state ---
        self._current_ws = self.ipc.get_current_workspace()
        self.rebuild_grid()
        self._start_refresh_timer()

    # -------------------------------------------------------------------------
    # Setup
    # -------------------------------------------------------------------------

    def _setup_layer_shell(self) -> None:
        if HAS_LAYER_SHELL:
            GtkLayerShell.init_for_window(self)
            GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
            for edge in (GtkLayerShell.Edge.TOP, GtkLayerShell.Edge.BOTTOM,
                         GtkLayerShell.Edge.LEFT, GtkLayerShell.Edge.RIGHT):
                GtkLayerShell.set_anchor(self, edge, True)
            # Keyboard grab cho ESC (hỗ trợ cả API mới và cũ)
            if hasattr(GtkLayerShell, 'set_keyboard_mode'):
                GtkLayerShell.set_keyboard_mode(
                    self, GtkLayerShell.KeyboardMode.EXCLUSIVE
                )
            elif hasattr(GtkLayerShell, 'set_keyboard_interactivity'):
                GtkLayerShell.set_keyboard_interactivity(self, True)
            else:
                log.warning(
                    "GtkLayerShell lacks keyboard API; ESC may not work."
                )
        else:
            log.warning("GtkLayerShell unavailable; falling back to Dialog window.")
            self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
            self.set_keep_above(True)
            self.set_default_size(960, 640)
            self.set_position(Gtk.WindowPosition.CENTER)

    def _setup_css(self) -> None:
        provider = Gtk.CssProvider()
        try:
            provider.load_from_data(CSS.encode('utf-8'))
            ctx = self.get_style_context()
            ctx.add_provider(provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        except GLib.Error as e:
            log.error("CSS load error: %s", e)

    def _build_root_layout(self) -> None:
        self.outer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.outer_box.get_style_context().add_class("outer-box")
        self.outer_box.set_halign(Gtk.Align.CENTER)
        self.outer_box.set_valign(Gtk.Align.CENTER)
        self.add(self.outer_box)

        self.grid = Gtk.Grid()
        self.grid.get_style_context().add_class("grid")
        self.grid.set_column_spacing(20)
        self.grid.set_row_spacing(20)
        self.grid.set_halign(Gtk.Align.CENTER)
        self.grid.set_valign(Gtk.Align.CENTER)
        self.outer_box.pack_start(self.grid, True, True, 0)

        self.hint_label = Gtk.Label(
            label="Drag apps to move  •  Click app: switch + focus  •  "
                  "Click workspace: switch  •  ESC: close"
        )
        self.hint_label.get_style_context().add_class("hint-label")
        self.hint_label.set_line_wrap(True)
        self.hint_label.set_max_width_chars(70)
        self.hint_label.set_halign(Gtk.Align.CENTER)
        self.outer_box.pack_start(self.hint_label, False, False, 0)

    # -------------------------------------------------------------------------
    # Rebuild
    # -------------------------------------------------------------------------

    def rebuild_grid(self, clients: Optional[List[ClientInfo]] = None) -> None:
        """Vẽ lại toàn bộ lưới từ IPC data (hoặc từ clients truyền vào)."""
        if self._closed:
            return

        # Clear existing children (GTK auto-disconnects signals on destroy)
        for child in self.grid.get_children():
            self.grid.remove(child)
            child.destroy()
        self._ws_widgets.clear()

        if clients is None:
            clients = self.ipc.get_all_clients()
        self._last_signature = self._clients_signature(clients)

        # Group clients by workspace
        ws_clients: Dict[int, List[ClientInfo]] = {
            i: [] for i in range(1, NUM_WORKSPACES + 1)
        }
        for c in clients:
            for ws in c.tags:
                if 1 <= ws <= NUM_WORKSPACES:
                    ws_clients[ws].append(c)

        for i in range(1, NUM_WORKSPACES + 1):
            ws_widget = self._build_workspace_widget(i, ws_clients[i])
            col = (i - 1) % GRID_COLUMNS
            row = (i - 1) // GRID_COLUMNS
            self.grid.attach(ws_widget, col, row, 1, 1)
            self._ws_widgets[i] = ws_widget

        self.show_all()

    @staticmethod
    def _clients_signature(clients: List[ClientInfo]) -> Tuple:
        """Quick signature để detect state change (id, title, tags)."""
        return tuple(sorted((c.id, c.title, tuple(c.tags)) for c in clients))

    def _build_workspace_widget(
        self, ws_id: int, clients: List[ClientInfo]
    ) -> Gtk.EventBox:
        ws_eventbox = Gtk.EventBox()
        ws_eventbox.set_visible_window(False)
        ws_eventbox.set_hexpand(True)
        ws_eventbox.set_vexpand(True)

        # Click workspace background -> switch + close
        ws_eventbox.connect("button-press-event", self._on_ws_button_press, ws_id)

        # Drop target
        ws_eventbox.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.MOVE)
        ws_eventbox.drag_dest_add_text_targets()
        ws_eventbox.connect("drag-data-received", self._on_drop, ws_id)
        ws_eventbox.connect("drag-motion", self._on_drag_motion)
        ws_eventbox.connect("drag-leave", self._on_drag_leave)

        ws_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        ws_box.get_style_context().add_class("ws-btn")
        ws_box.set_size_request(WORKSPACE_MIN_WIDTH, WORKSPACE_MIN_HEIGHT)
        if ws_id == self._current_ws:
            ws_box.get_style_context().add_class("ws-active")
        ws_eventbox.add(ws_box)

        ws_label = Gtk.Label(label=f"Workspace {ws_id}")
        ws_label.set_halign(Gtk.Align.START)
        ws_label.get_style_context().add_class("ws-label")
        ws_box.pack_start(ws_label, False, False, 0)

        app_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        app_container.set_valign(Gtk.Align.START)
        ws_box.pack_start(app_container, True, True, 0)

        for client in clients:
            app_widget = self._build_app_widget(client)
            app_container.pack_start(app_widget, False, False, 0)

        return ws_eventbox

    def _build_app_widget(self, client: ClientInfo) -> Gtk.EventBox:
        app_eventbox = Gtk.EventBox()
        app_eventbox.set_visible_window(False)

        # Click handling (press + release)
        app_eventbox.connect(
            "button-press-event", self._on_app_button_press, client
        )
        app_eventbox.connect(
            "button-release-event", self._on_app_button_release, client
        )

        # Drag source
        app_eventbox.drag_source_set(
            Gdk.ModifierType.BUTTON1_MASK, [], Gdk.DragAction.MOVE
        )
        app_eventbox.drag_source_add_text_targets()
        app_eventbox.connect("drag-data-get", self._on_drag_data_get, client.id)
        app_eventbox.connect("drag-begin", self._on_drag_begin, client)
        app_eventbox.connect("drag-end", self._on_drag_end)

        app_inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        app_inner.get_style_context().add_class("app-box")
        app_inner.set_halign(Gtk.Align.START)

        # Icon (nếu lấy được)
        icon_pixbuf = self.icon_cache.get(client.appid)
        if icon_pixbuf is not None:
            icon_img = Gtk.Image.new_from_pixbuf(icon_pixbuf)
            app_inner.pack_start(icon_img, False, False, 0)

        # Title (luôn hiển thị, wrap nếu dài, ellipsize chống overflow)
        title_label = Gtk.Label(label=client.title)
        title_label.set_max_width_chars(APP_TITLE_MAX_CHARS)
        title_label.set_line_wrap(True)
        title_label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        title_label.set_ellipsize(Pango.EllipsizeMode.END)
        title_label.set_tooltip_text(client.title)
        app_inner.pack_start(title_label, True, True, 0)

        app_eventbox.add(app_inner)
        return app_eventbox

    # -------------------------------------------------------------------------
    # Event Handlers: Click
    # -------------------------------------------------------------------------

    def _on_app_button_press(
        self, widget: Gtk.Widget, event: Gdk.EventButton, client: ClientInfo
    ) -> bool:
        """Press trên app: ghi nhận để phân biệt với ws background press."""
        if event.button != 1:
            return False
        self._pressed_client = client
        # Safety timeout: clear stale _pressed_client nếu release không fire
        # (ví dụ user kéo chuột ra ngoài widget rồi thả).
        self._schedule_pressed_timeout()
        # Return False để event lan truyền -> drag source có thể detect motion
        return False

    def _on_app_button_release(
        self, widget: Gtk.Widget, event: Gdk.EventButton, client: ClientInfo
    ) -> bool:
        """Release trên app: nếu KHÔNG phải tail của drag -> click thật."""
        if event.button != 1:
            return False
        if self._closed:
            return True

        self._cancel_pressed_timeout()
        pressed = self._pressed_client
        self._pressed_client = None

        # Bỏ qua nếu release là tail của drag (spurious release)
        if self.drag_state.should_suppress_click():
            return False
        if pressed is None:
            return False

        # Click thật -> switch ws + focus + close
        target_ws = pressed.tags[0] if pressed.tags else self._current_ws
        self._handle_app_click(pressed.id, target_ws)
        return True  # stop propagation

    def _on_ws_button_press(
        self, widget: Gtk.Widget, event: Gdk.EventButton, ws_id: int
    ) -> bool:
        """Press trên workspace background."""
        if event.button != 1:
            return False
        if event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        if self._closed:
            return True

        # Nếu press bắt đầu trên app -> không xử lý ở đây
        # (app release handler sẽ lo việc click, hoặc drag sẽ lo việc move).
        # Return True để stop propagation, không switch ws.
        if self._pressed_client is not None:
            return True

        # Click workspace background -> switch + close
        self.ipc.switch_workspace(ws_id)
        self.close()
        return True

    def _handle_app_click(self, client_id: str, ws_id: int) -> None:
        """Xử lý single-click trên app: switch ws + focus + close."""
        self.ipc.switch_workspace(ws_id)
        self.ipc.focus_client(client_id)
        self.close()

    def _schedule_pressed_timeout(self) -> None:
        """Schedule clear _pressed_client nếu release không fire kịp."""
        self._cancel_pressed_timeout()
        self._pressed_timeout_handler = GLib.timeout_add(
            PRESSED_CLIENT_TIMEOUT_MS, self._on_pressed_timeout
        )

    def _cancel_pressed_timeout(self) -> None:
        if self._pressed_timeout_handler is not None:
            GLib.source_remove(self._pressed_timeout_handler)
            self._pressed_timeout_handler = None

    def _on_pressed_timeout(self) -> bool:
        """Clear stale _pressed_client (press không có release)."""
        self._pressed_client = None
        self._pressed_timeout_handler = None
        return False  # one-shot

    # -------------------------------------------------------------------------
    # Event Handlers: Drag & Drop
    # -------------------------------------------------------------------------

    def _on_drag_data_get(
        self, widget: Gtk.Widget, ctx: Gdk.DragContext,
        data: Gtk.SelectionData, info: int, time: int, client_id: str
    ) -> None:
        data.set_text(client_id, -1)

    def _on_drag_begin(
        self, widget: Gtk.Widget, ctx: Gdk.DragContext, client: ClientInfo
    ) -> None:
        self.drag_state.begin(client.id, client.tags)
        # Drag bắt đầu -> không còn là click nữa, clear pressed_client
        self._cancel_pressed_timeout()
        self._pressed_client = None

        inner = widget.get_child()
        if inner is not None:
            inner.get_style_context().add_class("app-box-dragging")

        # Drag icon: dùng icon của app nếu có, fallback "window-new"
        pixbuf = self.icon_cache.get(client.appid, DRAG_ICON_SIZE)
        if pixbuf is not None:
            Gtk.drag_set_icon_pixbuf(ctx, pixbuf, 0, 0)
        else:
            try:
                Gtk.drag_set_icon_name(ctx, "window-new", 0, 0)
            except Exception as e:
                log.debug("drag_set_icon_name failed: %s", e)
                Gtk.drag_set_icon_default(ctx)

    def _on_drag_end(self, widget: Gtk.Widget, ctx: Gdk.DragContext) -> None:
        self.drag_state.end()
        try:
            inner = widget.get_child()
            if inner is not None:
                inner.get_style_context().remove_class("app-box-dragging")
        except Exception as e:
            # Widget có thể đã bị destroy nếu rebuild xảy ra giữa chừng
            log.debug("drag-end cleanup skipped: %s", e)

    def _on_drag_motion(
        self, widget: Gtk.Widget, ctx: Gdk.DragContext,
        x: int, y: int, time: int
    ) -> bool:
        child = widget.get_child()
        if child is not None:
            child.get_style_context().add_class("ws-drop")
        # Explicitly set drag status để drop được accept
        Gdk.drag_status(ctx, Gdk.DragAction.MOVE, time)
        return True

    def _on_drag_leave(
        self, widget: Gtk.Widget, ctx: Gdk.DragContext, time: int
    ) -> None:
        child = widget.get_child()
        if child is not None:
            child.get_style_context().remove_class("ws-drop")

    def _on_drop(
        self, widget: Gtk.Widget, ctx: Gdk.DragContext, x: int, y: int,
        data: Gtk.SelectionData, info: int, time: int, ws_id: int
    ) -> None:
        """Drop application lên workspace ws_id."""
        if self._closed:
            return

        client_id = data.get_text()
        if not client_id:
            log.debug("Drop received empty data; ignoring.")
            return

        # Skip no-op drop (thả lên ws mà client đang chỉ ở đó)
        if self.drag_state.is_noop_drop(client_id, ws_id):
            log.debug("Drop on same workspace; no-op.")
            return

        # CHỈ move client. KHÔNG view, KHÔNG focus, KHÔNG close.
        self.ipc.move_client_to_workspace(client_id, ws_id)

        # Vẽ lại (debounced) để phản ánh state mới
        self.schedule_rebuild()

        # Xóa drop highlight
        child = widget.get_child()
        if child is not None:
            child.get_style_context().remove_class("ws-drop")

    # -------------------------------------------------------------------------
    # Rebuild Scheduling
    # -------------------------------------------------------------------------

    def schedule_rebuild(self, delay_ms: int = REBUILD_DEBOUNCE_MS) -> None:
        """Debounce rebuild để tránh spam rebuild khi drop liên tục."""
        if self._closed:
            return
        if self._rebuild_handler is not None:
            GLib.source_remove(self._rebuild_handler)
        self._rebuild_handler = GLib.timeout_add(delay_ms, self._do_rebuild)

    def _do_rebuild(self) -> bool:
        self._rebuild_handler = None
        if self._closed:
            return False
        # Nếu đang drag, delay rebuild để không phá widget nguồn
        if self.drag_state.is_dragging:
            self._rebuild_handler = GLib.timeout_add(REBUILD_RETRY_MS, self._do_rebuild)
            return False
        self.rebuild_grid()
        return False  # one-shot

    # -------------------------------------------------------------------------
    # Refresh Timer (cho app open/close/title change)
    # -------------------------------------------------------------------------

    def _start_refresh_timer(self) -> None:
        """Khởi động timer kiểm tra state change mỗi REFRESH_INTERVAL_MS."""
        self._refresh_handler = GLib.timeout_add(
            REFRESH_INTERVAL_MS, self._refresh_if_changed
        )

    def _refresh_if_changed(self) -> bool:
        """
        Kiểm tra state change trong background thread (non-blocking GUI).
        Nếu ws hoặc clients thay đổi -> rebuild trên main thread.
        """
        if self._closed:
            return False
        # Skip khi đang drag hoặc rebuild pending
        if self.drag_state.is_dragging:
            return True
        if self._rebuild_handler is not None:
            return True

        def worker() -> None:
            try:
                new_ws = self.ipc.get_current_workspace()
                new_clients = self.ipc.get_all_clients()
                new_sig = self._clients_signature(new_clients)
                GLib.idle_add(self._on_refresh_done, new_sig, new_ws, new_clients)
            except Exception as e:
                log.warning("Refresh worker failed: %s", e)

        threading.Thread(target=worker, daemon=True).start()
        return True  # repeat

    def _on_refresh_done(
        self, new_sig: Tuple, new_ws: int, new_clients: List[ClientInfo]
    ) -> bool:
        """Callback trên main thread sau khi worker fetch xong."""
        if self._closed:
            return False
        ws_changed = (new_ws != self._current_ws)
        clients_changed = (new_sig != self._last_signature)
        if ws_changed or clients_changed:
            if ws_changed:
                self._current_ws = new_ws
            log.debug("State changed (ws=%s, clients=%s); refreshing.",
                      ws_changed, clients_changed)
            self.rebuild_grid(clients=new_clients)
        return False  # one-shot

    # -------------------------------------------------------------------------
    # Keyboard & Close
    # -------------------------------------------------------------------------

    def _on_key_press(self, widget: Gtk.Widget, event: Gdk.EventKey) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False

    def _on_delete(self, widget: Gtk.Widget, event: Gdk.Event) -> bool:
        """WM close request (Alt+F4) -> close graceful."""
        if not self._closed:
            self.close()
        return True  # we handle destroy ourselves

    def _on_destroy(self, widget: Gtk.Widget) -> None:
        """Cleanup tất cả timers khi destroy."""
        self._closed = True
        if self._rebuild_handler is not None:
            GLib.source_remove(self._rebuild_handler)
            self._rebuild_handler = None
        if self._refresh_handler is not None:
            GLib.source_remove(self._refresh_handler)
            self._refresh_handler = None
        if self._pressed_timeout_handler is not None:
            GLib.source_remove(self._pressed_timeout_handler)
            self._pressed_timeout_handler = None
        self.drag_state.cleanup()
        Gtk.main_quit()

    def close(self) -> None:
        """Đóng GUI an toàn (idempotent)."""
        if self._closed:
            return
        self._closed = True
        self.destroy()


# =============================================================================
# Main
# =============================================================================

def main() -> None:
    win = GridOverview()
    win.show_all()
    win.present()  # đảm bảo keyboard focus cho ESC
    Gtk.main()


if __name__ == "__main__":
    main()
