#!/usr/bin/env bash
#
# wifi-manager — A complete WiFi management TUI for Wayland
#                 (mangowm + waybar + fuzzel + NetworkManager)
# ============================================================================
#
# PURPOSE
#   Provides a full network-applet experience (scan, connect, disconnect,
#   forget, status, hotspot, hidden networks, radio toggle, advanced)
#   driven by fuzzel and backed entirely by NetworkManager (nmcli).
#   Designed to be launched from a mangowm keybind or a waybar custom module.
#
# DEPENDENCIES (Arch / CachyOS package names)
#   Required:
#     - networkmanager         # provides nmcli + the NetworkManager daemon
#     - fuzzel                 # Wayland-native dmenu / launcher
#   Recommended:
#     - libnotify              # notify-send (desktop notifications)
#     - jq                     # only needed if you hack on JSON parsing
#     - ttf-nerd-fonts-symbols # crisp 1-cell icon glyphs (recommended)
#
#   Install everything with:
#       sudo pacman -S --needed networkmanager fuzzel libnotify jq ttf-nerd-fonts-symbols
#
# RECOMMENDED KEYBIND (mangowm)
#   In your mangowm config:
#       bindsym Mod4+Shift+w exec /path/to/wifi-manager
#
# RECOMMENDED WAYBAR MODULE
#   "custom/wifi": {
#     "exec": "~/.config/waybar/scripts/wifi-status.sh",
#     "exec-on-click": "/path/to/wifi-manager",
#     "interval": 5,
#     "tooltip": true,
#     "return-on-click": 1
#   }
#   See the wifi-status.sh snippet in the README below the script.
#
# USAGE
#   wifi-manager                # interactive main menu (default)
#   wifi-manager --status       # print current connection to stdout, exit
#   wifi-manager --toggle       # toggle WiFi radio, exit
#   wifi-manager --rescan       # trigger a scan, exit
#   wifi-manager --help         # show this header
#   wifi-manager --version
#
# EXIT CODES
#   0  success / user dismissed a menu
#   1  generic runtime failure
#   2  missing dependency
#   3  NetworkManager not running / not accessible
#   4  no WiFi adapter available
#   5  user cancelled an interactive prompt
# ============================================================================

set -euo pipefail

# -----------------------------------------------------------------------------
# Configuration & constants
# -----------------------------------------------------------------------------
readonly SCRIPT_NAME="wifi-manager"
readonly VERSION="1.1.0"

# Override via env if desired
FUZZEL_LINES="${FUZZEL_LINES:-22}"
FUZZEL_WIDTH="${FUZZEL_WIDTH:-72}"
FUZZEL_FONT="${FUZZEL_FONT:-}"               # e.g. "JetBrainsMono Nerd Font:size=11"

# Column widths (in characters) for tabular displays
readonly COL_SSID=24          # max SSID width in scan list
readonly COL_LABEL=12         # label column in status block
readonly COL_NAME=28          # connection name in saved list

# Icons — Nerd Font PUA glyphs + Unicode symbols, all 1-cell wide so that
# labels align perfectly in any monospace font. Override via env if desired.
ICON_WIFI="${ICON_WIFI:-}"
ICON_WIFI_OFF="${ICON_WIFI_OFF:-󰖪}"
ICON_LOCK="${ICON_LOCK:-󱚿}"
ICON_OPEN="${ICON_OPEN:-󱛀}"
ICON_REFRESH="${ICON_REFRESH:-}"
ICON_GEAR="${ICON_GEAR:-}"
ICON_INFO="${ICON_INFO:-}"
ICON_TRASH="${ICON_TRASH:-}"
ICON_POWER="${ICON_POWER:-}"
ICON_HOTSPOT="${ICON_HOTSPOT:-󰜕}"
ICON_HIDDEN="${ICON_HIDDEN:-}"
ICON_LOGS="${ICON_LOGS:-}"
ICON_BACK="${ICON_BACK:-}"
ICON_QUIT="${ICON_QUIT:-}"
ICON_STAR="${ICON_STAR:-}"
ICON_CHECK="${ICON_CHECK:-}"
ICON_DISCONNECT="${ICON_DISCONNECT:-}"
ICON_DEVICE="${ICON_DEVICE:-}"
ICON_SCAN="${ICON_SCAN:-}"
ICON_STATUS="${ICON_STATUS:-}"
ICON_ADVANCED="${ICON_ADVANCED:-}"
ICON_TOGGLE="${ICON_TOGGLE:-}"

