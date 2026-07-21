# Commands to run in interactive sessions can go here
if status is-interactive
    # No greeting
    # set fish_greeting

    # Use starship
    function starship_transient_prompt_func
        starship module character
    end
    if test "$TERM" != "linux"
        starship init fish | source
        enable_transience
    end

    # Colors
    if test -f ~/.local/state/quickshell/user/generated/terminal/sequences.txt
        cat ~/.local/state/quickshell/user/generated/terminal/sequences.txt
    end

    # Aliases
    # kitty doesn't clear properly so we need to do this weird printing
    alias clear "printf '\033[2J\033[3J\033[1;1H'"
    alias celar "printf '\033[2J\033[3J\033[1;1H'"
    alias claer "printf '\033[2J\033[3J\033[1;1H'"
    alias pamcan pacman
    alias q 'qs -c ii'
    if test "$TERM" != "linux"
        alias ls 'eza --icons'
    end
    if test "$TERM" = "xterm-kitty"
        alias ssh 'kitten ssh'
    end
end
if status is-login
    set -Ux GTK_IM_MODULE fcitx
    set -Ux QT_IM_MODULE fcitx
    set -Ux XMODIFIERS @im=fcitx
    set -Ux SDL_IM_MODULE fcitx
    set -Ux GLFW_IM_MODULE ibus
end





























######################################################################################

function ff
    ~/.local/bin/fetch-art.sh $argv
end

function ff-ascii
    # Liệt kê hoặc dùng ASCII art
    if test (count $argv) -eq 0
        echo "📜 ASCII arts có sẵn:"
        for f in ~/.config/fastfetch/ascii/*.txt
            set name (basename $f .txt)
            echo "  - $name"
        end
        echo ""
        echo "Cách dùng: ff-ascii <tên>"
    else
        ~/.local/bin/fetch-art.sh --ascii $argv[1]
    end
end

function ff-pfp
    ~/.local/bin/fetch-art.sh --pfp $argv
end

function ff-art-add
    # Tạo ASCII art mới từ figlet
    if test (count $argv) -lt 2
        echo "Cách dùng: ff-art-add <tên> <text>"
        echo "Ví dụ: ff-art-add hacker 'HACKER'"
        return 1
    end
    set name $argv[1]
    set text $argv[2..-1]

    if not command -v figlet >/dev/null
        echo "❌ Cần cài figlet: sudo pacman -S figlet"
        return 1
    end

    figlet -f slant "$text" > ~/.config/fastfetch/ascii/$name.txt
    echo "✅ Đã tạo: $name"
    cat ~/.config/fastfetch/ascii/$name.txt
end



# ═══════════════════════════════════════════
# CUSTOM FISH GREETING + ASCII ART
# ═══════════════════════════════════════════
function fish_greeting
    # ── Thông tin thời gian ──
    set -l hour (date +%H)
    set -l time_str (date '+%H:%M')
    set -l date_str (date '+%A, %d/%m/%Y')

    # ── Chào theo giờ ──
    set -l greeting "Chào"
    set -l icon "🐟"
    if test $hour -ge 5 -a $hour -lt 12
        set greeting "Chào buổi sáng"
        set icon "☀️"
    else if test $hour -ge 12 -a $hour -lt 18
        set greeting "Chào buổi chiều"
        set icon "⛅"
    else if test $hour -ge 18 -a $hour -lt 22
        set greeting "Chào buổi tối"
        set icon "🌙"
    else
        set greeting "Chúc ngủ ngon"
        set icon "💤"
    end

    # ── Uptime ──
    set -l uptime_str (uptime -p 2>/dev/null | string replace 'up ' '')
    if test -z "$uptime_str"
        set uptime_str "unknown"
    end

    # ── Battery (PABAS0241231 từ system report) ──
    set -l bat_pct "N/A"
    set -l bat_icon "🔋"
    set -l bat_path ""
    for p in /sys/class/power_supply/BAT* /sys/class/power_supply/PABAS*
        if test -d "$p"
            set bat_path "$p"
            break
        end
    end

    if test -n "$bat_path" -a -f "$bat_path/capacity"
        set bat_pct (cat "$bat_path/capacity")
        set -l bat_status (cat "$bat_path/status" 2>/dev/null)
        if test "$bat_status" = "Charging"
            set bat_icon "󰂄"
        else if test "$bat_status" = "Full"
            set bat_icon "󰁹"
        else if test $bat_pct -gt 80
            set bat_icon "󰂂"
        else if test $bat_pct -gt 50
            set bat_icon "󰂀"
        else if test $bat_pct -gt 20
            set bat_icon "󰁾"
        else
            set bat_icon "󰂎"
        end
    end

    # ── CPU temp (Tctl từ k10temp-pci-00c3) ──
    set -l temp "N/A"
    if command -v sensors >/dev/null 2>&1
        set temp (sensors 2>/dev/null | awk '/Tctl:/{gsub(/[+°C]/,"",$2); print $2; exit}')
    end

    # ── Build greeting box (array) ──
    set -l g
    set -a g (set_color -o f9a825)"╭──── $greeting, $USER $icon"(set_color normal)
    set -a g (set_color 00acc1)"│"(set_color normal)
    set -a g (set_color a6adc8)"│    📅  $date_str"(set_color normal)
    set -a g (set_color a6adc8)"│    🕐  $time_str"(set_color normal)
    set -a g (set_color 6c7086)"│"(set_color normal)
    set -a g (set_color 6c7086)"│    ⏱  Uptime: $uptime_str"(set_color normal)
    set -a g (set_color 6c7086)"│    $bat_icon  Battery: $bat_pct%"(set_color normal)
    set -a g (set_color 6c7086)"│    🌡  CPU: $temp°C"(set_color normal)
    set -a g (set_color f9a825)"╰──────────────────────────"(set_color normal)

    # ── Đọc ASCII art từ file ──
    set -l art_file "$HOME/.config/fastfetch/ascii/hhog.txt"
    set -l a
    if test -f "$art_file"
        while read -l line
            set -a a "$line"
        end < "$art_file"
    else
        set -a a "  [ASCII art không tồn tại]"
        set -a a "  File: $art_file"
    end

    # ── Render song song ──
    echo ""
    set -l max_lines (count $g)
    if test (count $a) -gt $max_lines
        set max_lines (count $a)
    end

    # Độ rộng cố định cho greeting box (điều chỉnh nếu cần)
    set -l box_width 42

    for i in (seq 1 $max_lines)
        set -l g_line ""
        if test $i -le (count $g)
            set g_line $g[$i]
        end

        set -l a_line ""
        if test $i -le (count $a)
            set a_line $a[$i]
        end

        # Tính độ rộng thực tế (bỏ qua ANSI escape codes)
        set -l plain (string replace -ar '\e\[[0-9;]*m' '' -- $g_line)
        set -l actual_width (string length -- $plain)
        set -l pad_len (math $box_width - $actual_width)

        # Tạo padding
        set -l pad ""
        if test $pad_len -gt 0
            set pad (printf '%*s' $pad_len '')
        end

        printf "%s%s%s\n" "$g_line" "$pad" "$a_line"
    end

    echo ""
end



alias config='/usr/bin/git --git-dir=$HOME/.cfg/ --work-tree=$HOME'
alias backup-dotfiles-to-github='bash ~/.config/scripts/dotfiles_backup.sh'
