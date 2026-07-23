#!/usr/bin/env bash
#
# dotfiles_backup.sh
# Backup dotfiles dùng git-dir riêng: ~/.cfg, work-tree: $HOME
#
# Ví dụ:
#   dotfiles_backup.sh
#   dotfiles_backup.sh .config/scripts/
#   dotfiles_backup.sh --pull
#   dotfiles_backup.sh --quiet .config/waybar/

set -Eeuo pipefail

# ==========================
# Cấu hình
# ==========================

DOTFILES_GIT_DIR="${DOTFILES_GIT_DIR:-$HOME/.cfg}"
DOTFILES_WORK_TREE="${DOTFILES_WORK_TREE:-$HOME}"
DOTFILES_REMOTE="${DOTFILES_REMOTE:-origin}"

LOCK_FILE="/tmp/dotfiles-backup-$(id -u).lock"
LOG_FILE="${XDG_STATE_HOME:-$HOME/.local/state}/dotfiles-backup.log"

# ==========================
# Parse args
# ==========================

QUIET=0
PULL_FIRST=0
PATHS=()

for arg in "$@"; do
    case "$arg" in
        --pull)
            PULL_FIRST=1
            ;;
        --quiet)
            QUIET=1
            ;;
        -h|--help)
            cat <<'EOF'
Dotfiles backup script

Usage:
  dotfiles_backup.sh [options] [path...]

Options:
  --pull      Chạy pull --rebase --autostash trước khi push
  --quiet     Không hiện notify-send
  -h, --help  Hiển thị trợ giúp

Examples:
  dotfiles_backup.sh
  dotfiles_backup.sh .config/scripts/
  dotfiles_backup.sh --pull
  dotfiles_backup.sh --quiet .config/waybar/ .config/fish/
EOF
            exit 0
            ;;
        *)
            PATHS+=("$arg")
            ;;
    esac
done

# ==========================
# Helpers
# ==========================

mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true

notify() {
    if (( QUIET )); then
        return 0
    fi

    if command -v notify-send >/dev/null 2>&1; then
        notify-send "$@" || true
    fi
}

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG_FILE" 2>/dev/null || true
}

fail() {
    notify "Dotfiles Backup" "$1" --icon=dialog-error
    log "ERROR: $1"
    exit 1
}

trap 'fail "Backup thất bại ở dòng $LINENO."' ERR

# ==========================
# Lock để tránh chạy trùng
# ==========================

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    notify "Dotfiles Backup" "Đang có một tiến trình backup khác đang chạy." --icon=dialog-information
    exit 0
fi

# ==========================
# Git wrapper
# ==========================

git_cfg() {
    git -C "$DOTFILES_WORK_TREE" \
        --no-optional-locks \
        --git-dir="$DOTFILES_GIT_DIR" \
        --work-tree="$DOTFILES_WORK_TREE" \
        "$@"
}

# ==========================
# Kiểm tra repo
# ==========================

if ! git_cfg rev-parse --git-dir >/dev/null 2>&1; then
    fail "❌ Không tìm thấy dotfiles repo tại: $DOTFILES_GIT_DIR"
fi

if ! git_cfg remote get-url "$DOTFILES_REMOTE" >/dev/null 2>&1; then
    fail "❌ Remote '$DOTFILES_REMOTE' chưa được cấu hình."
fi

BRANCH="$(git_cfg symbolic-ref --short HEAD 2>/dev/null || true)"
if [[ -z "$BRANCH" ]]; then
    fail "❌ Repo đang ở detached HEAD, không push tự động được."
fi

GIT_USER_NAME="$(git_cfg config user.name || true)"
GIT_USER_EMAIL="$(git_cfg config user.email || true)"

if [[ -z "$GIT_USER_NAME" || -z "$GIT_USER_EMAIL" ]]; then
    fail "❌ Thiếu git user.name hoặc user.email. Cấu hình trước khi commit."
fi

# ==========================
# Pull/rebase nếu được yêu cầu
# ==========================

if (( PULL_FIRST )); then
    if git_cfg pull --rebase --autostash "$DOTFILES_REMOTE" "$BRANCH"; then
        log "Pull/rebase OK từ $DOTFILES_REMOTE/$BRANCH"
    else
        fail "❌ Pull/rebase thất bại. Kiểm tra conflict rồi chạy lại."
    fi
fi

# ==========================
# Add files
# ==========================

if (( ${#PATHS[@]} > 0 )); then
    git_cfg add -- "${PATHS[@]}"
    log "Add các path: ${PATHS[*]}"
else
    git_cfg add -A
    log "Add toàn bộ thay đổi được .gitignore cho phép"
fi

# ==========================
# Kiểm tra có gì được stage không
# ==========================

if git_cfg diff --cached --quiet; then
    notify "Dotfiles Backup" "Không có thay đổi nào để backup." --icon=dialog-information
    log "Không có thay đổi để commit"
    exit 0
fi

# ==========================
# Commit
# ==========================

TIMESTAMP="$(date '+%Y-%m-%d %H:%M:%S')"
HOSTNAME_VAL="$(hostname 2>/dev/null || echo unknown)"
COMMIT_MSG="Auto backup: $TIMESTAMP from $HOSTNAME_VAL"

if ! git_cfg commit -m "$COMMIT_MSG"; then
    fail "❌ Commit thất bại."
fi

# ==========================
# Push
# ==========================

if git_cfg push "$DOTFILES_REMOTE" "HEAD:$BRANCH"; then
    SHORT_SHA="$(git_cfg rev-parse --short HEAD)"
    notify "Dotfiles Backup" "🚀 Đã backup thành công: $SHORT_SHA" --icon=task-complete
    log "Push OK: $SHORT_SHA lên $DOTFILES_REMOTE/$BRANCH"
else
    fail "❌ Push thất bại. Kiểm tra mạng, remote, hoặc branch bị lệch."
fi
