#!/usr/bin/env python3
#
# Screen Recorder GUI for MangoWM / Wayland
# Backend: wl-screenrec
# Audio: captures output monitor, excludes microphone
#

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib

import subprocess
import signal
import os
import sys
import threading
from shutil import which
from datetime import datetime

try:
    gi.require_version("GtkLayerShell", "1.0")
    from gi.repository import GtkLayerShell
    HAS_LAYER_SHELL = True
except (ValueError, ImportError):
    HAS_LAYER_SHELL = False


# ======================================================
# CONFIG
# ======================================================

VIDEO_DIR = os.path.expanduser("~/Videos/Recordings")

MAX_FPS = "60"
CODEC = "hevc"

# Ví dụ: "2M", "4M", hoặc để trống nếu không muốn giới hạn bitrate.
# Cú pháp chính xác phụ thuộc version wl-screenrec, hãy test.
BITRATE = ""

# Nếu muốn ép một thiết bị âm thanh cụ thể, điền vào đây.
# Ví dụ:
# AUDIO_DEVICE_OVERRIDE = "alsa_output.pci-0000_05_00.6.analog-stereo.monitor"
AUDIO_DEVICE_OVERRIDE = ""

# Bật True nếu bạn muốn thu toàn bộ output từ nhiều sink bằng combine sink.
# False: chỉ thu monitor của sink mặc định.
RECORD_ALL_OUTPUT = True

COMBINE_SINK_NAME = "screenrec_all_output"
FALLBACK_MONITOR = "alsa_output.pci-0000_05_00.6.analog-stereo.monitor"

# ======================================================


CSS = """
window {
    background-color: rgba(25, 23, 36, 0.85);
}

.box {
    padding: 30px;
}

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

.hint {
    color: #908caa;
    font-size: 12px;
    margin-top: 15px;
}
"""


def notify(title, body, icon="dialog-information"):
    """
    Notify không chặn UI.
    """
    cmd = [
        "notify-send",
        "--app-name", "Screen Recorder",
        "--icon", icon,
        title,
        body,
    ]

    threading.Thread(
        target=subprocess.run,
        args=(cmd,),
        kwargs={
            "check": False,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        },
        daemon=True,
    ).start()


def run_cmd(cmd, timeout=5):
    """
    Chạy lệnh an toàn, không văng exception.
    """
    try:
        return subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except Exception:
        return subprocess.CompletedProcess(cmd, 1, "", "")


def get_default_sink():
    r = run_cmd(["pactl", "get-default-sink"])
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return None


def source_exists(source_name):
    if not source_name:
        return False

    r = run_cmd(["pactl", "list", "short", "sources"])
    if r.returncode != 0:
        return False

    return source_name in r.stdout


def get_default_monitor():
    """
    Trả về monitor của sink mặc định.
    Monitor của output thì không thu mic.
    """
    if AUDIO_DEVICE_OVERRIDE:
        return AUDIO_DEVICE_OVERRIDE

    sink = get_default_sink()
    if sink:
        monitor = f"{sink}.monitor"
        if source_exists(monitor):
            return monitor

    if source_exists(FALLBACK_MONITOR):
        return FALLBACK_MONITOR

    return ""


def unload_stale_combine_sink():
    """
    Dọn combine sink cũ nếu còn.
    """
    r = run_cmd(["pactl", "list", "short", "modules"])
    if r.returncode != 0:
        return

    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            module_id = parts[0]
            module_name = parts[1]

            if module_name == "module-combine-sink" and COMBINE_SINK_NAME in line:
                run_cmd(["pactl", "unload-module", module_id])


def move_all_streams_to_sink(sink_name, exclude_module_id=None):
    """
    Cố gắng chuyển các stream âm thanh đang chạy sang sink được chỉ định.

    exclude_module_id dùng để không chuyển chính các stream nội bộ
    của module-combine-sink, tránh loop.
    """
    r = run_cmd(["pactl", "list", "sink-inputs"], timeout=10)
    if r.returncode != 0:
        return

    blocks = r.stdout.split("Sink Input #")[1:]

    for block in blocks:
        lines = block.splitlines()
        if not lines:
            continue

        input_id = lines[0].strip()

        if exclude_module_id:
            if (
                f'module.id = "{exclude_module_id}"' in block
                or f"module.id = {exclude_module_id}" in block
            ):
                continue

        run_cmd(["pactl", "move-sink-input", input_id, sink_name], timeout=5)


