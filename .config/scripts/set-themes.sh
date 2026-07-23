#!/bin/bash
# Script set Cursor Theme / Cursor Size / App Icon Theme cho MangoWM
# Idempotent: chạy lại nhiều lần không chồng file, không lặp dòng cấu hình.
# Yêu cầu: fuzzel, gsettings, notify-send
# Tùy chọn: mmsg (MangoWM IPC) - nếu có sẽ tự reload_config sau khi set xong.
#
# Áp dụng theme ở 5 tầng: environment.d (session) -> gsettings (GTK/live) ->
# X11/XWayland fallback (~/.icons/default) -> qt5ct/qt6ct -> cấu hình gốc
# của Mango (cursor_size=/cursor_theme=/env=XCURSOR_* trong env.conf).
#
# Lưu ý: KHÔNG dùng `set -e`. fuzzel (dmenu-compatible) và grep trả về khác 0
# khi người dùng hủy chọn hoặc khi không tìm thấy theme - đó là chuyện bình
# thường, không phải lỗi thật, nên mỗi bước tự kiểm tra exit code lấy từ $?
# thay vì để set -e âm thầm giết cả script.

MANGO_CONFIG_DIR="$HOME/.config/mango"
ENV_CONF="$MANGO_CONFIG_DIR/env.conf"

err() {
    echo "Lỗi: $1" >&2
    command -v notify-send >/dev/null 2>&1 && notify-send "Lỗi" "$1" --icon=dialog-error
}

warn() {
    echo "Cảnh báo: $1" >&2
}

# 1. Kiểm tra dependencies (bắt buộc: fuzzel, gsettings, notify-send)
for dep in fuzzel gsettings notify-send; do
    if ! command -v "$dep" >/dev/null 2>&1; then
        echo "Lỗi: Không tìm thấy '$dep'. Hãy cài đặt nó trước!" >&2
        [ "$dep" != "notify-send" ] && command -v notify-send >/dev/null 2>&1 && \
            notify-send "Lỗi" "Không tìm thấy '$dep'. Hãy cài đặt nó trước!" --icon=dialog-error
        exit 1
    fi
done

ICON_DIRS=(
    "$HOME/.icons"
    "$HOME/.local/share/icons"
    "/usr/share/icons"
)

# Quét theme. -L để find theo cả symlink (nhiều package cursor theme cài qua symlink).
get_themes() {
    local type="$1"
    local found=""
    for dir in "${ICON_DIRS[@]}"; do
        [ -d "$dir" ] || continue
        if [ "$type" == "cursor" ]; then
            while IFS= read -r line; do
                found="$found$(basename "$line")\n"
            done < <(find -L "$dir" -maxdepth 2 -type d -name "cursors" -exec dirname {} \; 2>/dev/null)
        elif [ "$type" == "icon" ]; then
            while IFS= read -r line; do
                found="$found$(basename "$line")\n"
            done < <(find -L "$dir" -maxdepth 2 -type f -name "index.theme" -exec dirname {} \; 2>/dev/null)
        fi
    done
    echo -e "$found" | sort -u | grep -v '^$'
    return 0
}

echo "Đang quét các theme có trong máy..."
CURSOR_LIST=$(get_themes cursor)
ICON_LIST=$(get_themes icon)

# 2. Chọn Cursor Theme (bắt buộc)
if [ -z "$CURSOR_LIST" ]; then
    err "Không tìm thấy Cursor Theme nào trong máy!"
    exit 1
fi
CURSOR_THEME=$(echo -e "$CURSOR_LIST" | fuzzel -d -p "󰇼 Select Cursor Theme: ")
if [ -z "$CURSOR_THEME" ]; then
    echo "Đã hủy thao tác."
    exit 0
fi

# 3. Chọn Cursor Size (hủy hoặc gõ không phải số -> mặc định 24, không âm thầm nhận rác)
CURSOR_SIZE=$(printf "16\n24\n32\n48" | fuzzel -d -p "󰇒 Select Cursor Size: ")
if ! [[ "$CURSOR_SIZE" =~ ^[0-9]+$ ]]; then
    CURSOR_SIZE=24
fi

# 4. Chọn App Icon Theme (tùy chọn - hủy hoặc không có theme nào = bỏ qua bước
#    này, KHÔNG được làm hỏng phần cursor theme/size vừa chọn ở trên)
ICON_THEME=""
if [ -n "$ICON_LIST" ]; then
    ICON_THEME=$(echo -e "$ICON_LIST" | fuzzel -d -p "󰀻 Select App Icon Theme: ")
fi

echo "Đang áp dụng themes..."

