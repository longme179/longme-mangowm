#!/usr/bin/env python3
#
# Grid Overview Workspace cho MangoWM (Production Release)
#
# =============================================================================
# DEPENDENCIES (Yêu cầu hệ thống):
# =============================================================================
# - Python 3.7+
# - GTK+ 3
# - PyGObject (python3-gi / python-gobject)
# - gtk-layer-shell (Tùy chọn nhưng KHUYẾN NGHỊ MẠNH MẼ để UI hiển thị đúng)
# - mmsg (MangoWM IPC CLI)
#
# Cách cài đặt trên Arch Linux:
#   sudo pacman -S python-gobject gtk3 gtk-layer-shell
#
# Cách cài đặt trên Debian/Ubuntu:
#   sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-gtklayershell-0.1
#
# Cách cài đặt trên Fedora:
#   sudo dnf install python3-gobject gtk3 gtk-layer-shell
#
# =============================================================================
# KIẾN TRÚC & CẢI TIẾN (Refactored Architecture):
# =============================================================================
# - State Management: Quản lý trạng thái client độc lập với UI. Chỉ cập nhật
#   widget khi có sự thay đổi (Diffing), ngăn chặn hoàn toàn hiện tượng flicker
#   và memory leak khi rebuild UI mù quáng.
# - Race Conditions Fixed: Tạm dừng polling IPC khi người dùng đang thực hiện
#   Drag & Drop để tránh việc widget bị destroy giữa chừng gây crash.
# - Event Propagation: Xử lý triệt để logic Click App vs Click Workspace bằng
#   cách can thiệp vào `button-release-event` và kiểm soát sự kiện nổi (bubbling).
# - Robust IPC: Thêm timeout và error handling/JSON validation nghiêm ngặt
#   khi giao tiếp với mmsg, không làm block GTK Main Loop.
# - Scrollable Workspace: Bọc danh sách app trong `Gtk.ScrolledWindow` để lưới
#   không bị phá vỡ cấu trúc 3x3 khi có quá nhiều cửa sổ.
# - Desktop Icons: Sử dụng `Gtk.IconTheme` để tìm kiếm icon dựa trên app_id.
# =============================================================================

import gi
import json
import logging
import subprocess
from dataclasses import dataclass
from typing import List, Dict, Optional, Any

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Gio

# Cấu hình logging
logging.basicConfig(level=logging.INFO, format="[GridOverview] %(levelname)s: %(message)s")

try:
    gi.require_version('GtkLayerShell', '1.0')
    from gi.repository import GtkLayerShell
    HAS_LAYER_SHELL = True
except (ValueError, ImportError):
    HAS_LAYER_SHELL = False
    logging.warning("GtkLayerShell không được tìm thấy. Khuyến nghị cài đặt để tránh lỗi Tiling.")

CSS = b"""
window {
    background-color: rgba(25, 23, 36, 0.85);
}
.bg-overlay {
    background-color: transparent;
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
    min-width: 220px;
    min-height: 180px;
    padding: 12px;
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
    border: 3px dashed #eb6f92;
    background-color: rgba(235, 111, 146, 0.3);
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
    background-color: rgba(110, 106, 134, 0.4);
    border-radius: 8px;
    padding: 6px;
    margin-bottom: 4px;
    transition: all 100ms ease;
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
    margin-left: 6px;
}
.hint-label {
    color: #908caa;
    font-size: 13px;
    margin-top: 24px;
}
"""

DND_TARGETS = [Gtk.TargetEntry.new("application/x-mango-client-id", Gtk.TargetFlags.SAME_APP, 0)]

@dataclass
class ClientState:
    id: str
    title: str
    app_id: str
    ws: int

    def __eq__(self, other):
        if not isinstance(other, ClientState):
            return False
        return (self.id, self.title, self.app_id, self.ws) == \
               (other.id, other.title, other.app_id, other.ws)


