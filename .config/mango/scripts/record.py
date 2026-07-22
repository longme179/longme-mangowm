#!/usr/bin/env python3
#
# Screen Recorder GUI for MangoWM
# Requirements: wf-recorder, notify-send, python-gobject, gtk3
# Description: A popup GUI to record the native Wayland screen.
#              Hides itself while recording. Stops and saves when
#              clicking Stop or closing the popup.
#

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk
import subprocess
import signal
import os
import time

try:
    gi.require_version('GtkLayerShell', '1.0')
    from gi.repository import GtkLayerShell
    HAS_LAYER_SHELL = True
except (ValueError, ImportError):
    HAS_LAYER_SHELL = False

CSS = """
window { background-color: rgba(25, 23, 36, 0.85); }
.box { padding: 30px; }
.btn {
    background-color: rgba(38, 35, 58, 0.6);
    border: 2px solid #6e6a86;
    border-radius: 12px;
    color: #ebbcba;
    padding: 20px 40px;
    font-size: 16px;
    font-weight: bold;
    transition: all 150ms ease;
}
.btn:hover {
    background-color: rgba(235, 188, 186, 0.2);
    border-color: #ebbcba;
    color: #e0def4;
}
.btn-stop {
    background-color: rgba(235, 111, 146, 0.3);
    border-color: #eb6f92;
    color: #eb6f92;
}
.btn-stop:hover {
    background-color: rgba(235, 111, 146, 0.5);
    color: #e0def4;
}
.hint { color: #908caa; font-size: 12px; margin-top: 15px; }
"""

class RecorderGUI(Gtk.Window):
    def __init__(self):
        super().__init__(title="Recorder")
        self.set_decorated(False)

        if HAS_LAYER_SHELL:
            GtkLayerShell.init_for_window(self)
            GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
            for edge in [GtkLayerShell.Edge.TOP, GtkLayerShell.Edge.BOTTOM, GtkLayerShell.Edge.LEFT, GtkLayerShell.Edge.RIGHT]:
                GtkLayerShell.set_anchor(self, edge, True)
        else:
            self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
            self.set_keep_above(True)
            self.set_default_size(300, 150)
            self.set_position(Gtk.WindowPosition.CENTER)

        self.connect("destroy", self.on_stop_clicked)
        self.connect("key-press-event", lambda w, e: self.on_stop_clicked() if e.keyval == Gdk.KEY_Escape else None)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode('utf-8'))
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=15)
        main_box.get_style_context().add_class("box")
        main_box.set_halign(Gtk.Align.CENTER)
        main_box.set_valign(Gtk.Align.CENTER)
        self.add(main_box)

        self.btn = Gtk.Button(label="󰕧 Start Recording")
        self.btn.get_style_context().add_class("btn")
        self.btn.connect("clicked", self.on_toggle_record)
        main_box.pack_start(self.btn, False, False, 0)

        self.hint = Gtk.Label(label="Press ESC or close window to stop & save")
        self.hint.get_style_context().add_class("hint")
        main_box.pack_start(self.hint, False, False, 0)

        self.is_recording = False
        self.proc = None
        self.filename = ""

    def on_toggle_record(self, widget):
        if not self.is_recording:
            self.start_recording()
        else:
            self.on_stop_clicked()

    def start_recording(self):
        # Tạo thư mục Videos nếu chưa có
        videos_dir = os.path.expanduser("~/Videos")
        os.makedirs(videos_dir, exist_ok=True)

        self.filename = f"{videos_dir}/recording_{int(time.time())}.mp4"

        # Lệnh quay màn hình (wf-recorder tự tối ưu GPU để file nhỏ và nét)
        cmd = ["wf-recorder", "-f", self.filename]
        self.proc = subprocess.Popen(cmd)

        self.is_recording = True
        self.btn.set_label("󰛊 Stop & Save")
        self.btn.get_style_context().add_class("btn-stop")
        self.hint.set_label("Recording... Press ESC or close to stop")

        # Ẩn popup đi để không bị quay vào video
        self.hide()
        while Gtk.events_pending():
            Gtk.main_iteration()

        subprocess.Popen(["notify-send", "Screen Recorder", "Started recording..."])

    def on_stop_clicked(self, *args):
        if self.is_recording and self.proc:
            self.is_recording = False

            # Gửi SIGINT để wf-recorder lưu file lại an toàn
            self.proc.send_signal(signal.SIGINT)
            self.proc.wait()

            subprocess.Popen(["notify-send", "Screen Recorder", f"Video saved to {self.filename}", "--icon=video-x-generic"])
            Gtk.main_quit()

win = RecorderGUI()
win.show_all()
Gtk.main()