class RecorderGUI(Gtk.Window):
    def __init__(self):
        super().__init__(title="Recorder")
        self.set_decorated(False)

        if HAS_LAYER_SHELL:
            GtkLayerShell.init_for_window(self)
            GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)

            for edge in [
                GtkLayerShell.Edge.TOP,
                GtkLayerShell.Edge.BOTTOM,
                GtkLayerShell.Edge.LEFT,
                GtkLayerShell.Edge.RIGHT,
            ]:
                GtkLayerShell.set_anchor(self, edge, True)
        else:
            self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
            self.set_keep_above(True)
            self.set_default_size(340, 180)
            self.set_position(Gtk.WindowPosition.CENTER)

        self.connect("destroy", self.on_destroy)
        self.connect("key-press-event", self.on_key_press)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

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

        self.hint = Gtk.Label(label="Click Start để bắt đầu quay")
        self.hint.get_style_context().add_class("hint")
        self.hint.set_line_wrap(True)
        self.hint.set_justify(Gtk.Justification.CENTER)
        main_box.pack_start(self.hint, False, False, 0)

        self.is_recording = False
        self.is_stopping = False
        self.proc = None
        self.filename = ""

        self.poll_id = None
        self.stop_ticks = 0

        self.audio_module_id = None
        self.old_default_sink = None

    def prepare_audio_device(self):
        """
        Chuẩn bị thiết bị âm thanh để thu.

        Mặc định:
            - Thu monitor của sink mặc định.
            - Không thu mic.

        Nếu RECORD_ALL_OUTPUT = True:
            - Tạo combine sink.
            - Chuyển stream sang combine sink.
            - Thu monitor của combine sink.
        """
        self.audio_module_id = None
        self.old_default_sink = get_default_sink()

        if not which("pactl"):
            notify(
                "Screen Recorder",
                "Không tìm thấy pactl. Sẽ quay không có âm thanh.",
                "dialog-warning",
            )
            return ""

        if AUDIO_DEVICE_OVERRIDE:
            if source_exists(AUDIO_DEVICE_OVERRIDE):
                return AUDIO_DEVICE_OVERRIDE

            notify(
                "Screen Recorder",
                f"Audio device override không tồn tại:\n{AUDIO_DEVICE_OVERRIDE}",
                "dialog-warning",
            )

        if RECORD_ALL_OUTPUT:
            unload_stale_combine_sink()

            r = run_cmd([
                "pactl",
                "load-module",
                "module-combine-sink",
                f"sink_name={COMBINE_SINK_NAME}",
                "sink_properties=device.description=ScreenRec_All_Output",
            ])

            if r.returncode == 0 and r.stdout.strip():
                self.audio_module_id = r.stdout.strip()

                run_cmd(["pactl", "set-default-sink", COMBINE_SINK_NAME])

                move_all_streams_to_sink(
                    COMBINE_SINK_NAME,
                    exclude_module_id=self.audio_module_id,
                )

                monitor = f"{COMBINE_SINK_NAME}.monitor"

                if source_exists(monitor):
                    return monitor

            notify(
                "Screen Recorder",
                "Không tạo được combine sink. Dùng monitor mặc định.",
                "dialog-warning",
            )

        monitor = get_default_monitor()

        if monitor and source_exists(monitor):
            return monitor

        notify(
            "Screen Recorder",
            "Không tìm thấy audio monitor. Sẽ quay không có âm thanh.",
            "dialog-warning",
        )

        return ""

    def cleanup_audio(self):
        """
        Xóa combine sink và trả lại sink cũ nếu đã tạo.
        """
        if not self.audio_module_id:
            return

        if self.old_default_sink:
            run_cmd(["pactl", "set-default-sink", self.old_default_sink])

            move_all_streams_to_sink(
                self.old_default_sink,
                exclude_module_id=self.audio_module_id,
            )

        run_cmd(["pactl", "unload-module", self.audio_module_id])
        self.audio_module_id = None

    def on_start_clicked(self, widget):
        if self.is_recording or self.is_stopping:
            return

        if not which("wl-screenrec"):
            notify(
                "Screen Recorder",
                "Không tìm thấy wl-screenrec. Hãy cài wl-screenrec trước.",
                "dialog-error",
            )
            return

        os.makedirs(VIDEO_DIR, exist_ok=True)

        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.filename = os.path.join(
            VIDEO_DIR,
            f"recording_{timestamp}.mp4",
        )

        audio_device = self.prepare_audio_device()

        cmd = [
            "wl-screenrec",
            "--max-fps", MAX_FPS,
            "--codec", CODEC,
        ]

        if audio_device:
            cmd += [
                "--audio",
                "--audio-device", audio_device,
            ]

        if BITRATE:
            cmd += ["-b", BITRATE]

        cmd += ["-f", self.filename]

        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            notify(
                "Screen Recorder",
                f"Không thể khởi chạy wl-screenrec:\n{e}",
                "dialog-error",
            )
            self.cleanup_audio()
            return

        self.is_recording = True
        self.is_stopping = False

        self.btn_start.hide()
        self.btn_stop.show()
        self.btn_stop.set_sensitive(True)
        self.btn_stop.set_label("󰛊 Stop & Save")

        if RECORD_ALL_OUTPUT and self.audio_module_id:
            audio_state = "Audio: all output"
        elif audio_device:
            audio_state = "Audio: default output"
        else:
            audio_state = "No audio"

        self.hint.set_label(
            f"Recording {MAX_FPS}fps • {audio_state}\n"
            f"{os.path.basename(self.filename)}\n"
            "Bấm Stop hoặc ESC để lưu."
        )

        self.poll_id = GLib.timeout_add(500, self.poll_recorder)

        notify(
            "Screen Recorder",
            f"Bắt đầu quay:\n{os.path.basename(self.filename)}",
            "media-record",
        )

    def poll_recorder(self):
        """
        Kiểm tra nếu wl-screenrec tự chết giữa chừng.
        """
        if not self.is_recording:
            return False

        if self.proc and self.proc.poll() is not None:
            self.is_recording = False
            self.is_stopping = False

            returncode = self.proc.returncode
            self.cleanup_audio()
            self.reset_ui()

            notify(
                "Screen Recorder",
                f"wl-screenrec tự dừng bất ngờ.\nExit code: {returncode}",
                "dialog-warning",
            )

            return False

        return True

    def on_stop_clicked(self, *args):
        if self.is_stopping:
            return

        if not self.is_recording:
            Gtk.main_quit()
            return

        self.is_recording = False
        self.is_stopping = True

        if self.poll_id:
            GLib.source_remove(self.poll_id)
            self.poll_id = None

        self.btn_stop.set_label("Saving...")
        self.btn_stop.set_sensitive(False)
        self.hint.set_label("Đang lưu file, vui lòng chờ...")

        while Gtk.events_pending():
            Gtk.main_iteration()

        if self.proc and self.proc.poll() is None:
            try:
                # SIGINT giúp wl-screenrec finalize file an toàn hơn SIGKILL.
                self.proc.send_signal(signal.SIGINT)
            except Exception:
                pass

            self.stop_ticks = 0
            GLib.timeout_add(100, self.wait_stop)
        else:
            self.finish_stop()

    def wait_stop(self):
        """
        Chờ wl-screenrec lưu file, tối đa khoảng 5 giây.
        Nếu quá lâu thì kill.
        """
        self.stop_ticks += 1

        if self.proc and self.proc.poll() is not None:
            self.finish_stop()
            return False

        if self.stop_ticks >= 50:
            if self.proc:
                try:
                    self.proc.kill()
                except Exception:
                    pass

            self.finish_stop()
            return False

        return True

    def finish_stop(self):
        self.cleanup_audio()

        notify(
            "Screen Recorder",
            f"Đã lưu:\n{self.filename}",
            "video-x-generic",
        )

        Gtk.main_quit()

    def reset_ui(self):
        self.btn_start.show()

        self.btn_stop.hide()
        self.btn_stop.set_sensitive(True)
        self.btn_stop.set_label("󰛊 Stop & Save")

        self.hint.set_label("Click Start để bắt đầu quay")

    def on_destroy(self, *args):
        if self.is_stopping:
            return

        if self.is_recording:
            self.on_stop_clicked()
        else:
            Gtk.main_quit()

    def on_key_press(self, widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.on_stop_clicked()
            return True

        return False


def main():
    if not which("wl-screenrec"):
        notify(
            "Screen Recorder",
            "Không tìm thấy wl-screenrec. Hãy cài wl-screenrec trước.",
            "dialog-error",
        )
        sys.exit(1)

    win = RecorderGUI()
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
