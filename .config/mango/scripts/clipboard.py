#!/usr/bin/env python3
#
# Clipboard Manager GUI for MangoWM
# Requirements: cliphist, wl-clipboard, python-gobject, gtk3
# Description: A popup GUI to view, search, and select recently
#              copied text and images from the clipboard history.
#              Displays thumbnails for the most recent images instantly.
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
window { background-color: rgba(25, 23, 36, 0.95); }
.box { padding: 20px; }
.title { color: #ebbcba; font-size: 18px; font-weight: bold; margin-bottom: 10px; }
.scroll { border: 2px solid #6e6a86; border-radius: 12px; min-width: 450px; min-height: 350px; }
.list { background-color: transparent; }
.row { border-bottom: 1px solid rgba(110, 106, 134, 0.3); padding: 8px; }
.row:selected { background-color: rgba(235, 188, 186, 0.2); }
.row-label { color: #e0def4; }
"""

class ClipboardGUI(Gtk.Window):
    def __init__(self):
        super().__init__(title="Clipboard")
        self.set_decorated(False)

        if HAS_LAYER_SHELL:
            GtkLayerShell.init_for_window(self)
            GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
            for edge in [GtkLayerShell.Edge.TOP, GtkLayerShell.Edge.BOTTOM, GtkLayerShell.Edge.LEFT, GtkLayerShell.Edge.RIGHT]:
                GtkLayerShell.set_anchor(self, edge, True)
        else:
            self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
            self.set_keep_above(True)
            self.set_default_size(500, 450)
            self.set_position(Gtk.WindowPosition.CENTER)

        self.connect("destroy", Gtk.main_quit)
        self.connect("key-press-event", self.on_key_press)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode('utf-8'))
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        main_box.get_style_context().add_class("box")
        self.add(main_box)

        title = Gtk.Label(label="󰆏 Clipboard History")
        title.get_style_context().add_class("title")
        main_box.pack_start(title, False, False, 0)

        self.scroll = Gtk.ScrolledWindow()
        self.scroll.get_style_context().add_class("scroll")
        main_box.pack_start(self.scroll, True, True, 0)

        self.listbox = Gtk.ListBox()
        self.listbox.get_style_context().add_class("list")
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.connect("row-activated", self.on_row_activated)
        self.scroll.add(self.listbox)

        self.load_history()

    def load_history(self):
        try:
            res = subprocess.run(["cliphist", "list"], capture_output=True, text=True)
            if res.returncode == 0:
                lines = res.stdout.strip().split('\n')[:20]
                image_count = 0
                MAX_THUMBS = 3 # Chỉ load 3 ảnh gần nhất để tránh lag

                for line in lines:
                    if not line.strip():
                        continue

                    parts = line.split('\t', 1)
                    text = parts[1] if len(parts) > 1 else parts[0]

                    thumb = None
                    label_text = text[:40] + '...' if len(text) > 40 else text

                    # Nếu là ảnh
                    if "[[ binary data" in text:
                        label_text = "Image Snippet"
                        # Chỉ decode nếu chưa vượt quá giới hạn
                        if image_count < MAX_THUMBS:
                            try:
                                proc = subprocess.run(["cliphist", "decode"], input=line.encode('utf-8'), capture_output=True)
                                if proc.returncode == 0 and proc.stdout:
                                    loader = GdkPixbuf.PixbufLoader()
                                    loader.write(proc.stdout)
                                    loader.close()
                                    pixbuf = loader.get_pixbuf()

                                    if pixbuf:
                                        aspect = pixbuf.get_width() / pixbuf.get_height() if pixbuf.get_height() > 0 else 1
                                        w = 40
                                        h = int(w / aspect)
                                        thumb = pixbuf.scale_simple(w, h, GdkPixbuf.InterpType.BILINEAR)
                                        image_count += 1
                            except Exception:
                                pass

                    # Thêm dòng vào listbox
                    row = Gtk.ListBoxRow()
                    row.get_style_context().add_class("row")
                    row.raw_data = line

                    hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
                    row.add(hbox)

                    if thumb:
                        img = Gtk.Image.new_from_pixbuf(thumb)
                        hbox.pack_start(img, False, False, 0)

                    label = Gtk.Label(label=label_text, halign=Gtk.Align.START)
                    label.get_style_context().add_class("row-label")
                    hbox.pack_start(label, True, True, 0)

                    self.listbox.add(row)

        except Exception:
            pass

        self.show_all()

    def on_row_activated(self, listbox, row):
        raw = row.raw_data
        try:
            proc = subprocess.run(["cliphist", "decode"], input=raw.encode('utf-8'), capture_output=True)
            if proc.stdout:
                subprocess.Popen(["wl-copy", proc.stdout])
        except Exception:
            pass
        Gtk.main_quit()

    def on_key_press(self, widget, event):
        if event.keyval == Gdk.KEY_Escape:
            Gtk.main_quit()

win = ClipboardGUI()
win.show_all()
Gtk.main()
