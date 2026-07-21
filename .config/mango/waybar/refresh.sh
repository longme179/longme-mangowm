#!/bin/bash
CONFIG_FILE="$HOME/.config/mango/waybar/config.jsonc"
STYLE_FILE="$HOME/.config/mango/waybar/style.css"
WAYBAR_CMD="waybar -c $CONFIG_FILE -s $STYLE_FILE"

# Xóa waybar cũ nếu đang chạy
killall waybar 2>/dev/null

# Chạy waybar
 $WAYBAR_CMD >/dev/null 2>&1 &

# Theo dõi sự thay đổi của file (dùng inotifywait)
while inotifywait -q -e close_write,modify "$CONFIG_FILE" "$STYLE_FILE"; do
    # Khi file thay đổi, kill waybar và chạy lại
    killall waybar 2>/dev/null
    $WAYBAR_CMD >/dev/null 2>&1 &
done
