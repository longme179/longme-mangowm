function fish_greeting
    # Lấy thông tin thời gian
    set hour (date +%H)
    set time_str (date '+%H:%M')
    set date_str (date '+%A, %d/%m')
    
    # Câu chào theo giờ
    set greeting ""
    if test $hour -ge 5 -a $hour -lt 12
        set greeting "Chào buổi sáng"
        set icon ""
    else if test $hour -ge 12 -a $hour -lt 18
        set greeting "Chào buổi chiều"
        set icon ""
    else if test $hour -ge 18 -a $hour -lt 22
        set greeting "Chào buổi tối"
        set icon ""
    else
        set greeting "Chúc ngủ ngon"
        set icon ""
    end
    
    # Thông tin hệ thống nhẹ
    set uptime_str (uptime -p | sed 's/up //')
    set battery_pct (cat /sys/class/power_supply/BAT1/capacity 2>/dev/null || echo "N/A")
    set battery_status (cat /sys/class/power_supply/BAT1/status 2>/dev/null || echo "")
    
    # Icon battery
    set bat_icon ""
    if test "$battery_status" = "Charging"
        set bat_icon "󰂄"
    else if test $battery_pct -gt 80
        set bat_icon "󰁹"
    else if test $battery_pct -gt 50
        set bat_icon "󰂃"
    else if test $battery_pct -gt 20
        set bat_icon "󰂃"
    else
        set bat_icon "󰂎"
    end
    
    # Quote ngẫu nhiên (tùy chọn)
    set quotes \
        "Code is poetry." \
        "Simplicity is the ultimate sophistication." \
        "Talk is cheap. Show me the code." \
        "First, solve the problem. Then, write the code." \
        "The best way to predict the future is to create it." \
        "In theory, there's no difference between theory and practice."
    set quote $quotes[(random 1 (count $quotes))]
    
    # Render greeting với box-drawing
    echo ""
    set_color -o f9a825  # amber bold
    echo "╭───── $greeting, $USER $icon ─────"
    
    set_color normal
    set_color 00acc1  # cyan
    echo "│"
    
    set_color normal
    set_color a6adc8  # gray light
    printf "│  %s  %s\n" "" "$date_str"
    printf "│  %s  %s\n" "" "$time_str"
    
    set_color normal
    set_color 6c7086  # gray mid
    echo "│"
    printf "│  %s  Uptime: %s\n" "" "$uptime_str"
    printf "│  %s  Battery: %s %s%%\n" "" "$bat_icon" "$battery_pct"
    
    set_color normal
    set_color 6c7086
    echo "│"
    
    set_color normal
    set_color -o a6adc8  # gray light bold
    echo "│  💬 $quote"
    
    set_color normal
    set_color f9a825  # amber
    echo "╰───────────────────────────────────"
    set_color normal
    echo ""
end