# 5. environment.d (session, ghi đè - không chồng file)
mkdir -p "$HOME/.config/environment.d"
cat <<EOF > "$HOME/.config/environment.d/cursor.conf"
XCURSOR_THEME=$CURSOR_THEME
XCURSOR_SIZE=$CURSOR_SIZE
EOF

# 6. GSettings - một schema thiếu/từ chối giá trị không được kéo sập các bước sau
if ! gsettings set org.gnome.desktop.interface cursor-theme "$CURSOR_THEME" 2>/dev/null; then
    warn "gsettings không set được cursor-theme (có thể thiếu gsettings-desktop-schemas)."
fi
if ! gsettings set org.gnome.desktop.interface cursor-size "$CURSOR_SIZE" 2>/dev/null; then
    warn "gsettings không set được cursor-size."
fi
if [ -n "$ICON_THEME" ]; then
    if ! gsettings set org.gnome.desktop.interface icon-theme "$ICON_THEME" 2>/dev/null; then
        warn "gsettings không set được icon-theme."
    fi
fi

# 7. X11 / XWayland fallback (bạn có QT_QPA_PLATFORM=Wayland;xcb nên phần này vẫn cần)
mkdir -p "$HOME/.icons/default"
cat <<EOF > "$HOME/.icons/default/index.theme"
[Icon Theme]
Inherits=$CURSOR_THEME
EOF

# 8. Qt (qt5ct/qt6ct): thay nếu key đã có, THÊM nếu chưa có (bản cũ chỉ thay,
#    nên nếu key chưa từng tồn tại thì nó lặng lẽ không set được gì cả)
set_qt_kv() {
    local conf="$1" key="$2" value="$3"
    if grep -q "^${key}=" "$conf" 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${value}|" "$conf"
    elif grep -q "^\[Appearance\]" "$conf" 2>/dev/null; then
        sed -i "/^\[Appearance\]/a ${key}=${value}" "$conf"
    else
        printf '%s=%s\n' "$key" "$value" >> "$conf"
    fi
}
for qt in qt5ct qt6ct; do
    conf="$HOME/.config/$qt/$qt.conf"
    if [ -f "$conf" ]; then
        cp "$conf" "$conf.bak" 2>/dev/null
        [ -n "$ICON_THEME" ] && set_qt_kv "$conf" "icon_theme" "$ICON_THEME"
        set_qt_kv "$conf" "cursor_theme" "$CURSOR_THEME"
    fi
done

# 9. MangoWM native: cursor_size=/cursor_theme= là key gốc của Mango (không chỉ
#    biến môi trường), đọc trực tiếp trong config.conf/env.conf - set ở đây để
#    compositor tự vẽ đúng cursor, không chỉ phụ thuộc gsettings/XWayland.
mango_set_plain() {  # key value file
    local key="$1" value="$2" file="$3"
    if grep -q "^${key}=" "$file" 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${value}|" "$file"
    else
        echo "${key}=${value}" >> "$file"
    fi
}
mango_set_env() {  # key value file
    local key="$1" value="$2" file="$3"
    if grep -q "^env=${key}," "$file" 2>/dev/null; then
        sed -i "s|^env=${key},.*|env=${key},${value}|" "$file"
    else
        echo "env=${key},${value}" >> "$file"
    fi
}
MANGO_RELOADED=0
if [ -d "$MANGO_CONFIG_DIR" ]; then
    mkdir -p "$MANGO_CONFIG_DIR"
    touch "$ENV_CONF"
    cp "$ENV_CONF" "$ENV_CONF.bak" 2>/dev/null
    mango_set_plain "cursor_size" "$CURSOR_SIZE" "$ENV_CONF"
    mango_set_plain "cursor_theme" "$CURSOR_THEME" "$ENV_CONF"
    mango_set_env "XCURSOR_SIZE" "$CURSOR_SIZE" "$ENV_CONF"
    mango_set_env "XCURSOR_THEME" "$CURSOR_THEME" "$ENV_CONF"
    if command -v mmsg >/dev/null 2>&1; then
        mmsg -d reload_config >/dev/null 2>&1 && MANGO_RELOADED=1
    fi
fi

# 10. Thông báo - dùng printf để \n thành xuống dòng thật, không phải 2 ký tự "\n"
BODY=$(printf 'Cursor: %s (%s)\nIcons: %s\n\n%s' \
    "$CURSOR_THEME" "$CURSOR_SIZE" "${ICON_THEME:-None}" \
    "GTK/Qt app đang mở cần khởi động lại (hoặc đăng xuất/đăng nhập) để nhận theme mới hoàn toàn.")
notify-send "Themes Applied!" "$BODY" --icon=preferences-desktop-theme
echo "Xong!"
if [ "$MANGO_RELOADED" == "1" ]; then
    echo "(đã gọi mmsg -d reload_config cho Mango)"
fi
exit 0
