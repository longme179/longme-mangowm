#!/bin/bash
# Script an toàn tuyệt đối để set Cursor/App Icon theme cho MangoWM
# Tính năng: Idempotent, không vỡ khi Esc, không lỗi khi thiếu theme, validate input.
# Tích hợp ghi thẳng vào env.conf của MangoWM.
# Yêu cầu: fuzzel, gsettings, notify-send

# 1. Kiểm tra dependencies
MISSING=""
for cmd in fuzzel gsettings notify-send; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        MISSING="$MISSING $cmd"
    fi
done

if [ -n "$MISSING" ]; then
    echo "Lỗi: Thiếu các công cụ:$MISSING"
    if ! echo "$MISSING" | grep -q "notify-send"; then
        notify-send "Lỗi" "Thiếu các công cụ:$MISSING" --icon=dialog-error
    fi
    exit 1
fi

trap 'notify-send "Lỗi" "Script dừng bất ngờ ở dòng $LINENO." --icon=dialog-error' ERR

ICON_DIRS=(
    "$HOME/.local/share/icons"
    "/usr/share/icons"
)

get_themes() {
    local type=$1
    local found=""
    for dir in "${ICON_DIRS[@]}"; do
        if [ -d "$dir" ]; then
            if [ "$type" == "cursor" ]; then
                while IFS= read -r line; do
                    found="$found$(basename "$line")\n"
                done < <(find -L "$dir" -maxdepth 2 -type d -name "cursors" -exec dirname {} \; 2>/dev/null)
            elif [ "$type" == "icon" ]; then
                while IFS= read -r line; do
                    found="$found$(basename "$line")\n"
                done < <(find -L "$dir" -maxdepth 2 -type f -name "index.theme" -exec dirname {} \; 2>/dev/null)
            fi
        fi
    done
    echo -e "$found" | sort -u | grep -v '^$' || true
}

echo "Đang quét các theme có trong máy..."
CURSOR_LIST=$(get_themes cursor)
ICON_LIST=$(get_themes icon)

# 2. Chọn Cursor Theme
if [ -z "$CURSOR_LIST" ]; then
    notify-send "Lỗi" "Không tìm thấy Cursor Theme nào trong máy!" --icon=dialog-error
    exit 1
fi

CURSOR_THEME=$(echo -e "$CURSOR_LIST" | fuzzel -d -p "󰇼 Select Cursor Theme: ") || true
if [ -z "$CURSOR_THEME" ]; then
    echo "Đã hủy thao tác."
    exit 0
fi

# 3. Chọn Cursor Size (Validate số)
CURSOR_SIZE=$(printf "16\n24\n32\n48" | fuzzel -d -p "󰇒 Select Cursor Size: ") || true
if [ -z "$CURSOR_SIZE" ]; then
    CURSOR_SIZE=24
fi
if ! [[ "$CURSOR_SIZE" =~ ^[0-9]+$ ]]; then
    CURSOR_SIZE=24
fi

# 4. Chọn App Icon Theme (Optional)
ICON_THEME=""
if [ -n "$ICON_LIST" ]; then
    ICON_THEME=$(echo -e "$ICON_LIST" | fuzzel -d -p "󰀻 Select App Icon Theme: ") || true
fi

echo "Đang áp dụng themes..."

# 5. Ghi đè environment.d (Cho các app không qua MangoWM)
mkdir -p "$HOME/.config/environment.d"
cat <<EOF > "$HOME/.config/environment.d/cursor.conf"
XCURSOR_THEME=$CURSOR_THEME
XCURSOR_SIZE=$CURSOR_SIZE
EOF

# 6. Ghi đè vào env.conf của MangoWM (Cực kỳ quan trọng cho Firefox/Qt)
MANGO_ENV="$HOME/.config/mango/env.conf"
if [ -f "$MANGO_ENV" ]; then
    # Xóa các dòng XCURSOR cũ nếu có
    sed -i '/^env=XCURSOR_THEME/d' "$MANGO_ENV"
    sed -i '/^env=XCURSOR_SIZE/d' "$MANGO_ENV"
    # Thêm dòng mới vào cuối file
    echo "env=XCURSOR_THEME,$CURSOR_THEME" >> "$MANGO_ENV"
    echo "env=XCURSOR_SIZE,$CURSOR_SIZE" >> "$MANGO_ENV"
fi

# 7. Ghi đè GSettings
gsettings set org.gnome.desktop.interface cursor-theme "$CURSOR_THEME" || true
gsettings set org.gnome.desktop.interface cursor-size "$CURSOR_SIZE" || true
if [ -n "$ICON_THEME" ]; then
    gsettings set org.gnome.desktop.interface icon-theme "$ICON_THEME" || true
fi

# 8. Ghi đè X11 Fallback
mkdir -p "$HOME/.icons/default"
cat <<EOF > "$HOME/.icons/default/index.theme"
[Icon Theme]
Inherits=$CURSOR_THEME
EOF

# 9. Cập nhật Qt an toàn (Append nếu chưa có, Replace nếu đã có)
for qt in qt5ct qt6ct; do
    conf="$HOME/.config/$qt/$qt.conf"
    if [ -f "$conf" ]; then
        if [ -n "$ICON_THEME" ]; then
            if grep -q "^icon_theme=" "$conf"; then
                sed -i "/^icon_theme=/c\icon_theme=$ICON_THEME" "$conf"
            else
                echo "icon_theme=$ICON_THEME" >> "$conf"
            fi
        fi

        if grep -q "^cursor_theme=" "$conf"; then
            sed -i "/^cursor_theme=/c\cursor_theme=$CURSOR_THEME" "$conf"
        else
            echo "cursor_theme=$CURSOR_THEME" >> "$conf"
        fi
    fi
done

# 10. Thông báo thành công
notify-send "Themes Applied!" $'Cursor: '"$CURSOR_THEME"' ('"$CURSOR_SIZE"$')\nIcons: '"${ICON_THEME:-None}"$'\n\nĐăng xuất MangoWM để apply 100%.' --icon=preferences-desktop-theme
echo "Xong! Hãy đăng xuất MangoWM ra và vào lại để apply toàn bộ."
