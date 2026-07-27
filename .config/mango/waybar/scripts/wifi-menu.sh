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
#     - networkmanager   # provides nmcli + the NetworkManager daemon
#     - fuzzel           # Wayland-native dmenu / launcher
#   Recommended:
#     - libnotify        # notify-send (desktop notifications)
#     - jq               # only needed if you hack on JSON parsing
#     - ttf-nerd-fonts-symbols  # for crisp icon glyphs (optional)
#
#   Install everything with:
#       sudo pacman -S --needed networkmanager fuzzel libnotify jq
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
readonly VERSION="1.0.0"

# Override via env if desired
FUZZEL_LINES="${FUZZEL_LINES:-20}"
FUZZEL_WIDTH="${FUZZEL_WIDTH:-70}"
FUZZEL_FONT="${FUZZEL_FONT:-}"               # e.g. "JetBrainsMono Nerd Font:size=11"

# Icons — simple Unicode glyphs that render in any font (incl. Nerd Fonts).
# Override via env to customise (e.g. plain text for minimal setups).
ICON_WIFI="${ICON_WIFI:-}"
ICON_WIFI_OFF="${ICON_WIFI_OFF:-󰖪}"
ICON_LOCK="${ICON_LOCK:-󱚿}"
ICON_OPEN="${ICON_OPEN:-󱛀}"
ICON_REFRESH="${ICON_REFRESH:-}"
ICON_GEAR="${ICON_GEAR:-⚙}"
ICON_INFO="${ICON_INFO:-ℹ}"
ICON_TRASH="${ICON_TRASH:-}"
ICON_POWER="${ICON_POWER:-⏻}"
ICON_HOTSPOT="${ICON_HOTSPOT:-󰜕}"
ICON_HIDDEN="${ICON_HIDDEN:-}"
ICON_LOGS="${ICON_LOGS:-}"
ICON_BACK="${ICON_BACK:-↩}"
ICON_QUIT="${ICON_QUIT:-✕}"
ICON_STAR="${ICON_STAR:-★}"
ICON_CHECK="${ICON_CHECK:-✓}"
ICON_DISCONNECT="${ICON_DISCONNECT:-}"

# Workspace + log file
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/${SCRIPT_NAME}.XXXXXX")"
trap 'rm -rf "$WORKDIR"' EXIT