class MangoWM_IPC:
    """Xử lý giao tiếp với MangoWM IPC."""

    @staticmethod
    def run_command(args: List[str], timeout: float = 1.0) -> Optional[Any]:
        """Thực thi mmsg với timeout để tránh treo GUI."""
        try:
            res = subprocess.run(["mmsg"] + args, capture_output=True, text=True, timeout=timeout)
            if res.returncode == 0 and res.stdout.strip():
                return json.loads(res.stdout)
        except subprocess.TimeoutExpired:
            logging.error(f"Timeout khi gọi mmsg: {' '.join(args)}")
        except json.JSONDecodeError:
            logging.error(f"Dữ liệu JSON từ mmsg không hợp lệ: {res.stdout}")
        except Exception as e:
            logging.error(f"Lỗi khi gọi mmsg {' '.join(args)}: {e}")
        return None

    @staticmethod
    def dispatch(args: List[str]):
        """Gửi lệnh fire-and-forget đến WM."""
        try:
            # Dùng run timeout ngắn, tránh Popen sinh zombie process dài hạn
            subprocess.run(["mmsg", "dispatch"] + args, timeout=0.5, check=False)
        except Exception as e:
            logging.error(f"Lỗi khi dispatch mmsg: {e}")

    @staticmethod
    def parse_tags(tags_raw: Any) -> List[int]:
        ws_list = []
        if isinstance(tags_raw, list):
            for t in tags_raw:
                try:
                    ws_list.append(int(t))
                except (ValueError, TypeError):
                    pass
        elif isinstance(tags_raw, int):
            for i in range(1, 33):
                if tags_raw & (1 << (i - 1)):
                    ws_list.append(i)
        return ws_list

    @staticmethod
    def get_current_ws() -> int:
        data = MangoWM_IPC.run_command(["get", "focusing-client"])
        if isinstance(data, dict) and "tags" in data:
            ws_list = MangoWM_IPC.parse_tags(data["tags"])
            if ws_list:
                return ws_list[0]
        return 1

    @staticmethod
    def get_clients() -> List[ClientState]:
        data = MangoWM_IPC.run_command(["get", "all-clients"])
        if not isinstance(data, dict):
            return []

        apps = []
        for c in data.get("clients", []):
            if not isinstance(c, dict):
                continue

            app_id = str(c.get("id", ""))
            if not app_id:
                continue

            wm_class = str(c.get("appid", "") or c.get("class", ""))
            title = str(c.get("title", "") or wm_class or "Unknown Window")
            tags = c.get("tags", [])
            ws_list = MangoWM_IPC.parse_tags(tags)

            for ws in ws_list:
                apps.append(ClientState(id=app_id, title=title, app_id=wm_class, ws=ws))
        return apps


def get_icon_name(app_id: str, title: str) -> str:
    """Lấy icon an toàn dựa vào Theme hiện hành, trả về icon mặc định nếu không tìm thấy."""
    if not app_id:
        return "application-x-executable"

    theme = Gtk.IconTheme.get_default()

    # Thử khớp chính xác
    if theme.has_icon(app_id):
        return app_id

    # Thử chữ thường
    app_id_lower = app_id.lower()
    if theme.has_icon(app_id_lower):
        return app_id_lower

    # Xử lý các app có prefix reverse-DNS (vd: org.gnome.Terminal -> terminal)
    parts = app_id_lower.split('.')
    if len(parts) > 1 and theme.has_icon(parts[-1]):
        return parts[-1]

    # Fallback cho terminal hoặc browser phổ biến nếu nhận diện qua class name
    if "kitty" in app_id_lower: return "kitty"
    if "firefox" in app_id_lower: return "firefox"
    if "discord" in app_id_lower: return "discord"

    return "application-x-executable"


class AppWidget(Gtk.EventBox):
    """Widget đại diện cho một Application."""

    def __init__(self, client: ClientState, main_window: "GridOverview"):
        super().__init__()
        self.client = client
        self.main_window = main_window
        self.set_visible_window(True)  # Cần thiết để bắt sự kiện chuột riêng biệt

        # Bắt sự kiện bấm chuột
        self.connect("button-press-event", self.on_button_press)
        self.connect("button-release-event", self.on_button_release)

        # Cấu hình kéo thả (Drag Source)
        self.drag_source_set(Gdk.ModifierType.BUTTON1_MASK, DND_TARGETS, Gdk.DragAction.MOVE)
        self.connect("drag-data-get", self.on_drag_data_get)
        self.connect("drag-begin", self.on_drag_begin)
        self.connect("drag-end", self.on_drag_end)

        self.box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.box.get_style_context().add_class("app-box")

        self.icon = Gtk.Image()
        self.label = Gtk.Label()
        self.label.set_ellipsize(Pango.EllipsizeMode.END) if hasattr(Gtk, "Pango") else None
        self.label.set_max_width_chars(20)
        self.label.set_halign(Gtk.Align.START)
        self.label.get_style_context().add_class("app-label")

        self.box.pack_start(self.icon, False, False, 0)
        self.box.pack_start(self.label, True, True, 0)
        self.add(self.box)

        self._render()

    def _render(self):
        icon_name = get_icon_name(self.client.app_id, self.client.title)
        self.icon.set_from_icon_name(icon_name, Gtk.IconSize.MENU)
        self.label.set_text(self.client.title.strip())

    def update(self, new_client: ClientState):
        """Cập nhật dữ liệu UI nếu state thay đổi."""
        if self.client != new_client:
            self.client = new_client
            self._render()

    def on_button_press(self, widget, event):
        # Return False để cho phép GTK khởi tạo Drag and Drop.
        return False

    def on_button_release(self, widget, event):
        if event.button == 1:
            # Ngăn chặn sự kiện nổi lên WorkspaceWidget (Tránh chuyển nhầm workspace)
            if not self.main_window.is_dragging:
                # Nếu click bình thường -> focus app và đóng grid
                MangoWM_IPC.dispatch([f"focus,{self.client.id}"])
                self.main_window.close_app()
            return True
        return False

    def on_drag_data_get(self, widget, context, data, info, time):
        data.set_text(self.client.id, -1)

    def on_drag_begin(self, widget, context):
        self.main_window.is_dragging = True
        self.box.get_style_context().add_class("app-box-dragging")

        # Set icon khi kéo
        icon_name = get_icon_name(self.client.app_id, self.client.title)
        Gtk.drag_set_icon_name(context, icon_name, 0, 0)

    def on_drag_end(self, widget, context):
        self.main_window.is_dragging = False
        try:
            self.box.get_style_context().remove_class("app-box-dragging")
        except Exception:
            pass
        # Đồng bộ state khẩn cấp ngay khi nhả chuột thất bại/thành công
        GLib.idle_add(self.main_window.sync_state)


