
# 🌸 Longme's MangoWM Dotfiles

A minimal, aesthetic, and usability-focused Wayland desktop configuration tailored specifically for **MangoWM**. Inspired by end4's dots but optimized to work seamlessly with MangoWM's unique API. The entire system utilizes the **Rose Pine** color palette for a soft, consistent, and eye-friendly experience.

![WM](https://img.shields.io/badge/WM-MangoWM-blue)
![Bar](https://img.shields.io/badge/Bar-Waybar-orange)
![OS](https://img.shields.io/badge/OS-Arch%20Linux-success)



## 🛠️ Dependencies

To get the most out of this setup, ensure you have the following packages installed:

```bash
# Window Manager & Core
mangowm mmsg waybar fuzzel swaync wlogout swaybg

# Terminal & Shell
kitty fish starship

# App Utilities
rofi dolphin zed micro gowall btop fastfetch cava

# Python & Libraries (for Grid Overview)
python python-gobject gtk3 gtk-layer-shell

# System Utilities
inotify-tools ydotool brightnessctl pamixer
```

## ⌨️ Keybinds

| Keybind | Action |
| :--- | :--- |
| `Super + Tab` | Open Workspace Grid (Drag & Drop apps) |
| `Super + [1-10]` | Switch to Workspace |
| `Super + Alt + [1-10]` | Move active window to Workspace |
| `Alt + Space` | Open App Launcher (Fuzzel) |
| `Alt + Return` | Open Terminal (Kitty) |
| `Super + Q` | Kill active window |
| `Super + M` | Quit MangoWM |

## 🚀 Installation

This dotfiles setup uses the **Git Bare Repository** method. To install on a fresh machine:

1. Clone the repo into a bare directory:
   ```bash
   git clone --bare https://github.com/longme179/longme-mangowm.git $HOME/.cfg
   ```
2. Define the alias temporarily in your shell:
   ```bash
   alias config='/usr/bin/git --git-dir=$HOME/.cfg/ --work-tree=$HOME'
   ```
3. Checkout the actual files:
   ```bash
   config checkout
   ```
   *(If you get an error about existing files, backup your old `~/.config` folder and run the command again).*
4. Hide untracked files:
   ```bash
   config config --local status.showUntrackedFiles no
   ```

## 📂 Structure

Only essential configurations are backed up:
- `~/.config/mango/` - WM configs, keybinds, rules, and custom scripts (`grid.py`, `dotfiles_backup.sh`).
- `~/.config/waybar/` - Bar modules and Rose Pine styling.
- `~/.config/fuzzel/` & `~/.config/swaync/` - App launcher & Notification center.
- `~/.config/fish/` & `~/.config/kitty/` - Terminal environment.
- `~/Wallpapers/` - Wallpaper collection.

## 🙏 Credits

- [MangoWM](https://github.com/mangowm) - An excellent Wayland window manager.
- [Rose Pine](https://rosepinetheme.com/) - The beautiful pastel color palette.
- [end4](https://github.com/end-4/dots-hyprland) - Inspiration for the grid UI and overall usability design.
