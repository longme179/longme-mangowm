#!/usr/bin/env bash
# ~/.config/mango/waybar/scripts/weather.sh
# Waybar custom weather module — wttr.in JSON API, single request, disk cache.
# Output: {"text": "...", "tooltip": "<pango markup>", "class": "..."}
#
# Requirements : curl, jq, GNU date (coreutils)
# Notes        : change CITY below (leave empty for IP geolocation).
#                CACHE_TTL should match "interval" in waybar config.
#                Units are metric (°C, km/h, mm) — swap temp_C/windspeedKmph
#                for temp_F/windspeedMiles in the parse section if needed.

set -o pipefail
export LC_ALL=C

CITY="Ho Chi Minh City"
DISPLAY_NAME="Ho Chi Minh City"   # fixed display name (wttr.in often garbles non-ASCII place names); leave empty to use API name
CACHE_TTL=1800
TIMEOUT=8
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/waybar"
CACHE="$CACHE_DIR/weather.json"
ACCENT="#ebbcba"   # rose — same accent as the waybar theme
DIM="#908caa"

mkdir -p "$CACHE_DIR"

# ---------- helpers ----------
xml_escape() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }

# Undo the double encoding (UTF-8 misread as Latin-1) wttr.in sometimes returns;
# if the string is already correct, keep it unchanged
fix_encoding() {
    local orig fixed
    orig=$(cat)
    fixed=$(printf '%s' "$orig" | iconv -f UTF-8 -t LATIN1 2>/dev/null) || { printf '%s' "$orig"; return; }
    if printf '%s' "$fixed" | iconv -f UTF-8 -t UTF-8 >/dev/null 2>&1; then
        printf '%s' "$fixed"
    else
        printf '%s' "$orig"
    fi
}
weather_icon() {  # $1 = WMO weather code, $2 = 1 if night
    case "$1" in
        113) if [[ $2 == 1 ]]; then printf '󰖔'; else printf '󰖙'; fi ;;
        116) if [[ $2 == 1 ]]; then printf '󰼱'; else printf '󰖕'; fi ;;
        119|122)             printf '󰖐' ;;
        143|248|260)         printf '󰖑' ;;
        176|263|266|281|284|293|296|299|302|305|308|353|356)
                             printf '󰖗' ;;
        359)                 printf '󰖖' ;;
        179|227|230|320|323|326|329|332|335|368|371)
                             printf '󰖘' ;;
        338)                 printf '󰼴' ;;
        182|185|350|362|365|374|377)
                             printf '󰼳' ;;
        200|386|389|392|395) printf '󰖓' ;;
        *)                   printf '󰖐' ;;
    esac
}

uv_label() {
    local uv=${1%%.*}
    [[ $uv =~ ^[0-9]+$ ]] || { echo "—"; return; }
    if   (( uv <= 2 ));  then echo "Low"
    elif (( uv <= 5 ));  then echo "Moderate"
    elif (( uv <= 7 ));  then echo "High"
    elif (( uv <= 10 )); then echo "Very High"
    else                      echo "Extreme"
    fi
}

moon_icon() {
    case "$1" in
        "New Moon")        printf '󰽤' ;;
        "Waxing Crescent") printf '󰽥' ;;
        "First Quarter")   printf '󰽦' ;;
        "Waxing Gibbous")  printf '󰽧' ;;
        "Full Moon")       printf '󰽨' ;;
        "Waning Gibbous")  printf '󰽩' ;;
        "Last Quarter")    printf '󰽪' ;;
        "Waning Crescent") printf '󰽫' ;;
        *)                 printf '󰽨' ;;
    esac
}

# ---------- fallback for systems without jq: one-line English summary ----------
if ! command -v jq >/dev/null 2>&1; then
    W=$(curl -s --max-time "$TIMEOUT" \
        "wttr.in/${CITY// /+}?format=%c+%t+(feels+%f),+wind+%w,+humidity+%h")
    [[ -z "$W" ]] && W="󰖐 --°  Weather unavailable"
    W=$(printf '%s' "$W" | sed 's/"/\\"/g')
    printf '{"text": "%s", "tooltip": "%s"}\n' "$W" "$W"
    exit 0
fi

# ---------- fetch with cache (protects against wttr.in rate limits) ----------
now=$(date +%s)
mtime=$(stat -c %Y "$CACHE" 2>/dev/null || echo 0)
stale=0
if (( now - mtime > CACHE_TTL )); then
    tmp="$CACHE.tmp.$$"
    if curl -sf --max-time "$TIMEOUT" "https://wttr.in/${CITY// /+}?format=j1" -o "$tmp"; then
        mv "$tmp" "$CACHE"
    else
        rm -f "$tmp"
    fi
    (( $(stat -c %Y "$CACHE" 2>/dev/null || echo 0) == mtime )) && stale=1
fi

if [[ ! -s "$CACHE" ]] || ! jq -e '.current_condition[0]' "$CACHE" >/dev/null 2>&1; then
    printf '{"text": "󰖐 --°", "tooltip": "Weather unavailable — check network or wttr.in"}\n'
    exit 0
fi

