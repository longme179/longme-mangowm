Dove portrait for fastfetch

Original image supplied by the user:
https://cdn.droptocdn.com/files/36c8f15e-5d62-4531-bb62-53e68cbaf054/Dove_Across_a_Painted_Portrait.png

Files
  dove-56.ansi     Recommended: 56 columns x 28 lines, truecolor ASCII.
  dove-44.ansi     Compact: 44 columns x 22 lines, truecolor ASCII.
  dove-mono.txt    Plain ASCII without color; 56 columns x 28 lines.
  ascii-preview.png  Rendered comparison on a dark background.
  logo.json       Logo settings only; merge this block into your existing config.

Installation
1. Extract this archive.
2. Copy your preferred .ansi file to ~/.config/fastfetch/dove.ansi.
3. Try it without replacing your current information layout:
   fastfetch --logo-type file-raw --logo ~/.config/fastfetch/dove.ansi --logo-padding-right 4
4. For permanent use, replace only the logo object in your existing
   ~/.config/fastfetch/config.jsonc with the block from logo.json.
   Keep your existing modules and display settings.

These logos are static. Kitty supports their 24-bit colors. No image tool,
Python script, Chafa or Gowall is required when running fastfetch.
The smaller version necessarily loses facial and feather detail.

Validation: printable glyphs are ASCII; only ANSI SGR color sequences are
embedded. Both sizes were run through fastfetch 2.70.0 in file-raw mode.
The preview is a text rendering, not a screenshot of the user's terminal.
