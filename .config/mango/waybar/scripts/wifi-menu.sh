#!/bin/bash
# ~/.config/waybar/scripts/wifi-menu.sh
# Menu rofi để xem/đổi mạng Wi-Fi qua NetworkManager (nmcli).
# Click trái vào module network trên waybar sẽ gọi script này.

ROFI_THEME_WIDTH="width: 480px;"

# Bật wifi nếu đang tắt
nmcli radio wifi on

# Quét lại danh sách (không chặn quá lâu)
nmcli device wifi rescan --rescan auto >/dev/null 2>&1
sleep 1

# Lấy danh sách SSID đang thấy, bỏ trùng, đánh dấu mạng đã kết nối bằng "*"
current_ssid=$(nmcli -t -f active,ssid dev wifi | awk -F: '$1=="yes"{print $2}')

wifi_list=$(nmcli -t -f ssid,signal,security dev wifi list | awk -F: '!seen[$1]++ && $1!=""')

menu_entries=$(echo "$wifi_list" | while IFS=: read -r ssid signal security; do
    mark=""
    [ "$ssid" = "$current_ssid" ] && mark=" (đang dùng)"
    lock=""
    [ -n "$security" ] && [ "$security" != "--" ] && lock=" 󰌾"
    echo "${ssid}${mark}${lock}  [${signal}%]"
done)

chosen=$(printf '%s\n 󰑓 Quét lại\n 󰖪 Tắt Wi-Fi\n 󰢾 Sửa kết nối (nm-connection-editor)' "$menu_entries" | \
    rofi -dmenu -i -p "Wi-Fi" -theme-str "window { $ROFI_THEME_WIDTH }")

[ -z "$chosen" ] && exit 0

case "$chosen" in
    *"Quét lại"*)
        nmcli device wifi rescan
        exec "$0"
        ;;
    *"Tắt Wi-Fi"*)
        nmcli radio wifi off
        exit 0
        ;;
    *"Sửa kết nối"*)
        nm-connection-editor
        exit 0
        ;;
esac

# Bóc lại SSID gốc từ dòng đã chọn (bỏ phần đánh dấu + tín hiệu)
ssid=$(echo "$chosen" | sed -E 's/ \(đang dùng\)//; s/ 󰌾//; s/  \[[0-9]+%\]$//')

# Nếu đã có profile lưu sẵn thì connect thẳng, chưa có thì hỏi mật khẩu
if nmcli -t -f NAME connection show | grep -Fxq "$ssid"; then
    nmcli connection up "$ssid"
else
    password=$(rofi -dmenu -p "Mật khẩu cho ${ssid}" -password)
    [ -z "$password" ] && exit 0
    nmcli device wifi connect "$ssid" password "$password"
fi