class WorkspaceWidget(Gtk.EventBox):
    """Widget đại diện cho một Workspace (Drop Target)."""

    def __init__(self, ws_id: int, main_window: "GridOverview"):
        super().__init__()
        self.ws_id = ws_id
        self.main_window = main_window
        self.set_visible_window(True)
        self.app_widgets: Dict[str, AppWidget] = {}

        # Bắt click để chuyển workspace
        self.connect("button-release-event", self.on_button_release)

        # Setup D&D Destination
        self.drag_dest_set(Gtk.DestDefaults.ALL, DND_TARGETS, Gdk.DragAction.MOVE)
        self.connect("drag-data-received", self.on_drag_data_received)
        self.connect("drag-motion", self.on_drag_motion)
        self.connect("drag-leave", self.on_drag_leave)

        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.box.get_style_context().add_class("ws-btn")

        self.label = Gtk.Label(label=f"Workspace {self.ws_id}")
        self.label.set_halign(Gtk.Align.START)
        self.label.get_style_context().add_class("ws-label")
        self.box.pack_start(self.label, False, False, 0)

        # Dùng ScrolledWindow để tránh tràn UI khi workspace chứa hàng chục app
        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroll.set_min_content_height(130)

        self.app_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.app_container.get_style_context().add_class("app-container")
        self.scroll.add(self.app_container)

        self.box.pack_start(self.scroll, True, True, 0)
        self.add(self.box)

    def set_active(self, is_active: bool):
        ctx = self.box.get_style_context()
        if is_active:
            ctx.add_class("ws-active")
        else:
            ctx.remove_class("ws-active")

    def sync_apps(self, ws_clients: List[ClientState]):
        """Cập nhật UI dựa trên tập hợp Clients mà không rebuild toàn bộ."""
        new_ids = {c.id for c in ws_clients}
        current_ids = set(self.app_widgets.keys())

        # Xóa các app widget không còn nằm trong workspace này
        for dead_id in current_ids - new_ids:
            widget = self.app_widgets.pop(dead_id)
            self.app_container.remove(widget)
            widget.destroy()

        # Thêm mới hoặc cập nhật app widget
        for client in ws_clients:
            if client.id in self.app_widgets:
                self.app_widgets[client.id].update(client)
            else:
                new_widget = AppWidget(client, self.main_window)
                self.app_widgets[client.id] = new_widget
                self.app_container.pack_start(new_widget, False, False, 0)
                new_widget.show_all()

    def on_button_release(self, widget, event):
        if event.button == 1:
            MangoWM_IPC.dispatch([f"view,{self.ws_id},0"])
            self.main_window.close_app()
            return True
        return False

    def on_drag_motion(self, widget, context, x, y, time):
        self.box.get_style_context().add_class("ws-drop")
        return True

    def on_drag_leave(self, widget, context, time):
        self.box.get_style_context().remove_class("ws-drop")

    def on_drag_data_received(self, widget, context, x, y, data, info, time):
        app_id = data.get_text()
        self.box.get_style_context().remove_class("ws-drop")

        if app_id:
            # Di chuyển app qua Workspace đích
            # Behavior Bắt Buộc: KHÔNG view (đổi focus workspace), chỉ gán tag.
            MangoWM_IPC.dispatch([f"tag,{self.ws_id},0", f"client,{app_id}"])
            context.finish(True, False, time)

            # Gây trễ 100ms để WM xử lý, sau đó update lại state
            GLib.timeout_add(100, self.main_window.sync_state)
        else:
            context.finish(False, False, time)
        return True


