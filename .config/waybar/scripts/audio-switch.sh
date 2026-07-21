#!/bin/bash
# ~/.config/waybar/scripts/audio-switch.sh
# Menu rofi để đổi thiết bị audio output (loa/tai nghe) hoặc input (mic).
# Right-click vào module loa/mic trên waybar sẽ gọi script này.
# Cách dùng: audio-switch.sh output   hoặc   audio-switch.sh input

mode="$1"
[ -z "$mode" ] && mode="output"

if [ "$mode" = "output" ]; then
    list_cmd="pactl list short sinks"
    default_cmd="pactl get-default-sink"
    set_cmd="pactl set-default-sink"
    title="Chọn thiết bị phát (loa/tai nghe)"
else
    list_cmd="pactl list short sources"
    default_cmd="pactl get-default-source"
    set_cmd="pactl set-default-source"
    title="Chọn thiết bị thu (mic)"
fi

current=$($default_cmd)

# Cột 2 của `pactl list short sinks/sources` là tên thiết bị (dùng để set-default)
mapfile -t devices < <($list_cmd | awk '{print $2}')

menu=""
for dev in "${devices[@]}"; do
    mark=""
    [ "$dev" = "$current" ] && mark=" 󰄬"
    menu+="${dev}${mark}\n"
done

chosen=$(echo -e "$menu" | rofi -dmenu -i -p "$title")
[ -z "$chosen" ] && exit 0

chosen_dev=$(echo "$chosen" | sed 's/ 󰄬$//')
$set_cmd "$chosen_dev"

# Chuyển các stream đang chạy sang thiết bị mới luôn, không cần restart app
if [ "$mode" = "output" ]; then
    pactl list short sink-inputs | awk '{print $1}' | while read -r id; do
        pactl move-sink-input "$id" "$chosen_dev"
    done
else
    pactl list short source-outputs | awk '{print $1}' | while read -r id; do
        pactl move-source-output "$id" "$chosen_dev"
    done
fi

notify-send -a "Audio" "Đã đổi sang: ${chosen_dev}" -t 1500
