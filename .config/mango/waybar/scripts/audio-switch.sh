#!/usr/bin/env bash
# ~/.config/waybar/scripts/audio-switch.sh
# Menu fuzzel để đổi thiết bị audio output (loa/tai nghe) hoặc input (mic).
# Right-click vào module loa/mic trên waybar sẽ gọi script này.
#
# Cách dùng:
#   audio-switch.sh output
#   audio-switch.sh input

set -euo pipefail

mode="${1:-output}"

if [[ "$mode" == "output" ]]; then
    list_cmd=(pactl list short sinks)
    default_cmd=(pactl get-default-sink)
    set_cmd=(pactl set-default-sink)
    title="Chọn thiết bị phát (loa/tai nghe)"
    stream_type="sink-inputs"
    move_cmd=(pactl move-sink-input)

elif [[ "$mode" == "input" ]]; then
    list_cmd=(pactl list short sources)
    default_cmd=(pactl get-default-source)
    set_cmd=(pactl set-default-source)
    title="Chọn thiết bị thu (mic)"
    stream_type="source-outputs"
    move_cmd=(pactl move-source-output)

else
    echo "Usage: $0 [output|input]" >&2
    exit 1
fi

if ! command -v pactl >/dev/null 2>&1; then
    notify-send -a "Audio" -u critical "Thiếu pactl" 2>/dev/null || true
    exit 1
fi

if ! command -v fuzzel >/dev/null 2>&1; then
    notify-send -a "Audio" -u critical "Thiếu fuzzel" 2>/dev/null || true
    exit 1
fi

current="$("${default_cmd[@]}" 2>/dev/null || true)"

# Cột 2 của `pactl list short sinks/sources` là tên thiết bị.
mapfile -t devices < <("${list_cmd[@]}" | awk 'NF >= 2 {print $2}')

if (( ${#devices[@]} == 0 )); then
    notify-send -a "Audio" "Không có thiết bị nào." -t 1500 2>/dev/null || true
    exit 0
fi

# Dấu đánh dấu thiết bị hiện tại.
# Nếu font không hiển thị được, có thể đổi thành: mark=" *"
mark=" ✓"

options=()
for dev in "${devices[@]}"; do
    if [[ "$dev" == "$current" ]]; then
        options+=("${dev}${mark}")
    else
        options+=("$dev")
    fi
done

# fuzzel dmenu mode.
# Nếu muốn truyền thêm arg cho fuzzel, ví dụ font/width/lines,
# có thể sửa dòng bên dưới thành:
#   fuzzel --dmenu --prompt "$title > " --width 80 --lines 12
chosen="$(printf '%s\n' "${options[@]}" | fuzzel --dmenu --prompt "$title > " || true)"

# Người dùng bấm Esc / huỷ.
if [[ -z "$chosen" ]]; then
    exit 0
fi

# Bỏ dấu "thiết bị hiện tại" nếu có.
chosen_dev="${chosen%"$mark"}"

# An toàn: chỉ chấp nhận thiết bị thật sự có trong danh sách.
if ! printf '%s\n' "${devices[@]}" | grep -Fxq -- "$chosen_dev"; then
    exit 0
fi

# Đặt thiết bị mặc định.
"${set_cmd[@]}" "$chosen_dev"

# Chuyển các stream đang chạy sang thiết bị mới luôn, không cần restart app.
pactl list short "$stream_type" | awk '{print $1}' | while read -r id; do
    [[ -n "$id" ]] || continue
    "${move_cmd[@]}" "$id" "$chosen_dev" || true
done

notify-send -a "Audio" "Đã đổi sang: ${chosen_dev}" -t 1500 2>/dev/null || true
