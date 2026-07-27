# MangoWM Rice — Minimal, Themed Wayland Desktop Configuration

A focused, modular Linux "rice" built around MangoWM (Wayland window manager) and a curated set of Wayland-friendly tools. Designed for people who want a lightweight, visually consistent desktop with polished defaults, easy theming, and utilities configured for productivity.

Key goals
- Minimal and modular: copy or symlink only what you need from .config
- Theme-first: cohesive color/typography across Waybar, Kitty, GTK, and shell
- Practical defaults: multi-monitor support, sensible keybindings, autostart helpers
- Reproducible: wallpapers and assets are included so you can reproduce the look quickly

Features
- MangoWM configuration
  - Main configs: bind.conf, config.conf, rule.conf — full keybindings, window rules, and behaviors
  - Autostart script to launch background services and apps on login
  - Monitor-aware settings (monitor.conf) and tag/workspace definitions (tag.conf)
- Waybar status bar
  - Complete Waybar config plus modular includes, scripts, and CSS themes
  - Rose-pine / Moon-inspired theme (CSS files included)
  - Small helper scripts (auto-reload, module scripts) for dynamic content
- Terminal environment
  - Kitty terminal configuration with useful helper scripts (search, scroll markers)
  - Starship prompt (starship.toml) preconfigured for a compact, informative prompt
- Shell and utilities
  - Fish shell configuration and recommended tooling
  - btop, cava, fastfetch configurations for terminal monitoring and system info
  - MPV config, micro/zed editor settings, and other small app configs
- Input, UI and multimedia tweaks
  - fcitx5 (IME) configs for multilingual input
  - GTK 3/4 rc and Kvantum hints for GTK/QT style coherence
  - Spicetify directory for Spotify theming (when using spicetify)
- Wallpapers
  - High-resolution wallpapers included to reproduce the visual style quickly
- Small tools and scripts
  - Wallpaper management helpers, Waybar reload scripts, logout helper templates (wlogout)
  - Utility scripts under .config/scripts for common tasks

What’s included (top-level highlights)
- .config/mango/ — MangoWM configs: bind.conf, config.conf, rule.conf, autostart.sh, monitor/tag files
- .config/waybar/ — Waybar config, CSS themes, scripts, and modules
- .config/kitty/ — kitty.conf and helper Python scripts (search.py, scroll_mark.py)
- .config/starship.toml — starship prompt configuration
- .config/fish, .config/fcitx5, .config/mpv, .config/btop, .config/cava, etc.
- Wallpapers/ — bundled wallpapers used by the rice

Quick install (one-shot)
1. Clone the repo:
   git clone https://github.com/longme179/longme-mangowm.git
   cd longme-mangowm

2. Backup your existing configs:
   mv ~/.config ~/.config.backup.$(date +%s)

3. Deploy the configs (recommended: use rsync so existing files are preserved if needed):
   rsync -av --progress .config/ ~/.config/

4. Deploy wallpapers:
   mkdir -p ~/Pictures/Wallpapers
   rsync -av Wallpapers/ ~/Pictures/Wallpapers/

5. Make scripts executable and run autostart (optional):
   chmod +x ~/.config/mango/autostart.sh
   ~/.config/mango/autostart.sh

6. Install or enable the required programs (examples — use your package manager):
   - MangoWM (or your preferred Wayland WM compatible with these configs)
   - sway/wayland compositor + wlroots (if using Sway instead of Mango)
   - waybar
   - kitty
   - starship
   - fish / bash
   - mpv, fcitx5, btop, cava
   - spicetify (optional)
   Ensure your compositor is set up to source the MangoWM config, or adapt the included files to your compositor.

Notes on customization
- Theme colors: edit the Waybar CSS files (.config/waybar/*.css) and the starship.toml for prompt colors.
- Keybindings & behaviors: update .config/mango/bind.conf and rule.conf for window rules and workspace mapping.
- Waybar modules: add or remove modules in .config/waybar/config and place helper scripts in .config/waybar/scripts.
- Terminal: tune Kitty settings in .config/kitty/kitty.conf and use the included Python helpers to extend behavior.

Troubleshooting
- If Waybar doesn’t show: check compositor logs and run waybar from a TTY to see errors.
- Missing fonts or icons: install recommended nerd fonts or the fonts referenced by your GTK/Kitty configs.
- Autostart not running: confirm autostart.sh is executable and your session sources it (or integrate with MangoWM session startup).
- If a module script fails, run it manually to see stderr and fix dependencies (jq, curl, python, etc).
