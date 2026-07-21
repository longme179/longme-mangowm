#!/bin/bash
# ~/.config/waybar/scripts/nightmode-toggle.sh
# Bật/tắt night mode (lọc ánh sáng xanh) qua wlsunset.
# Right-click vào module backlight trên waybar sẽ gọi script này.

if pgrep -x wlsunset > /dev/null; then
    pkill -x wlsunset
    notify-send -a "Night Mode" "Đã tắt night mode" -i weather-clear-night -t 1500
else
    # -t: nhiệt độ ban đêm, -T: nhiệt độ ban ngày (nếu muốn chạy 24/24 thủ công)
    wlsunset -t 4000 -T 6500 &
    disown
    notify-send -a "Night Mode" "Đã bật night mode" -i weather-clear-night -t 1500
fi
