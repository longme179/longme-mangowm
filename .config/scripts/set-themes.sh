#!/bin/bash
# Script an toàn tuyệt đối để set Cursor/App Icon theme cho MangoWM
# Tính năng: Idempotent, không vỡ khi Esc, không lỗi khi thiếu theme, validate input.
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
    # Không thể dùng notify-send nếu chính nó bị thiếu
    if ! echo "$MISSING" | grep -q "notify-send"; then
        notify-send "Lỗi" "Thiếu các công cụ:$MISSING" --icon=dialog-error
    fi
    exit 1
fi

# Bắt lỗi runtime để báo cho user biết (thay cho set -e)
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
                # Dùng -L để theo symlink (fix lỗi bỏ sót theme symlink)
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
    # Thêm || true để grep không làm script văng ra nếu không có kết quả
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

# Dùng || true để khi bấm Esc (exit code 1), script không chết ngang
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
# Chống injection/gõ bậy, nếu không phải số thì mặc định 24
if ! [[ "$CURSOR_SIZE" =~ ^[0-9]+$ ]]; then
    CURSOR_SIZE=24
fi

# 4. Chọn App Icon Theme (Optional)
ICON_THEME=""
if [ -n "$ICON_LIST" ]; then
    ICON_THEME=$(echo -e "$ICON_LIST" | fuzzel -d -p "󰀻 Select App Icon Theme: ") || true
fi

echo "Đang áp dụng themes..."

# 5. Ghi đè environment.d
mkdir -p "$HOME/.config/environment.d"
cat <<EOF > "$HOME/.config/environment.d/cursor.conf"
XCURSOR_THEME=$CURSOR_THEME
XCURSOR_SIZE=$CURSOR_SIZE
EOF

# 6. Ghi đè GSettings (Dùng || true để nếu schema lỗi không làm văng script)
gsettings set org.gnome.desktop.interface cursor-theme "$CURSOR_THEME" || true
gsettings set org.gnome.desktop.interface cursor-size "$CURSOR_SIZE" || true
if [ -n "$ICON_THEME" ]; then
    gsettings set org.gnome.desktop.interface icon-theme "$ICON_THEME" || true
fi

# 7. Ghi đè X11 Fallback
mkdir -p "$HOME/.icons/default"
cat <<EOF > "$HOME/.icons/default/index.theme"
[Icon Theme]
Inherits=$CURSOR_THEME
EOF

# 8. Cập nhật Qt an toàn (Append nếu chưa có, Replace nếu đã có)
for qt in qt5ct qt6ct; do
    conf="$HOME/.config/$qt/$qt.conf"
    if [ -f "$conf" ]; then
        if [ -n "$ICON_THEME" ]; then
            if grep -q "^icon_theme=" "$conf"; then
                sed -i "/^icon_theme=/c\icon_theme=$ICON_THEME" "$conf"
            else
                # Nếu chưa có, append vào dưới cùng
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

# 9. Thông báo thành công (Dùng $'...' để xuống dòng thực sự)
notify-send "Themes Applied!" $'Cursor: '"$CURSOR_THEME"' ('"$CURSOR_SIZE"$')\nIcons: '"${ICON_THEME:-None}"$'\n\nĐăng xuất và đăng nhập lại để áp dụng 100%.' --icon=preferences-desktop-theme
echo "Xong! Hãy đăng xuất MangoWM ra và vào lại để apply toàn bộ."
