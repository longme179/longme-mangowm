#!/bin/bash
# ~/.config/waybar/scripts/wifi-menu.sh
# Fuzzel-based Wi-Fi menu for waybar (NetworkManager / nmcli).

set -o pipefail

FUZZEL_WIDTH=60
FUZZEL=(fuzzel --dmenu --width "$FUZZEL_WIDTH" --match-mode fuzzy)

# ---------- helpers ----------
have() { command -v "$1" >/dev/null 2>&1; }

notify() {  # notify <summary> <body>
    have notify-send && notify-send -a "Wi-Fi" "$1" "$2"
}

# Detect a terminal emulator for nmtui fallback
detect_term() {
    local t
    for t in foot kitty alacritty wezterm gnome-terminal; do
        have "$t" && { echo "$t"; return; }
    done
}

# ---------- ensure radio on + rescan ----------
nmcli radio wifi on >/dev/null 2>&1
nmcli device wifi rescan --rescan auto >/dev/null 2>&1

current_ssid=$(nmcli -t -f active,ssid dev wifi | awk -F: '$1=="yes"{print $2}')

# ---------- build network list ----------
wifi_list=$(nmcli -t -f ssid,signal,security dev wifi list \
  | awk -F: '!seen[$1]++ && $1!=""' \
  | while IFS=: read -r ssid signal security; do
      mark=""; lock=""
      [ "$ssid" = "$current_ssid" ] && mark=" *"
      [ -n "$security" ] && [ "$security" != "--" ] && lock=" 󰌾"
      printf '%s%s%s  [%s%%]\n' "$ssid" "$mark" "$lock" "$signal"
    done)

# ---------- main menu ----------
actions=""
[ -n "$current_ssid" ] && actions="${actions}\n 󰛳 Connection Info"
actions="${actions}\n 󰑓 Rescan"

if [ "$(nmcli radio wifi)" = "enabled" ]; then
    actions="${actions}\n 󰖪 Turn Off Wi-Fi"
else
    actions="${actions}\n 󰖪 Turn On Wi-Fi"
fi

actions="${actions}\n 󰅝 Forget Network"
have nm-connection-editor && actions="${actions}\n 󰢾 Edit Connections"
[ -n "$(detect_term)" ]   && actions="${actions}\n 󰞷 nmtui (terminal)"
have rfkill               && actions="${actions}\n 󰀝 Toggle Airplane Mode"

chosen=$(printf "%b\n%s" "$wifi_list" "$(echo -e "$actions" | sed '/^$/d')" \
  | "${FUZZEL[@]}" --prompt "Wi-Fi> ")

[ -z "$chosen" ] && exit 0

# ---------- action handlers ----------
case "$chosen" in
    *"Rescan"*)
        nmcli device wifi rescan >/dev/null 2>&1
        exec "$0" ;;
    *"Turn Off Wi-Fi"*)
        nmcli radio wifi off; notify "Wi-Fi" "Turned off"; exit 0 ;;
    *"Turn On Wi-Fi"*)
        nmcli radio wifi on; exec "$0" ;;
    *"Edit Connections"*)
        nm-connection-editor & exit 0 ;;
    *"nmtui"*)
        term=$(detect_term)
        [ -n "$term" ] && "$term" -e nmtui & exit 0 ;;
    *"Toggle Airplane Mode"*)
        if rfkill list wifi | grep -q "Soft blocked: yes"; then
            rfkill unblock wifi; notify "Airplane" "Wi-Fi unblocked"
        else
            rfkill block wifi; notify "Airplane" "Wi-Fi blocked"
        fi
        exit 0 ;;
    *"Forget Network"*)
        saved=$(nmcli -t -f NAME,TYPE connection show \
          | awk -F: '$2=="802-11-wireless"{print $1}')
        [ -z "$saved" ] && { notify "Forget" "No saved Wi-Fi profiles"; exit 0; }
        target=$(printf '%s\n' "$saved" | "${FUZZEL[@]}" --prompt "Forget> ")
        [ -z "$target" ] && exit 0
        nmcli connection delete "$target" >/dev/null 2>&1 \
          && notify "Forget" "Removed: $target" \
          || notify "Forget" "Failed: $target"
        exit 0 ;;
    *"Connection Info"*)
        dev=$(nmcli -t -f DEVICE,STATE device | awk -F: '$2=="connected"{print $1; exit}')
        info=$(nmcli -f IP4.ADDRESS,IP4.GATEWAY,IP4.DNS,GENERAL.HWADDR device show "$dev" 2>/dev/null \
               | grep -v '^$')
        sig=$(nmcli -t -f IN-USE,SIGNAL dev wifi | awk -F: '$1=="*"{print $2}')
        printf 'SSID: %s\nSignal: %s%%\n\n%s\n\n[Esc to close]' \
               "$current_ssid" "$sig" "$info" \
          | "${FUZZEL[@]}" --prompt "Info> " >/dev/null
        exit 0 ;;
esac

# ---------- connect to chosen network ----------
ssid=$(echo "$chosen" | sed -E 's/ \*$//; s/ 󰌾//; s/  \[[0-9]+%\]$//')

if nmcli -t -f NAME connection show | grep -Fxq "$ssid"; then
    nmcli connection up "$ssid" >/dev/null 2>&1 \
      && notify "Connected" "$ssid" \
      || notify "Failed" "Could not connect to $ssid"
else
    password=$("${FUZZEL[@]}" --password --prompt "Password for ${ssid}> ")
    [ -z "$password" ] && exit 0
    nmcli device wifi connect "$ssid" password "$password" >/dev/null 2>&1 \
      && notify "Connected" "$ssid" \
      || notify "Failed" "Wrong password or out of range"
fi
