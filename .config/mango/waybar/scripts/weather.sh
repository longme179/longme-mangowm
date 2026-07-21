#!/bin/bash
# ~/.config/waybar/scripts/weather.sh
# Custom waybar module: hiện thời tiết ngắn gọn, tooltip chi tiết hơn.
# Đổi "Ho+Chi+Minh+City" thành thành phố khác nếu cần.

LOCATION="Ho+Chi+Minh+City"

WEATHER=$(curl -s --max-time 5 "wttr.in/${LOCATION}?format=%c+%t")

if [ -z "$WEATHER" ]; then
    echo '{"text": "󰼵 --°", "tooltip": "Không lấy được dữ liệu thời tiết"}'
    exit 0
fi

TOOLTIP=$(curl -s --max-time 5 "wttr.in/${LOCATION}?format=%l:\n%C+%t,+cảm+giác+%f\nGió:+%w\nĐộ+ẩm:+%h\nMọc/lặn:+%S+/+%s")

# Escape để JSON hợp lệ (xuống dòng -> \n)
TOOLTIP_ESCAPED=$(echo "$TOOLTIP" | sed ':a;N;$!ba;s/\n/\\n/g' | sed 's/"/\\"/g')
TEXT_ESCAPED=$(echo "$WEATHER" | sed 's/"/\\"/g')

echo "{\"text\": \"${TEXT_ESCAPED}\", \"tooltip\": \"${TOOLTIP_ESCAPED}\"}"
