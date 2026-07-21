while inotifywait -e close_write ~/.config/mango/waybar; do killall -SIGUSR2 "waybar -c ~/.config/mango/waybar/config.jsonc -s ~/.config/mango/waybar/style.css >/dev/null 2>&1 &"; done
