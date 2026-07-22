#!/usr/bin/env python3
#
# Screen Recorder GUI for MangoWM (gpu-screen-recorder)
# Requirements: gpu-screen-recorder, notify-send, python-gobject, gtk3
# Description: Records the native Wayland screen at 30fps using GPU encoding.
#              Automatically captures desktop audio.
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

def get_default_audio_sink():
    try:
        res = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True)
        if res.returncode == 0 and res.stdout.strip():
            return f"{res.stdout.strip()}.monitor"
    except Exception:
        pass
    return None

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

        self.btn_start = Gtk.Button(label="󰕧 Start Recording")
        self.btn_start.get_style_context().add_class("btn")
        self.btn_start.connect("clicked", self.on_start_clicked)
        main_box.pack_start(self.btn_start, False, False, 0)

        self.btn_stop = Gtk.Button(label="󰛊 Stop & Save")
        self.btn_stop.get_style_context().add_class("btn btn-stop")
        self.btn_stop.connect("clicked", self.on_stop_clicked)
        self.btn_stop.set_no_show_all(True)
        main_box.pack_start(self.btn_stop, False, False, 0)

        self.hint = Gtk.Label(label="Click Start to begin recording (30fps)")
        self.hint.get_style_context().add_class("hint")
        main_box.pack_start(self.hint, False, False, 0)

        self.is_recording = False
        self.proc = None
        self.filename = ""
        self.log_file = None

    def on_start_clicked(self, widget):
        if self.is_recording:
            return

        videos_dir = os.path.expanduser("~/Videos")
        os.makedirs(videos_dir, exist_ok=True)

        self.filename = f"{videos_dir}/recording_{int(time.time())}.mp4"
        audio_sink = get_default_audio_sink()

        cmd = [
            "gpu-screen-recorder",
            "-w", "screen",
            "-f", "30",
            "-k", "h265",
            "-q", "ultra",
            "-o", self.filename
        ]

        if audio_sink:
            cmd.extend(["-a", audio_sink])

        # Mở file log để bắt lỗi stderr của gpu-screen-recorder
        self.log_file = open("/tmp/gsr_error.log", "w")
        self.proc = subprocess.Popen(cmd, stderr=self.log_file)

        self.is_recording = True
        self.btn_start.hide()
        self.btn_stop.show()
        self.hint.set_label("Recording... Click Stop or ESC to save")

        # Đợi 1 giây và kiểm tra xem process có bị crash ngay không
        GLib_timeout = False
        try:
            from gi.repository import GLib
            GLib.timeout_add(1000, self.check_crash)
        except:
            pass

    def check_crash(self):
        # Nếu process đã tắt (crash) ngay sau 1 giây
        if self.proc and self.proc.poll() is not None:
            self.is_recording = False
            self.log_file.close()

            # Đọc lỗi
            err_msg = "Unknown error"
            try:
                with open("/tmp/gsr_error.log", "r") as f:
                    err_msg = f.read().strip()[:100] # Lấy 100 ký tự đầu
            except:
                pass

            subprocess.Popen(["notify-send", "Recorder Crashed!", err_msg, "--icon=dialog-error"])

            # Reset UI
            self.btn_start.show()
            self.btn_stop.hide()
            self.hint.set_label(f"Error: {err_msg}")
            self.btn_stop.set_sensitive(True)
            self.btn_stop.set_label("󰛊 Stop & Save")
        return False

    def on_stop_clicked(self, *args):
        if not self.is_recording:
            Gtk.main_quit()
            return

        self.is_recording = False
        self.btn_stop.set_label("Saving...")
        self.btn_stop.set_sensitive(False)
        self.hint.set_label("Saving file, please wait...")

        while Gtk.events_pending():
            Gtk.main_iteration()

        if self.proc and self.proc.poll() is None:
            try:
                self.proc.send_signal(signal.SIGINT)
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()

        if self.log_file:
            self.log_file.close()

        subprocess.Popen(["notify-send", "Screen Recorder", f"Video saved to {self.filename}", "--icon=video-x-generic"])
        Gtk.main_quit()

win = RecorderGUI()
win.show_all()
Gtk.main()
