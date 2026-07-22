#!/usr/bin/env python3
# 
# Grid Overview Workspace cho MangoWM
# Yêu cầu: python-gobject, gtk3
# Khuyến nghị (để chống tiling): gtk-layer-shell (Arch: sudo pacman -S gtk-layer-shell)
#

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib
import subprocess
import json

# Kiểm tra xem có gtk-layer-shell không, nếu không thì dùng cửa sổ thường
try:
    gi.require_version('GtkLayerShell', '1.0')
    from gi.repository import GtkLayerShell
    HAS_LAYER_SHELL = True
except (ValueError, ImportError):
    HAS_LAYER_SHELL = False

# --- ROSE PINE THEME & CSS ---
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
    min-width: 180px;
    min-height: 120px;
    padding: 15px;
    color: #ebbcba;
    font-size: 16px;
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
.app-box {
    background-color: rgba(110, 106, 134, 0.3);
    border-radius: 8px;
    padding: 6px;
    margin: 3px;
    color: #e0def4;
    font-size: 12px;
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

# --- API HELPERS ---
def run_mmsg(args):
    try:
        res = subprocess.run(["mmsg"] + args, capture_output=True, text=True)
        if res.returncode == 0 and res.stdout.strip():
            return json.loads(res.stdout)
    except Exception:
        pass
    return None

def get_current_ws():
    data = run_mmsg(["get", "focusing-client"])
    if data and "tags" in data:
        tags = data["tags"]
        if isinstance(tags, list) and len(tags) > 0:
            return tags[0]
        elif isinstance(tags, int):
            for i in range(1, 11):
                if tags & (1 << (i-1)):
                    return i
    return 1

def get_clients():
    data = run_mmsg(["get", "all-clients"]) or {}
    apps = []
    for c in data.get("clients", []):
        app_id = str(c.get("id"))
        title = c.get("title") or c.get("appid") or "Unknown"
        tags = c.get("tags", [])
        
        if isinstance(tags, int):
            tags = [i for i in range(1, 11) if tags & (1 << (i-1))]
        elif not isinstance(tags, list):
            continue
            
        for ws in tags:
            apps.append({"id": app_id, "title": title[:14], "ws": ws})
    return apps

# --- MAIN GRID WINDOW ---
class GridOverview(Gtk.Window):
    def __init__(self):
        super().__init__(title="Grid Overview")
        self.set_decorated(False)
        
        # Cơ chế Fallback: Nếu có Layer Shell thì dùng, không thì dùng DIALOG
        if HAS_LAYER_SHELL:
            GtkLayerShell.init_for_window(self)
            GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
            GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.TOP, True)
            GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.BOTTOM, True)
            GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.LEFT, True)
            GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.RIGHT, True)
        else:
            # Fallback cho máy thiếu gtk-layer-shell
            self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
            self.set_keep_above(True)
            self.set_default_size(900, 500)
            self.set_position(Gtk.WindowPosition.CENTER)
        
        self.connect("destroy", Gtk.main_quit)
        self.connect("key-press-event", self.on_key_press)
        
        self.is_dragging = False
        self.just_dropped = False
        self.current_ws = get_current_ws()

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode('utf-8'))
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        self.outer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.outer_box.get_style_context().add_class("outer-box")
        self.outer_box.set_halign(Gtk.Align.CENTER)
        self.outer_box.set_valign(Gtk.Align.CENTER)
        self.add(self.outer_box)

        self.grid = Gtk.Grid()
        self.grid.set_column_spacing(20)
        self.grid.set_row_spacing(20)
        self.grid.set_halign(Gtk.Align.CENTER)
        self.outer_box.pack_start(self.grid, False, False, 0)

        self.hint_label = Gtk.Label(label="Drag apps to move • Click workspace to switch • ESC to close")
        self.hint_label.get_style_context().add_class("hint-label")
        self.outer_box.pack_start(self.hint_label, False, False, 0)

        self.rebuild_grid()

    def rebuild_grid(self):
        for child in self.grid.get_children():
            self.grid.remove(child)
            child.destroy()

        windows = get_clients()

        for i in range(1, 11):
            ws_eventbox = Gtk.EventBox()
            ws_eventbox.set_visible_window(False)
            ws_eventbox.connect("button-release-event", self.on_ws_clicked, i)
            
            ws_eventbox.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.MOVE)
            ws_eventbox.drag_dest_add_text_targets()
            ws_eventbox.connect("drag-data-received", self.on_drop, i)
            ws_eventbox.connect("drag-motion", self.on_drag_motion)
            ws_eventbox.connect("drag-leave", self.on_drag_leave)

            ws_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            ws_box.get_style_context().add_class("ws-btn")
            # Highlight workspace hiện tại
            if i == self.current_ws:
                ws_box.get_style_context().add_class("ws-active")
            ws_eventbox.add(ws_box)

            label = Gtk.Label(label=f"Workspace {i}")
            label.set_halign(Gtk.Align.START)
            ws_box.pack_start(label, False, False, 0)
            
            app_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            ws_box.pack_start(app_container, True, True, 0)

            for win in windows:
                if win['ws'] == i:
                    app_eventbox = Gtk.EventBox()
                    app_eventbox.set_visible_window(False)
                    
                    app_inner_box = Gtk.Box()
                    app_inner_box.get_style_context().add_class("app-box")
                    app_inner_box.set_halign(Gtk.Align.CENTER)
                    
                    app_label = Gtk.Label(label=win['title'])
                    app_label.set_max_width_chars(12)
                    app_label.set_line_wrap_mode(2)
                    app_inner_box.pack_start(app_label, True, True, 0)
                    app_eventbox.add(app_inner_box)

                    app_eventbox.drag_source_set(Gdk.ModifierType.BUTTON1_MASK, [], Gdk.DragAction.MOVE)
                    app_eventbox.drag_source_add_text_targets()
                    app_eventbox.connect("drag-data-get", self.on_drag_data_get, str(win['id']))
                    app_eventbox.connect("drag-begin", self.on_drag_begin, app_inner_box)
                    app_eventbox.connect("drag-end", self.on_drag_end, app_inner_box)

                    app_container.pack_start(app_eventbox, False, False, 0)

            col = (i - 1) % 5
            row = (i - 1) // 5
            self.grid.attach(ws_eventbox, col, row, 1, 1)

        self.show_all()

    def on_ws_clicked(self, widget, event, ws_id):
        if self.is_dragging or self.just_dropped:
            return True
        
        if event.button == 1:
            subprocess.Popen(["mmsg", "dispatch", f"view,{ws_id},0"])
            Gtk.main_quit()

    def on_drag_data_get(self, widget, drag_context, data, info, time, app_id):
        data.set_text(app_id, -1)

    def on_drag_begin(self, widget, context, app_box):
        self.is_dragging = True
        app_box.get_style_context().add_class("app-box-dragging")
        Gtk.drag_set_icon_name(context, "window-new", 0, 0)

    def on_drag_end(self, widget, context, app_box):
        self.is_dragging = False
        try:
            app_box.get_style_context().remove_class("app-box-dragging")
        except:
            pass

    def on_drag_motion(self, widget, context, x, y, time):
        ws_box = widget.get_child()
        if ws_box:
            ws_box.get_style_context().add_class("ws-drop")
        return True

    def on_drag_leave(self, widget, context, time):
        ws_box = widget.get_child()
        if ws_box:
            ws_box.get_style_context().remove_class("ws-drop")

    def on_drop(self, widget, drag_context, x, y, data, info, time, ws_id):
        app_id = data.get_text()
        if app_id:
            subprocess.Popen(["mmsg", "dispatch", f"tag,{ws_id},0", f"client,{app_id}"])
            subprocess.Popen(["mmsg", "dispatch", f"view,{self.current_ws},0"])
            
            self.just_dropped = True
            GLib.timeout_add(100, self.rebuild_grid)
            GLib.timeout_add(300, self.reset_drop_flag)

    def reset_drop_flag(self):
        self.just_dropped = False
        return False

    def on_key_press(self, widget, event):
        if event.keyval == Gdk.KEY_Escape:
            Gtk.main_quit()

if __name__ == "__main__":
    win = GridOverview()
    win.show_all()
    Gtk.main()
