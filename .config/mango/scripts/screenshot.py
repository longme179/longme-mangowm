#!/usr/bin/env python3
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk
import subprocess

# Cơ chế Fallback an toàn
try:
    gi.require_version('GtkLayerShell', '1.0')
    from gi.repository import GtkLayerShell
    HAS_LAYER_SHELL = True
except (ValueError, ImportError):
    HAS_LAYER_SHELL = False

CSS = """
window { background-color: rgba(25, 23, 36, 0.85); }
.box { padding: 30px; }
.grid { border-radius: 16px; }
.btn {
    background-color: rgba(38, 35, 58, 0.6);
    border: 2px solid #6e6a86;
    border-radius: 12px;
    color: #ebbcba;
    padding: 20px 30px;
    font-size: 16px;
    font-weight: bold;
    transition: all 150ms ease;
}
.btn:hover {
    background-color: rgba(235, 188, 186, 0.2);
    border-color: #ebbcba;
    color: #e0def4;
}
.hint { color: #908caa; font-size: 12px; margin-top: 15px; }
"""

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
            self.set_default_size(400, 200)
            self.set_position(Gtk.WindowPosition.CENTER)

        self.connect("destroy", Gtk.main_quit)
        self.connect("key-press-event", lambda w, e: Gtk.main_quit() if e.keyval == Gdk.KEY_Escape else None)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode('utf-8'))
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=15)
        main_box.get_style_context().add_class("box")
        self.add(main_box)

        grid = Gtk.Grid()
        grid.set_column_spacing(15)
        grid.set_row_spacing(15)
        grid.set_halign(Gtk.Align.CENTER)
        main_box.pack_start(grid, False, False, 0)

        actions = [
            ("󰹑 Full Screen", "grim - | wl-copy", 0, 0),
            ("󰒍 Select Area", "slurp | grim -g - - | wl-copy", 1, 0),
            ("󰒖 Current Monitor", "slurp -o | grim -g - - | wl-copy", 0, 1),
            (" Save to File", "grim ~/Pictures/screenshot_$(date +%s).png", 1, 1)
        ]

        for label, cmd, col, row in actions:
            btn = Gtk.Button(label=label)
            btn.get_style_context().add_class("btn")
            btn.connect("clicked", self.on_btn_clicked, cmd)
            grid.attach(btn, col, row, 1, 1)

        hint = Gtk.Label(label="ESC to close • Screenshots copied to clipboard")
        hint.get_style_context().add_class("hint")
        main_box.pack_start(hint, False, False, 0)

    def on_btn_clicked(self, widget, cmd):
        subprocess.Popen(cmd, shell=True)
        Gtk.main_quit()

win = ScreenshotGUI()
win.show_all()
Gtk.main()