# Workspace + log file
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/${SCRIPT_NAME}.XXXXXX")"
trap 'rm -rf "$WORKDIR"' EXIT

LOGFILE="${LOGFILE:-${XDG_STATE_HOME:-$HOME/.local/state}/${SCRIPT_NAME}/${SCRIPT_NAME}.log}"
mkdir -p "$(dirname "$LOGFILE")" 2>/dev/null || true

# -----------------------------------------------------------------------------
# String formatting helpers
# -----------------------------------------------------------------------------
# All helpers operate on character count (not visual width). With Nerd Font
# 1-cell icons and a monospace fuzzel font, this gives perfect alignment.

pad_right() {
    # pad_right STRING WIDTH → STRING padded with spaces to at least WIDTH chars
    local str="$1" width="$2"
    local len=${#str}
    if (( len >= width )); then
        printf '%s' "$str"
    else
        printf '%s%*s' "$str" $((width - len)) ''
    fi
}

truncate_str() {
    # truncate_str STRING MAX → STRING truncated to MAX chars with '…' suffix
    local str="$1" max="$2"
    if (( ${#str} > max )); then
        printf '%s…' "${str:0:$((max - 1))}"
    else
        printf '%s' "$str"
    fi
}

fmt_item() {
    # fmt_item ICON LABEL → "ICON  LABEL" (icon + 2 spaces + label)
    # All icons are 1-cell, so labels align at column 4.
    printf '%s  %s' "$1" "$2"
}

fmt_kv() {
    # fmt_kv LABEL VALUE → "  LABEL<padded> :  VALUE"
    # Used in status block for aligned key-value display.
    printf '  %s :  %s' "$(pad_right "$1" "$COL_LABEL")" "$2"
}

separator() {
    # separator TITLE → "─── TITLE ────────────────────────────"
    local title="$1"
    local line="──────────────────────────────────────────────────────────────────────"
    if [[ -z "$title" ]]; then
        printf '%s' "$line"
    else
        printf '─── %s %s' "$title" "${line:0:40}"
    fi
}

# -----------------------------------------------------------------------------
# Logging & notifications
# -----------------------------------------------------------------------------
log() {
    local level="$1"; shift
    local ts
    ts="$(date +'%Y-%m-%d %H:%M:%S')"
    printf '[%s] [%s] %s\n' "$ts" "$level" "$*" >>"$LOGFILE" 2>/dev/null || true
}
log_info()  { log INFO  "$@"; }
log_warn()  { log WARN  "$@"; }
log_error() { log ERROR "$@"; }

notify() {
    # notify SUMMARY [BODY]
    local summary="$1"; local body="${2:-}"
    if command -v notify-send >/dev/null 2>&1; then
        if [[ -n "$body" ]]; then
            notify-send -a "$SCRIPT_NAME" -u normal "$summary" "$body" >/dev/null 2>&1 &
        else
            notify-send -a "$SCRIPT_NAME" -u normal "$summary" >/dev/null 2>&1 &
        fi
    fi
    if [[ -n "$body" ]]; then
        printf '%s: %s\n' "$summary" "$body" >&2
    else
        printf '%s\n' "$summary" >&2
    fi
}

die() {
    # die EXIT_CODE MESSAGE...
    local code="$1"; shift
    log_error "FATAL: $*"
    notify "Error" "$*"
    exit "$code"
}

# -----------------------------------------------------------------------------
# Dependency & environment checks
# -----------------------------------------------------------------------------
check_deps() {
    local missing=()
    command -v nmcli >/dev/null 2>&1 || missing+=("networkmanager")
    command -v fuzzel >/dev/null 2>&1 || missing+=("fuzzel")
    if [[ ${#missing[@]} -gt 0 ]]; then
        cat >&2 <<EOF
[$SCRIPT_NAME] Missing required dependencies: ${missing[*]}

Install on CachyOS / Arch with:
    sudo pacman -S --needed ${missing[*]}
EOF
        exit 2
    fi
}

ensure_nm_running() {
    if ! nmcli general status >/dev/null 2>&1; then
        notify "NetworkManager not running" \
            "Start it with: sudo systemctl start NetworkManager"
        die 3 "NetworkManager is not running or not accessible via nmcli"
    fi
}

# -----------------------------------------------------------------------------
# WiFi device detection
# -----------------------------------------------------------------------------
get_wifi_device() {
    local devs dev name state
    mapfile -t devs < <(nmcli -t -f DEVICE,TYPE,STATE device 2>/dev/null \
                        | awk -F: '$2=="wifi" {print $1":"$3}')

    if [[ ${#devs[@]} -eq 0 ]]; then
        return 4
    fi

    for dev in "${devs[@]}"; do
        name="${dev%%:*}"
        [[ "$name" == "wlan0" ]] && { echo "$name"; return 0; }
    done

    for dev in "${devs[@]}"; do
        name="${dev%%:*}"
        state="${dev##*:}"
        if [[ "$state" == "connected" || "$state" == "connected (externally)" ]]; then
            echo "$name"; return 0
        fi
    done

    echo "${devs[0]%%:*}"
}

require_wifi_device() {
    local dev
    dev="$(get_wifi_device || true)"
    if [[ -z "$dev" ]]; then
        notify "No WiFi adapter" "No wifi device known to NetworkManager"
        die 4 "No WiFi adapter available"
    fi
    echo "$dev"
}

check_rfkill() {
    if command -v rfkill >/dev/null 2>&1; then
        if rfkill list wifi 2>/dev/null | grep -q "Soft blocked: yes"; then
            notify "WiFi soft-blocked" "Run: sudo rfkill unblock wifi"
            return 1
        fi
        if rfkill list wifi 2>/dev/null | grep -q "Hard blocked: yes"; then
            notify "WiFi hard-blocked" "Check the laptop hardware WiFi switch / Fn combo"
            return 1
        fi
    fi
    return 0
}

wifi_is_enabled() {
    [[ "$(nmcli -t -f WIFI radio wifi 2>/dev/null || echo disabled)" == "enabled" ]]
}

get_current_ssid() {
    local dev="$1"
    nmcli -t --escape yes -f ACTIVE,SSID device wifi list ifname "$dev" --rescan no 2>/dev/null \
        | awk -F: '$1=="*" {print $2; exit}' \
        | sed 's/\\3a/:/g; s/\\\\/\\/g'
}

# -----------------------------------------------------------------------------
# Menu helpers (fuzzel with graceful fallback)
# -----------------------------------------------------------------------------
menu_pick() {
    # menu_pick PROMPT LINE1 LINE2 ...
    local prompt="$1"; shift
    if [[ $# -eq 0 ]]; then
        return 5
    fi

    local choice
    if command -v fuzzel >/dev/null 2>&1; then
        local args=(--dmenu --prompt "$prompt" --lines "$FUZZEL_LINES" --width "$FUZZEL_WIDTH")
        [[ -n "$FUZZEL_FONT" ]] && args+=(--font "$FUZZEL_FONT")
        if ! choice="$(printf '%s\n' "$@" | fuzzel "${args[@]}" 2>/dev/null)"; then
            return 5
        fi
    elif command -v rofi >/dev/null 2>&1; then
        if ! choice="$(printf '%s\n' "$@" | rofi -dmenu -p "$prompt" -no-custom 2>/dev/null)"; then
            return 5
        fi
    else
        printf '%s\n' "$@" | cat -n >&2
        printf '%s' "$prompt (number, 0 to cancel): " >&2
        local n
        read -r n </dev/tty || return 5
        if [[ "$n" =~ ^[0-9]+$ ]] && (( n >= 1 && n <= $# )); then
            choice="$(printf '%s\n' "$@" | sed -n "${n}p")"
        else
            return 5
        fi
    fi

    [[ -z "$choice" ]] && return 5
    printf '%s' "$choice"
}

prompt_password() {
    local prompt="$1"
    local pw
    if command -v fuzzel >/dev/null 2>&1; then
        if ! pw="$(fuzzel --password --prompt "$prompt" --lines 1 2>/dev/null)"; then
            return 5
        fi
    elif command -v rofi >/dev/null 2>&1; then
        if ! pw="$(rofi -dmenu -password -p "$prompt" 2>/dev/null)"; then
            return 5
        fi
    else
        printf '%s' "$prompt" >&2
        read -rs pw </dev/tty || return 5
        printf '\n' >&2
    fi
    printf '%s' "$pw"
}

prompt_text() {
    local prompt="$1"; shift
    local text
    if command -v fuzzel >/dev/null 2>&1; then
        if [[ $# -gt 0 ]]; then
            if ! text="$(printf '%s\n' "$@" | fuzzel --dmenu --prompt "$prompt" \
                          --lines "$FUZZEL_LINES" --width "$FUZZEL_WIDTH" 2>/dev/null)"; then
                return 5
            fi
        else
            if ! text="$(printf '\n' | fuzzel --dmenu --prompt "$prompt" \
                          --lines 1 --width "$FUZZEL_WIDTH" 2>/dev/null)"; then
                return 5
            fi
        fi
    elif command -v rofi >/dev/null 2>&1; then
        if ! text="$(printf '%s\n' "$@" | rofi -dmenu -p "$prompt" 2>/dev/null)"; then
            return 5
        fi
    else
        printf '%s' "$prompt" >&2
        read -r text </dev/tty || return 5
    fi
    printf '%s' "$text"
}

show_text() {
    # show_text TITLE BODY — display a read-only blob of text via fuzzel.
    local title="$1"; local body="$2"
    if command -v fuzzel >/dev/null 2>&1; then
        printf '%s\n' "$body" | fuzzel --dmenu \
            --prompt "${title}  (Esc to close): " \
            --lines "$FUZZEL_LINES" --width "$FUZZEL_WIDTH" 2>/dev/null || true
    else
        printf '=== %s ===\n%s\n' "$title" "$body"
    fi
}

# -----------------------------------------------------------------------------
# Signal-strength rendering
# -----------------------------------------------------------------------------
signal_bars() {
    # signal_bars PERCENT → "[████░]" (5-cell bar with brackets)
    local pct="$1"
    local fill=$(( (pct * 5 + 50) / 100 ))
    (( fill > 5 )) && fill=5
    (( fill < 0 )) && fill=0
    local out="[" i
    for ((i=0; i<fill; i++)); do out+="█"; done
    for ((i=fill; i<5; i++)); do out+="░"; done
    out+="]"
    printf '%s' "$out"
}

# -----------------------------------------------------------------------------
# WiFi actions
# -----------------------------------------------------------------------------
do_rescan() {
    local dev="$1"
    if ! nmcli device wifi rescan ifname "$dev" 2>>"$LOGFILE"; then
        log_warn "rescan on $dev failed (likely cooldown) - using cached results"
        return 0
    fi
    log_info "triggered rescan on $dev"
}

do_scan_and_connect() {
    local dev="$1"

    if ! wifi_is_enabled; then
        notify "WiFi is off" "Enable it from the main menu first"
        return 1
    fi

    do_rescan "$dev"

    local raw
    if ! raw="$(nmcli -t --escape yes -f IN-USE,SSID,SIGNAL,SECURITY \
                  device wifi list ifname "$dev" --rescan no 2>>"$LOGFILE")"; then
        notify "Scan failed" "nmcli could not list networks"
        return 1
    fi

    local displays=() ssids=() securities=() in_uses=()
    local in_use ssid signal security sec_icon marker bars display ssid_col

    while IFS=: read -r in_use ssid signal security; do
        ssid="${ssid//\\3a/:}"
        ssid="${ssid//\\\\/\\}"
        security="${security//\\3a/:}"
        security="${security//\\\\/\\}"

        [[ -z "$ssid" || "$ssid" == "--" ]] && continue

        if [[ -n "$security" && "$security" != "--" ]]; then
            sec_icon="$ICON_LOCK"
        else
            sec_icon="$ICON_OPEN"
            security="open"
        fi

        if [[ "$in_use" == "*" ]]; then
            marker="$ICON_STAR"
        else
            marker=" "
        fi

        bars="$(signal_bars "${signal:-0}")"
        ssid_col="$(pad_right "$(truncate_str "$ssid" "$COL_SSID")" "$COL_SSID")"

        # Column layout (all 1-cell chars):
        #   marker  icon  signal(3r)%  bars  ssid(24)  security
        printf -v display '%s %s  %3s%% %s  %s  %s' \
            "$marker" "$sec_icon" "${signal:-0}" "$bars" "$ssid_col" "$security"

        displays+=("$display")
        ssids+=("$ssid")
        securities+=("$security")
        in_uses+=("$in_use")
    done <<< "$raw"

    if [[ ${#displays[@]} -eq 0 ]]; then
        notify "No networks found" "Try Refresh, then Scan & Connect again"
        return 1
    fi

    local choice
    if ! choice="$(menu_pick "$(fmt_item "$ICON_SCAN" 'Select network:') " "${displays[@]}")"; then
        return 5
    fi

    local idx=-1 i
    for i in "${!displays[@]}"; do
        if [[ "${displays[$i]}" == "$choice" ]]; then
            idx="$i"; break
        fi
    done
    if [[ "$idx" -lt 0 ]]; then
        log_error "could not match selected line: $choice"
        return 1
    fi

    local chosen_ssid="${ssids[$idx]}"
    local chosen_sec="${securities[$idx]}"
    local already_connected=0
    [[ "${in_uses[$idx]}" == "*" ]] && already_connected=1

    if [[ "$already_connected" -eq 1 ]]; then
        local ans
        if ! ans="$(menu_pick "Already on '${chosen_ssid}'. Reconnect? " \
                       "$(fmt_item "$ICON_CHECK" 'Yes, reconnect')" \
                       "$(fmt_item "$ICON_BACK" 'No, go back')")"; then
            return 0
        fi
        [[ "$ans" == *"No"* ]] && return 0
    fi

    if [[ -z "$chosen_sec" || "$chosen_sec" == "open" ]]; then
        if nmcli device wifi connect "$chosen_ssid" ifname "$dev" 2>>"$LOGFILE"; then
            notify "Connected" "$chosen_ssid"
            log_info "connected to open network: $chosen_ssid"
        else
            notify "Connection failed" "Could not connect to '$chosen_ssid'"
            return 1
        fi
    else
        local password
        if ! password="$(prompt_password "Password for '${chosen_ssid}': ")"; then
            return 5
        fi
        if [[ -z "$password" ]]; then
            notify "Cancelled" "No password entered"
            return 5
        fi
        if ! nmcli device wifi connect "$chosen_ssid" ifname "$dev" \
                password "$password" 2>>"$LOGFILE"; then
            notify "Connection failed" "Wrong password or network unavailable: '$chosen_ssid'"
            log_error "failed to connect to '$chosen_ssid'"
            return 1
        fi
        notify "Connected" "$chosen_ssid"
        log_info "connected to secured network: $chosen_ssid"
    fi
}

do_status() {
    # Echoes a formatted, aligned status block to stdout.
    local dev="$1"

    if ! wifi_is_enabled; then
        cat <<EOF
$(separator "")
  $(fmt_kv "Status"   "${ICON_WIFI_OFF}  WiFi radio is OFF")
  $(fmt_kv "Device"   "$dev")
  $(fmt_kv "Hint"     "Run '$SCRIPT_NAME --toggle' to enable")
EOF
        return 0
    fi

    local raw
    raw="$(nmcli -t --escape yes -f ACTIVE,SSID,SIGNAL,SECURITY,FREQ,CHAN,RATE,BSSID \
            device wifi list ifname "$dev" --rescan no 2>/dev/null \
            | awk -F: '$1=="*" {print; exit}')"

    if [[ -z "$raw" ]]; then
        cat <<EOF
$(separator "")
  $(fmt_kv "Status"   "${ICON_WIFI_OFF}  Not connected")
  $(fmt_kv "Device"   "$dev")
  $(fmt_kv "State"    "disconnected")
EOF
        return 0
    fi

    local a_ssid a_signal a_sec a_freq a_chan a_rate a_bssid
    IFS=: read -r _ a_ssid a_signal a_sec a_freq a_chan a_rate a_bssid <<< "$raw"
    a_ssid="${a_ssid//\\3a/:}"; a_ssid="${a_ssid//\\\\/\\}"
    a_sec="${a_sec//\\3a/:}";   a_sec="${a_sec//\\\\/\\}"
    [[ -z "$a_sec" || "$a_sec" == "--" ]] && a_sec="open"

    local ip4 gw dns
    ip4="$(nmcli -t -f IP4.ADDRESS device show "$dev" 2>/dev/null | head -1 | cut -d: -f2-)"
    gw="$(nmcli -t -f IP4.GATEWAY  device show "$dev" 2>/dev/null | head -1 | cut -d: -f2-)"
    dns="$(nmcli -t -f IP4.DNS     device show "$dev" 2>/dev/null | cut -d: -f2- | paste -sd ' ' -)"

    local signal_str="${a_signal:-0}%  $(signal_bars "${a_signal:-0}")"

    cat <<EOF
$(separator "Connection")
  $(fmt_kv "SSID"      "$a_ssid")
  $(fmt_kv "Signal"    "$signal_str")
  $(fmt_kv "Security"  "${a_sec:-none}")
  $(fmt_kv "Channel"   "${a_chan:-?}")
  $(fmt_kv "Frequency" "${a_freq:-?}")
  $(fmt_kv "Bitrate"   "${a_rate:-?}")
  $(fmt_kv "BSSID"     "${a_bssid:-?}")

$(separator "IPv4")
  $(fmt_kv "Address"   "${ip4:-none}")
  $(fmt_kv "Gateway"   "${gw:-none}")
  $(fmt_kv "DNS"       "${dns:-none}")
  $(fmt_kv "Device"    "$dev")
EOF
}

do_disconnect() {
    local dev="$1"
    local con
    con="$(nmcli -t -f GENERAL.CONNECTION device show "$dev" 2>/dev/null | cut -d: -f2-)"
    if [[ -z "$con" || "$con" == "--" ]]; then
        notify "Not connected" "Nothing to disconnect"
        return 0
    fi
    if nmcli device disconnect "$dev" 2>>"$LOGFILE"; then
        notify "Disconnected" "$con"
        log_info "disconnected from $con"
    else
        notify "Failed" "Could not disconnect $dev"
        return 1
    fi
}

do_forget() {
    local dev="$1"
    local saved=()
    local name type
    while IFS=: read -r name _ type _; do
        [[ "$type" == "wifi" ]] || continue
        [[ -z "$name" || "$name" == "--" ]] && continue
        saved+=("$name")
    done < <(nmcli -t --escape yes -f NAME,UUID,TYPE,DEVICE connection show 2>/dev/null)

    if [[ ${#saved[@]} -eq 0 ]]; then
        notify "No saved networks" "Nothing to forget"
        return 0
    fi

    local choice
    if ! choice="$(menu_pick "$(fmt_item "$ICON_TRASH" 'Forget network:') " "${saved[@]}")"; then
        return 5
    fi

    local ans
    if ! ans="$(menu_pick "Forget '${choice}'? " \
                   "$(fmt_item "$ICON_CHECK" 'Yes, forget it')" \
                   "$(fmt_item "$ICON_BACK" 'No, cancel')")"; then
        return 0
    fi
    [[ "$ans" == *"No"* ]] && return 0

    if nmcli connection delete "$choice" 2>>"$LOGFILE"; then
        notify "Forgotten" "$choice"
        log_info "deleted connection profile: $choice"
    else
        notify "Failed" "Could not delete '$choice'"
        return 1
    fi
}

do_toggle_wifi() {
    local dev="$1"
    if wifi_is_enabled; then
        nmcli radio wifi off 2>>"$LOGFILE"
        notify "WiFi off" ""
        log_info "wifi radio disabled"
    else
        nmcli radio wifi on 2>>"$LOGFILE"
        sleep 1
        notify "WiFi on" "Rescanning…"
        do_rescan "$dev"
        log_info "wifi radio enabled"
    fi
}

do_hidden_connect() {
    local dev="$1"
    local ssid
    if ! ssid="$(prompt_text "Hidden SSID: ")"; then
        return 5
    fi
    [[ -z "$ssid" ]] && { notify "Cancelled" "No SSID entered"; return 5; }

    local pass
    if ! pass="$(prompt_password "Password (leave blank if open): ")"; then
        return 5
    fi

    if [[ -n "$pass" ]]; then
        if nmcli device wifi connect "$ssid" --hidden ifname "$dev" \
                password "$pass" 2>>"$LOGFILE"; then
            notify "Connected" "$ssid (hidden, secured)"
            log_info "connected to hidden secured network: $ssid"
        else
            notify "Failed" "Could not connect to '$ssid'"
            return 1
        fi
    else
        if nmcli device wifi connect "$ssid" --hidden ifname "$dev" 2>>"$LOGFILE"; then
            notify "Connected" "$ssid (hidden, open)"
            log_info "connected to hidden open network: $ssid"
        else
            notify "Failed" "Could not connect to '$ssid'"
            return 1
        fi
    fi
}

do_hotspot() {
    local dev="$1"
    local ssid pass
    if ! ssid="$(prompt_text "Hotspot SSID: ")"; then
        return 5
    fi
    [[ -z "$ssid" ]] && { notify "Cancelled"; return 5; }

    if ! pass="$(prompt_password "Hotspot password (min 8 chars): ")"; then
        return 5
    fi
    if [[ ${#pass} -lt 8 ]]; then
        notify "Password too short" "WPA requires at least 8 characters"
        return 1
    fi

    if nmcli device wifi hotspot ifname "$dev" \
            con-name "hotspot-$ssid" ssid "$ssid" password "$pass" 2>>"$LOGFILE"; then
        notify "Hotspot started" "SSID: $ssid"
        log_info "started hotspot: $ssid on $dev"
    else
        notify "Hotspot failed" "Adapter may not support AP mode, or WiFi is off"
        return 1
    fi
}

do_show_saved() {
    local dev="$1"
    local out
    # Header + rows, all aligned by character count
    out="$(printf '  %s  %s  %s\n' \
            "$(pad_right "NAME" "$COL_NAME")" \
            "$(pad_right "AUTO" 5)" \
            "LAST USED")"
    out+="$(printf '  %s\n' "$(separator "")")"
    out+="$(nmcli -t --escape yes -f NAME,TYPE,AUTOCONNECT,TIMESTAMP-REAL \
            connection show 2>/dev/null \
            | awk -F: -v col="$COL_NAME" '$2=="wifi" {
                name=$1; ac=$3; ts=$4;
                gsub(/\\3a/, ":", name); gsub(/\\\\/, "\\", name);
                ac = (ac=="yes") ? "yes" : "no";
                if (ts=="--" || ts=="") ts="(never)";
                printf "  %-*s  %-5s  %s\n", col, name, ac, ts;
              }')"
    if [[ -z "${out#*$'\n'}" ]]; then
        out="  (no saved WiFi connections)"
    fi
    show_text "Saved WiFi connections" "$out"
}

do_show_device_details() {
    local dev="$1"
    local out
    # Reformat nmcli output into aligned key-value pairs
    out="$(nmcli -t device show "$dev" 2>/dev/null | awk -F: '{
        key=$1; val=$2;
        gsub(/^ +/, "", val); gsub(/ +$/, "", val);
        if (key=="" || val=="") next;
        printf "  %-22s :  %s\n", key, val;
    }')"
    [[ -z "$out" ]] && out="  (no details available for $dev)"
    show_text "Device details: $dev" "$out"
}

do_show_logs() {
    local out
    out="$(journalctl -u NetworkManager --no-pager -n 40 2>/dev/null || \
           echo 'journalctl not available or no permissions')"
    show_text "Recent NetworkManager logs (last 40 lines)" "$out"
}

do_restart_nm() {
    if command -v pkexec >/dev/null 2>&1; then
        if pkexec systemctl restart NetworkManager 2>>"$LOGFILE"; then
            notify "NetworkManager restarted" ""
            log_info "restarted NetworkManager via pkexec"
        else
            notify "Failed" "Could not restart NetworkManager (auth denied?)"
            return 1
        fi
    else
        notify "Restart requires privileges" \
            "Run: sudo systemctl restart NetworkManager"
        return 1
    fi
}

# -----------------------------------------------------------------------------
# Advanced submenu
# -----------------------------------------------------------------------------
do_advanced() {
    local dev="$1"
    while true; do
        local choice
        if ! choice="$(menu_pick "$(fmt_item "$ICON_ADVANCED" 'Advanced menu:') " \
            "$(fmt_item "$ICON_HIDDEN"   'Connect to hidden network…')" \
            "$(fmt_item "$ICON_HOTSPOT"  'Create hotspot…')" \
            "$(fmt_item "$ICON_REFRESH"  'Force rescan')" \
            "$(fmt_item "$ICON_INFO"     'Show saved connections')" \
            "$(fmt_item "$ICON_DEVICE"   'Show device details')" \
            "$(fmt_item "$ICON_POWER"    'Restart NetworkManager')" \
            "$(fmt_item "$ICON_LOGS"     'View NetworkManager logs')" \
            "$(fmt_item "$ICON_TOGGLE"   'Toggle WiFi radio')" \
            "$(fmt_item "$ICON_BACK"     'Back to main menu')" \
        )"; then
            return 0
        fi

        case "$choice" in
            *hidden*)             do_hidden_connect "$dev" ;;
            *hotspot*)            do_hotspot "$dev" ;;
            *Force\ rescan*)      do_rescan "$dev"; notify "Rescan triggered" "" ;;
            *saved\ connections*) do_show_saved "$dev" ;;
            *device\ details*)    do_show_device_details "$dev" ;;
            *Restart\ NetworkManager*) do_restart_nm ;;
            *NetworkManager\ logs*) do_show_logs ;;
            *Toggle\ WiFi*)       do_toggle_wifi "$dev" ;;
            *Back*)               return 0 ;;
        esac
    done
}

# -----------------------------------------------------------------------------
# Main menu
# -----------------------------------------------------------------------------
main_menu() {
    local dev
    dev="$(require_wifi_device)" || exit $?

    while true; do
        local wifi_state wifi_icon status_line current prompt
        if wifi_is_enabled; then
            wifi_state="on"
            wifi_icon="$ICON_WIFI"
            current="$(get_current_ssid "$dev")"
            if [[ -n "$current" ]]; then
                status_line="Connected: $(truncate_str "$current" 22)"
            else
                status_line="Not connected"
            fi
        else
            wifi_state="off"
            wifi_icon="$ICON_WIFI_OFF"
            status_line="WiFi off"
        fi
        prompt="$(printf '%s  WiFi Manager  [%s]: ' "$wifi_icon" "$status_line")"

        local choice
        if ! choice="$(menu_pick "$prompt" \
            "$(fmt_item "$ICON_SCAN"        'Scan & Connect…')" \
            "$(fmt_item "$ICON_STATUS"      'Current Connection Status')" \
            "$(fmt_item "$ICON_DISCONNECT"  'Disconnect')" \
            "$(fmt_item "$ICON_TRASH"       'Forget Network…')" \
            "$(fmt_item "$ICON_ADVANCED"    'Advanced…')" \
            "$(fmt_item "$ICON_TOGGLE"      "Toggle WiFi (currently: $wifi_state)")" \
            "$(fmt_item "$ICON_REFRESH"     'Refresh')" \
            "$(fmt_item "$ICON_QUIT"        'Quit')" \
        )"; then
            return 0
        fi

        case "$choice" in
            *Scan*)
                do_scan_and_connect "$dev" ;;
            *Current\ Connection\ Status*)
                show_text "Connection Status" "$(do_status "$dev")" ;;
            *Disconnect*)
                do_disconnect "$dev" ;;
            *Forget\ Network*)
                do_forget "$dev" ;;
            *Advanced*)
                do_advanced "$dev" ;;
            *Toggle\ WiFi*)
                do_toggle_wifi "$dev" ;;
            *Refresh*)
                : ;;
            *Quit*)
                return 0 ;;
        esac
    done
}

# -----------------------------------------------------------------------------
# CLI dispatch
# -----------------------------------------------------------------------------
usage() {
    sed -n '3,53p' "$0" | sed 's/^# \{0,1\}//'
}

main() {
    case "${1:-}" in
        --help|-h)
            usage
            exit 0
            ;;
        --version|-V)
            echo "$SCRIPT_NAME $VERSION"
            exit 0
            ;;
        --status)
            check_deps
            ensure_nm_running
            local dev
            dev="$(require_wifi_device)" || exit $?
            do_status "$dev"
            exit 0
            ;;
        --toggle)
            check_deps
            ensure_nm_running
            local dev
            dev="$(require_wifi_device)" || exit $?
            do_toggle_wifi "$dev"
            exit 0
            ;;
        --rescan)
            check_deps
            ensure_nm_running
            local dev
            dev="$(require_wifi_device)" || exit $?
            do_rescan "$dev"
            notify "Rescan triggered" ""
            exit 0
            ;;
        "")
            : ;;
        *)
            echo "Unknown option: $1" >&2
            echo "Try --help" >&2
            exit 1
            ;;
    esac

    check_deps
    ensure_nm_running
    check_rfkill || exit 1

    main_menu
}

main "$@"
