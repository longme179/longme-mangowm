#!/bin/bash
# nightmode-toggle.sh - toggle wlsunset cho mắt mày

PID=$(pgrep -x wlsunset)

if [ -n "$PID" ]; then
    kill "$PID"
    notify-send "🌙 Night mode OFF" -t 2000
else
    wlsunset -l 10.8 -L 106.6 -t 4000 -T 4600 -g 0.85 &
    notify-send "🌙 Night mode ON" -t 2000
fi
