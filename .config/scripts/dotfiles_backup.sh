#!/bin/bash
GIT_DIR="$HOME/.cfg"
WORK_TREE="$HOME"
CMD="git --git-dir=$GIT_DIR --work-tree=$WORK_TREE"

# Kiểm tra xem có file nào thay đổi không
if [ -z "$($CMD status --porcelain)" ]; then
    notify-send "Dotfiles Backup" "Không có thay đổi nào để backup." --icon=dialog-information
    exit 0
fi

TIMESTAMP=$(date +"%Y-%m-%d %H:%M:%S")

 $CMD add -A
 $CMD commit -m "Auto backup: $TIMESTAMP"
 $CMD push origin main

if [ $? -eq 0 ]; then
    notify-send "Dotfiles Backup" "🚀 Đã backup cấu hình lên GitHub thành công!" --icon=task-complete
else
    notify-send "Dotfiles Backup" "❌ Backup thất bại, kiểm tra mạng!" --icon=dialog-error
fi