LOGFILE="${LOGFILE:-${XDG_STATE_HOME:-$HOME/.local/state}/${SCRIPT_NAME}/${SCRIPT_NAME}.log}"
mkdir -p "$(dirname "$LOGFILE")" 2>/dev/null || true

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
    # Always also echo to stderr so waybar / terminal users see something
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
    # `nmcli general status` is the cheapest round-trip that proves the
    # daemon is reachable on the system D-Bus.
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
    # Echoes the chosen WiFi device name (e.g. wlan0).
    # Preference order: wlan0 (if UP/connected) → any connected wifi device →
    # any wifi device. Returns 4 if none exist.
    local devs dev name state
    mapfile -t devs < <(nmcli -t -f DEVICE,TYPE,STATE device 2>/dev/null \
                        | awk -F: '$2=="wifi" {print $1":"$3}')

    if [[ ${#devs[@]} -eq 0 ]]; then
        return 4
    fi

    # Prefer wlan0
    for dev in "${devs[@]}"; do
        name="${dev%%:*}"
        [[ "$name" == "wlan0" ]] && { echo "$name"; return 0; }
    done

    # Then prefer one that is connected
    for dev in "${devs[@]}"; do
        name="${dev%%:*}"
        state="${dev##*:}"
        if [[ "$state" == "connected" || "$state" == "connected (externally)" ]]; then
            echo "$name"; return 0
        fi
    done

    # Fall back to the first one (even if disconnected/unavailable)
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
    # Returns 1 (and notifies) if WiFi is soft- or hard-blocked.
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
    # get_current_ssid DEV → echoes current SSID or empty string.
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
    # Echoes the chosen line (verbatim) to stdout.
    # Returns 5 if user cancelled (Esc).
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
        # Last-resort terminal prompt
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
    # prompt_password PROMPT → echoes password (no trailing newline).
    # Returns 5 if user cancelled.
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
    # prompt_text PROMPT [SUGGESTION...] → echoes free-text entry.
    # Used for hidden SSID / hotspot SSID. Modern fuzzel returns the typed
    # query when no entry matches; we feed one blank line so the user can
    # type freely.
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
    # User dismisses with Esc; we ignore the "selection".
    local title="$1"; local body="$2"
    if command -v fuzzel >/dev/null 2>&1; then
        printf '%s\n' "$body" | fuzzel --dmenu \
            --prompt "${title} (Esc to close): " \
            --lines "$FUZZEL_LINES" --width "$FUZZEL_WIDTH" 2>/dev/null || true
    else
        printf '=== %s ===\n%s\n' "$title" "$body"
    fi
}

# -----------------------------------------------------------------------------
# Signal-strength rendering
# -----------------------------------------------------------------------------
signal_bars() {
    # signal_bars PERCENT → echoes a 5-char bracketed bar like [███░░]
    local pct="$1"
    local fill=$(( (pct * 5 + 50) / 100 ))   # round to nearest of 5
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
    # nmcli refuses to rescan more than once every ~30 s; ignore that error.
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

    # `--escape yes` escapes ':' and '\' in values so that ':' is a safe
    # field separator even for SSIDs that contain colons.
    local raw
    if ! raw="$(nmcli -t --escape yes -f IN-USE,SSID,SIGNAL,SECURITY \
                  device wifi list ifname "$dev" --rescan no 2>>"$LOGFILE")"; then
        notify "Scan failed" "nmcli could not list networks"
        return 1
    fi

    local displays=() ssids=() securities=() in_uses=()
    local in_use ssid signal security sec_icon marker bars display

    while IFS=: read -r in_use ssid signal security; do
        # Unescape nmcli's \3a (':') and \\ ('\')
        ssid="${ssid//\\3a/:}"
        ssid="${ssid//\\\\/\\}"
        security="${security//\\3a/:}"
        security="${security//\\\\/\\}"

        # Skip empty / hidden SSIDs (they show as "--" or blank)
        [[ -z "$ssid" || "$ssid" == "--" ]] && continue

        if [[ -n "$security" && "$security" != "--" ]]; then
            sec_icon="$ICON_LOCK"
        else
            sec_icon="$ICON_OPEN"
        fi

        if [[ "$in_use" == "*" ]]; then
            marker="$ICON_STAR"
        else
            marker=" "
        fi

        bars="$(signal_bars "${signal:-0}")"

        # Format: marker icon pct% bars  SSID  (security)
        printf -v display '%s %s %3s%% %s  %-26s  %s' \
            "$marker" "$sec_icon" "${signal:-0}" "$bars" "$ssid" "$security"

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
    if ! choice="$(menu_pick "Connect to: " "${displays[@]}")"; then
        return 5
    fi

    # Match the chosen display line back to its SSID
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

    # If already connected, ask whether to reconnect
    if [[ "$already_connected" -eq 1 ]]; then
        local ans
        if ! ans="$(menu_pick "Already connected to '$chosen_ssid'. Reconnect?" \
                       "Yes, reconnect" "No, go back")"; then
            return 0
        fi
        [[ "$ans" == "No"* ]] && return 0
    fi

    # Open vs secured
    if [[ -z "$chosen_sec" || "$chosen_sec" == "--" ]]; then
        if nmcli device wifi connect "$chosen_ssid" ifname "$dev" 2>>"$LOGFILE"; then
            notify "Connected" "$chosen_ssid"
            log_info "connected to open network: $chosen_ssid"
        else
            notify "Connection failed" "Could not connect to '$chosen_ssid'"
            return 1
        fi
    else
        local password
        if ! password="$(prompt_password "Password for '$chosen_ssid': ")"; then
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
    # Echoes a formatted status block to stdout.
    local dev="$1"

    if ! wifi_is_enabled; then
        cat <<EOF
$ICON_WIFI_OFF  WiFi radio is OFF

Device: $dev
Run '$SCRIPT_NAME --toggle' to enable.
EOF
        return 0
    fi

    local raw
    raw="$(nmcli -t --escape yes -f ACTIVE,SSID,SIGNAL,SECURITY,FREQ,CHAN,RATE,BSSID \
            device wifi list ifname "$dev" --rescan no 2>/dev/null \
            | awk -F: '$1=="*" {print; exit}')"

    if [[ -z "$raw" ]]; then
        cat <<EOF
$ICON_WIFI_OFF  Not connected

Device: $dev
State:  disconnected
EOF
        return 0
    fi

    local a_ssid a_signal a_sec a_freq a_chan a_rate a_bssid
    IFS=: read -r _ a_ssid a_signal a_sec a_freq a_chan a_rate a_bssid <<< "$raw"
    a_ssid="${a_ssid//\\3a/:}"; a_ssid="${a_ssid//\\\\/\\}"
    a_sec="${a_sec//\\3a/:}";   a_sec="${a_sec//\\\\/\\}"

    local ip4 gw dns
    ip4="$(nmcli -t -f IP4.ADDRESS device show "$dev" 2>/dev/null | head -1 | cut -d: -f2-)"
    gw="$(nmcli -t -f IP4.GATEWAY  device show "$dev" 2>/dev/null | head -1 | cut -d: -f2-)"
    dns="$(nmcli -t -f IP4.DNS     device show "$dev" 2>/dev/null | cut -d: -f2- | paste -sd ' ' -)"

    cat <<EOF
$ICON_WIFI  Connected to: $a_ssid

Signal:     ${a_signal}%  $(signal_bars "${a_signal:-0}")
Security:   ${a_sec:-none}
Channel:    ${a_chan:-?}    Frequency: ${a_freq:-?}
Bitrate:    ${a_rate:-?}
BSSID:      ${a_bssid:-?}

IPv4:       ${ip4:-none}
Gateway:    ${gw:-none}
DNS:        ${dns:-none}
Device:     $dev
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
    if ! choice="$(menu_pick "Forget network: " "${saved[@]}")"; then
        return 5
    fi

    # Confirm
    local ans
    if ! ans="$(menu_pick "Forget '$choice'?" "Yes, forget it" "No, cancel")"; then
        return 0
    fi
    [[ "$ans" == "No"* ]] && return 0

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
        # Give the radio a moment to come up
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

    # Will fail gracefully if the adapter does not support AP mode.
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
    out="$(nmcli -t --escape yes -f NAME,TYPE,AUTOCONNECT,TIMESTAMP-REAL \
            connection show 2>/dev/null \
            | awk -F: '$2=="wifi" {
                name=$1; ac=$3; ts=$4;
                gsub(/\\3a/, ":", name); gsub(/\\\\/, "\\", name);
                printf "  %-28s  auto=%-3s  last=%s\n", name, ac, ts
              }')"
    [[ -z "$out" ]] && out="(no saved WiFi connections)"
    show_text "Saved WiFi connections" "$out"
}

do_show_device_details() {
    local dev="$1"
    local out
    out="$(nmcli device show "$dev" 2>/dev/null | sed 's/^/  /')"
    show_text "Device details: $dev" "$out"
}

do_show_logs() {
    local out
    out="$(journalctl -u NetworkManager --no-pager -n 40 2>/dev/null || \
           echo 'journalctl not available or no permissions')"
    show_text "Recent NetworkManager logs (last 40 lines)" "$out"
}

do_restart_nm() {
    # Needs root. Prefer pkexec; fall back to giving the user the command.
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
        if ! choice="$(menu_pick "$ICON_GEAR  Advanced: " \
            "$ICON_HIDDEN   Connect to hidden network…" \
            "$ICON_HOTSPOT  Create hotspot…" \
            "$ICON_REFRESH  Force rescan" \
            "$ICON_INFO     Show saved connections" \
            "$ICON_INFO     Show device details" \
            "$ICON_POWER    Restart NetworkManager" \
            "$ICON_LOGS     View recent NetworkManager logs" \
            "$ICON_WIFI     Toggle WiFi radio" \
            "$ICON_BACK     Back to main menu" \
        )"; then
            return 0
        fi

        case "$choice" in
            *hidden*)            do_hidden_connect "$dev" ;;
            *hotspot*)           do_hotspot "$dev" ;;
            *Force\ rescan*)     do_rescan "$dev"; notify "Rescan triggered" "" ;;
            *saved\ connections*) do_show_saved "$dev" ;;
            *device\ details*)   do_show_device_details "$dev" ;;
            *Restart\ NetworkManager*) do_restart_nm ;;
            *NetworkManager\ logs*) do_show_logs ;;
            *Toggle\ WiFi*)      do_toggle_wifi "$dev" ;;
            *Back*)              return 0 ;;
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
        # Build dynamic header line
        local wifi_state wifi_icon status_line current
        if wifi_is_enabled; then
            wifi_state="on"
            wifi_icon="$ICON_WIFI"
            current="$(get_current_ssid "$dev")"
            if [[ -n "$current" ]]; then
                status_line="Connected: $current"
            else
                status_line="Not connected"
            fi
        else
            wifi_state="off"
            wifi_icon="$ICON_WIFI_OFF"
            status_line="WiFi off"
        fi

        local choice
        if ! choice="$(menu_pick "$wifi_icon  WiFi Manager  [$status_line]: " \
            "$ICON_REFRESH   Scan & Connect…" \
            "$ICON_INFO      Current Connection Status" \
            "$ICON_DISCONNECT  Disconnect" \
            "$ICON_TRASH     Forget Network…" \
            "$ICON_GEAR      Advanced…" \
            "$wifi_icon      Toggle WiFi (currently: $wifi_state)" \
            "$ICON_REFRESH   Refresh" \
            "$ICON_QUIT      Quit" \
        )"; then
            return 0
        fi

        case "$choice" in
            *Scan*)
                do_scan_and_connect "$dev" ;;
            *Current\ Connection\ Status*)
                show_text "Status" "$(do_status "$dev")" ;;
            *Disconnect*)
                do_disconnect "$dev" ;;
            *Forget\ Network*)
                do_forget "$dev" ;;
            *Advanced*)
                do_advanced "$dev" ;;
            *Toggle\ WiFi*)
                do_toggle_wifi "$dev" ;;
            *Refresh*)
                : ;;   # just re-render the header
            *Quit*)
                return 0 ;;
        esac
    done
}

# -----------------------------------------------------------------------------
# CLI dispatch
# -----------------------------------------------------------------------------
usage() {
    # Print the header comment block (lines 3-53)
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
            : ;;  # interactive mode
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