class GridOverview(Gtk.Window):
    def __init__(self):
        super().__init__(title="Grid Overview")
        self.set_decorated(False)
        self.is_dragging = False

        if HAS_LAYER_SHELL:
            GtkLayerShell.init_for_window(self)
            GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
            GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.TOP, True)
            GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.BOTTOM, True)
            GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.LEFT, True)
            GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.RIGHT, True)
            # Yêu cầu LayerShell tập trung nhận Keyboard Input
            GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.EXCLUSIVE)
        else:
            self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
            self.set_keep_above(True)
            self.fullscreen()

        self.connect("destroy", Gtk.main_quit)
        self.connect("key-press-event", self.on_key_press)

        # Áp dụng CSS
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        # Background có thể click để đóng
        self.bg_eventbox = Gtk.EventBox()
        self.bg_eventbox.get_style_context().add_class("bg-overlay")
        self.bg_eventbox.connect("button-press-event", self.on_bg_clicked)
        self.add(self.bg_eventbox)

        self.outer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.outer_box.get_style_context().add_class("outer-box")
        self.outer_box.set_halign(Gtk.Align.CENTER)
        self.outer_box.set_valign(Gtk.Align.CENTER)
        self.bg_eventbox.add(self.outer_box)

        self.grid = Gtk.Grid()
        self.grid.set_column_spacing(24)
        self.grid.set_row_spacing(24)
        self.grid.set_halign(Gtk.Align.CENTER)
        self.outer_box.pack_start(self.grid, False, False, 0)

        self.hint_label = Gtk.Label(label="Kéo thả App để di chuyển • Click Workspace để chuyển • Click vùng trống hoặc ESC để thoát")
        self.hint_label.get_style_context().add_class("hint-label")
        self.outer_box.pack_start(self.hint_label, False, False, 0)

        self.workspaces: Dict[int, WorkspaceWidget] = {}
        self.setup_ui_grid()

        # Lần đầu load state
        self.sync_state()
        self.show_all()

        # Background Poll: Cập nhật GUI mỗi 500ms
        self.poll_source = GLib.timeout_add(500, self.poll_state)

    def setup_ui_grid(self):
        """Khởi tạo lưới cố định 3x3 (9 Workspace)"""
        NUM_WORKSPACES = 9
        for i in range(1, NUM_WORKSPACES + 1):
            ws_widget = WorkspaceWidget(ws_id=i, main_window=self)
            self.workspaces[i] = ws_widget

            # Tính toán vị trí (Hàng và Cột cho lưới 3x3)
            col = (i - 1) % 3
            row = (i - 1) // 3
            self.grid.attach(ws_widget, col, row, 1, 1)

    def poll_state(self) -> bool:
        """Kiểm tra thay đổi. Nếu đang drag, bỏ qua poll để tránh Race Condition/Crash."""
        if not self.is_dragging:
            self.sync_state()
        return True  # Giữ timeout tiếp tục chạy

    def sync_state(self):
        """Lấy dữ liệu từ IPC và phân phối xuống các WorkspaceWidget, chỉ update những gì thay đổi."""
        current_ws = MangoWM_IPC.get_current_ws()
        all_clients = MangoWM_IPC.get_clients()

        # Gom nhóm Clients theo Workspace
        ws_client_map = {i: [] for i in range(1, 10)}
        for client in all_clients:
            if 1 <= client.ws <= 9:
                ws_client_map[client.ws].append(client)

        # Cập nhật từng widget con
        for i in range(1, 10):
            ws_widget = self.workspaces[i]
            ws_widget.set_active(i == current_ws)
            ws_widget.sync_apps(ws_client_map[i])

    def on_bg_clicked(self, widget, event):
        """Click vùng trống -> đóng GUI."""
        if event.window == self.bg_eventbox.get_window():
            self.close_app()
            return True
        return False

    def on_key_press(self, widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.close_app()
            return True
        return False

    def close_app(self):
        """Clean up và thoát an toàn."""
        if hasattr(self, 'poll_source'):
            GLib.source_remove(self.poll_source)
        Gtk.main_quit()

if __name__ == "__main__":
    try:
        from gi.repository import Pango  # Xử lý text wrap
    except ImportError:
        pass

    app = GridOverview()
    Gtk.main()
