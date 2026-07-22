#!/usr/bin/env python3
#
# Screenshot GUI for MangoWM
# Requirements: grim, slurp, wl-clipboard, python-gobject, gtk3
# Description: A popup GUI to take screenshots (full, area, monitor)
#              and save them. It also displays a thumbnail preview
#              of the image currently in the clipboard.
#

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, Gdk, GdkPixbuf
import subprocess

try:
    gi.require_version('GtkLayerShell', '1.0')
    from gi.repository import GtkLayerShell
    HAS_LAYER_SHELL = True
except (ValueError, ImportError):
    HAS_LAYER_SHELL = False

CSS = """
window { background-color: rgba(25, 23, 36, 0.85); }
.box { padding: 25px; }
.grid { border-radius: 16px; }
.btn {
    background-color: rgba(38, 35, 58, 0.6);
    border: 2px solid #6e6a86;
    border-radius: 12px;
    color: #ebbcba;
    padding: 15px 20px;
    font-size: 14px;
    font-weight: bold;
    transition: all 150ms ease;
}
.btn:hover {
    background-color: rgba(235, 188, 186, 0.2);
    border-color: #ebbcba;
    color: #e0def4;
}
.hint { color: #908caa; font-size: 12px; margin-top: 15px; }
.preview-box {
    border: 2px solid #6e6a86;
    border-radius: 12px;
    padding: 5px;
    background-color: rgba(38, 35, 58, 0.6);
    margin-bottom: 15px;
}
.preview-empty { color: #908caa; font-style: italic; padding: 20px; }
"""

def get_clipboard_thumbnail():
    """Lấy ảnh từ wl-clipboard và tạo thumbnail"""
    try:
        res = subprocess.run(["wl-paste", "-t", "image/png"], capture_output=True)
        if res.returncode == 0 and res.stdout:
            loader = GdkPixbuf.PixbufLoader()
            loader.write(res.stdout)
            loader.close()
            pixbuf = loader.get_pixbuf()

            # Thu nhỏ ảnh giữ nguyên tỉ lệ (max width 300px)
            width = 300
            height = int(pixbuf.get_height() * (width / pixbuf.get_width())) if pixbuf.get_width() > 0 else 200
            thumb = pixbuf.scale_simple(width, height, GdkPixbuf.InterpType.BILINEAR)
            return thumb
    except Exception:
        pass
    return None

class ScreenshotGUI(Gtk.Window):
    def __init__(self):
        super().__init__(title="Screenshot")
        self.set_decorated(False)

        if HAS_LAYER_SHELL:
            GtkLayerShell.init_for_window(self)
            GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
            for edge in [GtkLayerShell.Edge.TOP, GtkLayerShell.Edge.BOTTOM, GtkLayerShell.Edge.LEFT, GtkLayerShell.Edge.RIGHT]:
                GtkLayerShell.set_anchor(self, edge, True)
        else:
            self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
            self.set_keep_above(True)
            self.set_default_size(400, 300)
            self.set_position(Gtk.WindowPosition.CENTER)

        self.connect("destroy", Gtk.main_quit)
        self.connect("key-press-event", lambda w, e: Gtk.main_quit() if e.keyval == Gdk.KEY_Escape else None)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode('utf-8'))
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        main_box.get_style_context().add_class("box")
        main_box.set_halign(Gtk.Align.CENTER)
        main_box.set_valign(Gtk.Align.CENTER)
        self.add(main_box)

        # --- Khung xem ảnh Clipboard ---
        preview_box = Gtk.Box()
        preview_box.get_style_context().add_class("preview-box")
        preview_box.set_halign(Gtk.Align.CENTER)
        main_box.pack_start(preview_box, False, False, 0)

        thumb = get_clipboard_thumbnail()
        if thumb:
            self.preview_img = Gtk.Image.new_from_pixbuf(thumb)
            preview_box.pack_start(self.preview_img, False, False, 0)
        else:
            empty_label = Gtk.Label(label="Clipboard is empty or does not contain an image.")
            empty_label.get_style_context().add_class("preview-empty")
            preview_box.pack_start(empty_label, False, False, 0)

        # --- Các nút bấm ---
        grid = Gtk.Grid()
        grid.set_column_spacing(10)
        grid.set_row_spacing(10)
        grid.set_halign(Gtk.Align.CENTER)
        main_box.pack_start(grid, False, False, 0)

        actions = [
            ("󰹑 Full Screen", "grim - | wl-copy", 0, 0),
            ("󰒍 Select Area", "slurp | grim -g - - | wl-copy", 1, 0),
            ("󰒖 Current Monitor", "slurp -o | grim -g - - | wl-copy", 0, 1),
            (" Save Clipboard to File", "wl-paste -t image/png > ~/Pictures/screenshot_$(date +%s).png", 1, 1),
            ("󰕧 Record Video", "python3 ~/.config/mango/scripts/record.py", 0, 2)
        ]

        for label, cmd, col, row in actions:
            btn = Gtk.Button(label=label)
            btn.get_style_context().add_class("btn")
            btn.connect("clicked", self.on_btn_clicked, cmd)
            grid.attach(btn, col, row, 1, 1)

        hint = Gtk.Label(label="ESC to close • Screenshots go to clipboard")
        hint.get_style_context().add_class("hint")
        main_box.pack_start(hint, False, False, 0)

    def on_btn_clicked(self, widget, cmd):
        import time

        # Nếu là nút Save from Clipboard, không cần ẩn popup
        if "Save Clipboard" in cmd:
            subprocess.Popen(cmd, shell=True)
            Gtk.main_quit()
            return

        # Ẩn popup đi để không bị chụp vào ảnh
        self.hide()
        # Ép GTK vẽ lại UI ngay lập tức để cửa sổ biến mất
        while Gtk.events_pending():
            Gtk.main_iteration()

        # Đợi 300ms cho MangoWM kịp xóa hết popup khỏi màn hình
        time.sleep(0.3)

        # Thực hiện chụp màn hình
        subprocess.Popen(cmd, shell=True)

        # Đóng popup hoàn toàn
        Gtk.main_quit()

win = ScreenshotGUI()
win.show_all()
Gtk.main()