# ---------- parse ----------
get() { jq -r "($1) // \"—\"" "$CACHE"; }
CUR='.current_condition[0]'
TODAY='.weather[0]'

TEMP=$(get "$CUR.temp_C")
FEELS=$(get "$CUR.FeelsLikeC")
DESC=$(get "$CUR.weatherDesc[0].value" | fix_encoding | xml_escape)CODE=$(get "$CUR.weatherCode")
HUM=$(get "$CUR.humidity")
WIND=$(get "$CUR.windspeedKmph")
WDIR=$(get "$CUR.winddir16Point")
PRECIP=$(get "$CUR.precipMM")
UV=$(get "$CUR.uvIndex")
VIS=$(get "$CUR.visibility")
PRES=$(get "$CUR.pressure")
OBS=$(get "$CUR.localObsDateTime // ($CUR.observation_time + \" UTC\")" | xml_escape)
AREA=$(get '.nearest_area[0].areaName[0].value' | fix_encoding | xml_escape)
COUNTRY=$(get '.nearest_area[0].country[0].value' | fix_encoding | xml_escape)
# use the fixed name when DISPLAY_NAME is set — no more garbled place names
[[ -n "$DISPLAY_NAME" ]] && AREA=$(printf '%s' "$DISPLAY_NAME" | xml_escape)
TMIN=$(get "$TODAY.mintempC")
TMAX=$(get "$TODAY.maxtempC")
SUNRISE=$(get "$TODAY.astronomy[0].sunrise")
SUNSET=$(get "$TODAY.astronomy[0].sunset")
MOON=$(get "$TODAY.astronomy[0].moon_phase" | xml_escape)
MOONILL=$(get "$TODAY.astronomy[0].moon_illumination")
RAIN_MAX=$(get "[$TODAY.hourly[].chanceofrain | tonumber] | max // 0")

# day/night flag from local sunrise/sunset (fallback: 06:00–18:00)
night=0
NOW_HM=$(date +%H%M)
SR=$(date -d "$SUNRISE" +%H%M 2>/dev/null || echo 0600)
SS=$(date -d "$SUNSET"  +%H%M 2>/dev/null || echo 1800)
(( 10#$NOW_HM < 10#$SR || 10#$NOW_HM >= 10#$SS )) && night=1

# CSS class by weather group
case "$CODE" in
    113) CLASS="clear" ;;
    116) CLASS="partly" ;;
    119|122|143|248|260) CLASS="cloudy" ;;
    176|263|266|281|284|293|296|299|302|305|308|353|356|359) CLASS="rain" ;;
    179|227|230|320|323|326|329|332|335|338|368|371) CLASS="snow" ;;
    182|185|350|362|365|374|377) CLASS="sleet" ;;
    200|386|389|392|395) CLASS="thunder" ;;
    *) CLASS="cloudy" ;;
esac

# next 4 three-hour slots (rolls over to tomorrow after 21:00)
SLOT=$(( 10#$(date +%H) / 3 + 1 ))
HOURLY=$(jq -r --argjson s "$SLOT" '
    def pad: tostring | if length < 2 then "0" + . else . end;
    [ (.weather[0].hourly[$s:8][]?, .weather[1].hourly[]?) ] | .[0:4][] |
    "\(.time | tonumber / 100 | floor | pad):00|\(.tempC)|\(.weatherCode)|\(.chanceofrain)"
' "$CACHE")

HOURS=""
while IFS='|' read -r t temp code rain; do
    [[ -z "$t" ]] && continue
    h=${t%%:*}; hn=0
    (( 10#$h < 6 || 10#$h >= 18 )) && hn=1
    HOURS+="${t}  $(weather_icon "$code" "$hn")  ${temp}°  󰖖 ${rain}%"$'\n'
done <<< "$HOURLY"
HOURS=${HOURS%$'\n'}

# ---------- build output ----------
ICON=$(weather_icon "$CODE" "$night")
TEXT="${ICON} ${TEMP}°"

STALE_NOTE=""
(( stale )) && STALE_NOTE="  <span color='${DIM}'>(cached)</span>"

TOOLTIP="<b><big>${ICON}  ${TEMP}°C — ${DESC}</big></b>
<span color='${DIM}'>${AREA}, ${COUNTRY} · obs ${OBS}${STALE_NOTE}</span>

Feels like <b>${FEELS}°C</b> · Humidity <b>${HUM}%</b> · Wind <b>${WIND} km/h ${WDIR}</b>
UV <b>${UV}</b> ($(uv_label "$UV")) · Precip ${PRECIP} mm (today max ${RAIN_MAX}%) · Vis ${VIS} km · ${PRES} hPa

<span color='${ACCENT}'><b>Today ${TMIN}° / ${TMAX}°</b></span>   󰖟 ${SUNRISE} → 󰖛 ${SUNSET}   $(moon_icon "$MOON") ${MOON} (${MOONILL}%)

<span color='${ACCENT}'><b>Next hours</b></span>
<tt>${HOURS}</tt>"

jq -cn --arg text "$TEXT" --arg tooltip "$TOOLTIP" --arg class "$CLASS" \
    '{text: $text, tooltip: $tooltip, class: $class}'
