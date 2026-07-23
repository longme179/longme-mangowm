#!/usr/bin/env bash
# linux-context-report.sh
# Ultimate read-only Linux system context report generator.
# Writes a single Markdown file to ~/linux-context-report.md

set -u
export LC_ALL=C

have() { command -v "$1" >/dev/null 2>&1; }

esc_table() {
  printf '%s' "${1:-}" | tr '\n' ' ' | tr '|' '/' | tr -s ' ' | cut -c1-220
}

esc_md() {
  printf '%s' "${1:-}" | tr '"' ' ' | tr '\n' ' ' | tr -s ' ' | cut -c1-220
}

num() {
  case "${1:-}" in
    ''|*[!0-9]*) echo 0 ;;
    *) echo "$1" ;;
  esac
}

if [ -z "${HOME:-}" ]; then
  HOME="$(getent passwd "$(id -un 2>/dev/null)" 2>/dev/null | cut -d: -f6)"
fi
[ -z "${HOME:-}" ] && HOME="/tmp"

REPORT_FILE="${REPORT_FILE:-${HOME}/linux-context-report.md}"

if [ -d "$REPORT_FILE" ] || ! touch "$REPORT_FILE" 2>/dev/null; then
  echo "ERROR: Cannot write report file: $REPORT_FILE" >&2
  exit 1
fi

chmod 600 "$REPORT_FILE" 2>/dev/null || true

if have sudo && sudo -n true 2>/dev/null; then
  SUDO="sudo"
  SUDO_STATUS="passwordless sudo available"
else
  SUDO=""
  SUDO_STATUS="no passwordless sudo"
fi

NOW="$(date '+%Y-%m-%d %H:%M:%S %Z' 2>/dev/null || echo unknown)"
HOSTNAME_VAL="$(hostname 2>/dev/null || cat /proc/sys/kernel/hostname 2>/dev/null || echo unknown)"

if [ -r /etc/os-release ]; then
  . /etc/os-release
  OS_PRETTY="${PRETTY_NAME:-Unknown Linux}"
else
  OS_PRETTY="$(uname -s 2>/dev/null || echo Unknown)"
fi

KERNEL="$(uname -r 2>/dev/null || echo unknown)"
ARCH="$(uname -m 2>/dev/null || echo unknown)"
USER_NAME="$(id -un 2>/dev/null || echo unknown)"
USER_ID="$(id -u 2>/dev/null || echo unknown)"
USER_GROUPS="$(id -nG 2>/dev/null | tr ' ' ',')"

if UPTIME_STR="$(uptime -p 2>/dev/null)" && [ -n "$UPTIME_STR" ]; then
  :
elif UPTIME_RAW="$(uptime 2>/dev/null)"; then
  UPTIME_STR="$(printf '%s' "$UPTIME_RAW" | sed 's/^ *//; s/.*up /up /; s/, *[0-9]* user.*//; s/ *load average.*//')"
else
  UPTIME_STR="$(awk '{d=int($1/86400); h=int(($1%86400)/3600); m=int(($1%3600)/60); printf "up %dd %dh %dm", d,h,m}' /proc/uptime 2>/dev/null || echo unknown)"
fi
[ -z "${UPTIME_STR:-}" ] && UPTIME_STR="unknown"

VIRT="$(systemd-detect-virt 2>/dev/null || echo unknown)"

DMI_MANUFACTURER="$($SUDO dmidecode -s system-manufacturer 2>/dev/null || echo unknown)"
DMI_PRODUCT="$($SUDO dmidecode -s system-product-name 2>/dev/null || echo unknown)"
DMI_BIOS="$($SUDO dmidecode -s bios-version 2>/dev/null || echo unknown)"

CPU_MODEL="$(lscpu 2>/dev/null | awk -F: '/Model name/ {gsub(/^ +/,"",$2); print $2; exit}')"
[ -z "$CPU_MODEL" ] && CPU_MODEL="$(awk -F: '/model name/ {gsub(/^ +/,"",$2); print $2; exit}' /proc/cpuinfo 2>/dev/null)"
[ -z "$CPU_MODEL" ] && CPU_MODEL="$(lscpu 2>/dev/null | awk -F: '/Hardware name/ {gsub(/^ +/,"",$2); print $2; exit}')"
[ -z "$CPU_MODEL" ] && CPU_MODEL="unknown"

CPU_LOGICAL="$(nproc 2>/dev/null || grep -c ^processor /proc/cpuinfo 2>/dev/null || echo 1)"
CPU_LOGICAL="$(num "$CPU_LOGICAL")"

CPU_SOCKETS="$(lscpu 2>/dev/null | awk -F: '/Socket\(s\)/ {gsub(/[^0-9]/,"",$2); print $2; exit}')"
CPU_CORES_PER_SOCKET="$(lscpu 2>/dev/null | awk -F: '/Core\(s\) per socket/ {gsub(/[^0-9]/,"",$2); print $2; exit}')"
CPU_THREADS_PER_CORE="$(lscpu 2>/dev/null | awk -F: '/Thread\(s\) per core/ {gsub(/[^0-9]/,"",$2); print $2; exit}')"

CPU_SOCKETS="$(num "${CPU_SOCKETS:-0}")"
CPU_CORES_PER_SOCKET="$(num "${CPU_CORES_PER_SOCKET:-0}")"
CPU_THREADS_PER_CORE="$(num "${CPU_THREADS_PER_CORE:-0}")"

CPU_PHYSICAL=$(( CPU_SOCKETS * CPU_CORES_PER_SOCKET ))
[ "$CPU_PHYSICAL" -eq 0 ] && CPU_PHYSICAL="$CPU_LOGICAL"

CPU_MHZ="$(lscpu 2>/dev/null | awk -F: '/CPU MHz/ {gsub(/^ +/,"",$2); print $2; exit}')"
CPU_MAX_MHZ="$(lscpu 2>/dev/null | awk -F: '/CPU max MHz/ {gsub(/[^0-9.]/,"",$2); print $2; exit}')"
CPU_VIRT="$(lscpu 2>/dev/null | awk -F: '/Virtualization/ {gsub(/^ +/,"",$2); print $2; exit}')"

meminfo_kb() {
  awk -v key="$1" '$1==key":" {print $2}' /proc/meminfo 2>/dev/null
}

kb_to_human() {
  awk -v kb="${1:-0}" 'BEGIN{
    if (kb == "") kb = 0;
    split("Ki Mi Gi Ti", u, " ");
    i = 1;
    while (kb >= 1024 && i < 4) { kb /= 1024; i++ }
    printf "%.1f%s", kb, u[i]
  }'
}

if have free; then
  RAM_TOTAL_H="$(free -h 2>/dev/null | awk '/^Mem/ {print $2}')"
  RAM_USED_H="$(free -h 2>/dev/null | awk '/^Mem/ {print $3}')"
  RAM_FREE_H="$(free -h 2>/dev/null | awk '/^Mem/ {print $4}')"
  RAM_BUFF_H="$(free -h 2>/dev/null | awk '/^Mem/ {print $6}')"
  RAM_AVAIL_H="$(free -h 2>/dev/null | awk '/^Mem/ {print $7}')"
  RAM_TOTAL_M="$(free -m 2>/dev/null | awk '/^Mem/ {print $2}')"
  RAM_AVAIL_M="$(free -m 2>/dev/null | awk '/^Mem/ {print $7}')"
else
  RAM_TOTAL_KB="$(meminfo_kb MemTotal)"
  RAM_AVAIL_KB="$(meminfo_kb MemAvailable)"
  [ -z "$RAM_AVAIL_KB" ] && RAM_AVAIL_KB="$(meminfo_kb MemFree)"

  RAM_TOTAL_H="$(kb_to_human "${RAM_TOTAL_KB:-0}")"
  RAM_AVAIL_H="$(kb_to_human "${RAM_AVAIL_KB:-0}")"
  RAM_USED_H="unknown"
  RAM_FREE_H="unknown"
  RAM_BUFF_H="unknown"

  RAM_TOTAL_M=$(( ${RAM_TOTAL_KB:-0} / 1024 ))
  RAM_AVAIL_M=$(( ${RAM_AVAIL_KB:-0} / 1024 ))
fi

RAM_TOTAL_M="$(num "${RAM_TOTAL_M:-0}")"
RAM_AVAIL_M="$(num "${RAM_AVAIL_M:-0}")"

ROOT_DEV="$(findmnt -no SOURCE / 2>/dev/null || df -P / 2>/dev/null | awk 'NR==2{print $1}')"
ROOT_FSTYPE="$(findmnt -no FSTYPE / 2>/dev/null || echo unknown)"
ROOT_USE="$(df -P / 2>/dev/null | awk 'NR==2{gsub("%","",$5); print $5}')"
ROOT_USE="$(num "${ROOT_USE:-0}")"

ROOT_SIZE_H="$(df -h -P / 2>/dev/null | awk 'NR==2{print $2}')"
ROOT_USED_H="$(df -h -P / 2>/dev/null | awk 'NR==2{print $3}')"
ROOT_AVAIL_H="$(df -h -P / 2>/dev/null | awk 'NR==2{print $4}')"

DEFAULT_ROUTE="$(ip route show default 2>/dev/null | head -n1)"
DEFAULT_IF="$(printf '%s' "$DEFAULT_ROUTE" | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}')"
DEFAULT_GW="$(printf '%s' "$DEFAULT_ROUTE" | awk '{print $3}')"
IP_BRIEF="$(ip -brief addr 2>/dev/null || ifconfig -a 2>/dev/null || echo 'ip/ifconfig unavailable')"
IP_SHORT="$(ip -brief addr 2>/dev/null | awk 'NR<=3 {printf "%s ", $1"@"$3}' | cut -c1-120)"
DNS_SERVERS="$(grep -E '^nameserver' /etc/resolv.conf 2>/dev/null | awk '{print $2}' | paste -sd, - 2>/dev/null)"

GPU_INFO="$(lspci -nn 2>/dev/null | grep -Ei 'vga|3d|display' || true)"
[ -z "$GPU_INFO" ] && GPU_INFO="$(lspci 2>/dev/null | grep -Ei 'vga|3d|display' || true)"
[ -z "$GPU_INFO" ] && [ -r /proc/driver/nvidia/version ] && GPU_INFO="$(head -n1 /proc/driver/nvidia/version)"
if [ -z "$GPU_INFO" ]; then
  GPU_INFO="$(ls -1 /dev/dri 2>/dev/null | sed 's#^#/dev/dri/#' || true)"
fi
[ -z "$GPU_INFO" ] && GPU_INFO="No GPU detected or lspci unavailable"

PKG_MGR="unknown"
PKG_FAMILY="unknown"
PKG_COUNT="unknown"

if have apt-get || have dpkg; then
  PKG_MGR="apt/dpkg"
  PKG_FAMILY="dpkg"
  PKG_COUNT="$(dpkg -l 2>/dev/null | grep -c '^ii')"
elif have dnf || have rpm || have yum || have zypper; then
  if have dnf; then
    PKG_MGR="dnf/rpm"
  elif have yum; then
    PKG_MGR="yum/rpm"
  elif have zypper; then
    PKG_MGR="zypper/rpm"
  else
    PKG_MGR="rpm"
  fi
  PKG_FAMILY="rpm"
  PKG_COUNT="$(rpm -qa 2>/dev/null | wc -l | tr -d ' ')"
elif have pacman; then
  PKG_MGR="pacman"
  PKG_FAMILY="pacman"
  PKG_COUNT="$(pacman -Q 2>/dev/null | wc -l | tr -d ' ')"
elif have apk; then
  PKG_MGR="apk"
  PKG_FAMILY="apk"
  PKG_COUNT="$(apk info 2>/dev/null | wc -l | tr -d ' ')"
fi

PKG_COUNT="$(num "${PKG_COUNT:-0}")"

PKGLIST=(
  git curl wget micro htop iotop nginx apache2 httpd openssh-server ssh
  ufw firewalld fail2ban docker.io docker-ce podman mysql-server mariadb-server
  postgresql postgresql-server redis redis-server qemu-kvm libvirt-daemon-system
  libvirt snapd flatpak python3 python3-pip nodejs npm zfsutils-linux zfs
  zfsutils btrfs-progs lvm2 cryptsetup wireguard tailscale zerotier-one
  dnsmasq unbound bind9
)

IMPORTANT_PKGS=""
for p in "${PKGLIST[@]}"; do
  out=""
  case "$PKG_FAMILY" in
    dpkg)
      out="$(dpkg-query -W -f='${Package} ${Version}\n' "$p" 2>/dev/null)"
      ;;
    rpm)
      if rpm -q "$p" >/dev/null 2>&1; then
        out="$(rpm -q --qf '%{NAME} %{VERSION}-%{RELEASE}\n' "$p" 2>/dev/null)"
      fi
      ;;
    pacman)
      out="$(pacman -Q "$p" 2>/dev/null)"
      ;;
    apk)
      out="$(apk info "$p" 2>/dev/null | head -n1)"
      ;;
    *)
      out=""
      ;;
  esac
  [ -n "$out" ] && IMPORTANT_PKGS+="$out"$'\n'
done

SYSTEMD=0
[ -d /run/systemd/system ] && SYSTEMD=1

if [ "$SYSTEMD" -eq 1 ]; then
  RUNNING_SERVICES="$(systemctl list-units --type=service --state=running --no-pager --no-legend 2>/dev/null | awk '{print $1}' | sed 's/\.service$//' | head -n 60)"
  FAILED_SERVICES="$(systemctl list-units --type=service --state=failed --no-pager --no-legend 2>/dev/null | awk '{print $1}' | sed 's/\.service$//')"
  ENABLED_COUNT="$(systemctl list-unit-files --type=service --state=enabled --no-pager --no-legend 2>/dev/null | wc -l | tr -d ' ')"
else
  RUNNING_SERVICES="systemd not detected; services unknown"
  FAILED_SERVICES=""
  ENABLED_COUNT="unknown"
fi

RUNNING_SERVICES_OUT="${RUNNING_SERVICES:-none}"
[ -z "$RUNNING_SERVICES_OUT" ] && RUNNING_SERVICES_OUT="none"

FAILED_SERVICES_OUT="${FAILED_SERVICES:-none}"
[ -z "$FAILED_SERVICES_OUT" ] && FAILED_SERVICES_OUT="none"

DOCKER_INFO="not installed"
DOCKER_VERSION="unknown"
DOCKER_RUNNING=0
DOCKER_ALL=0
DOCKER_PS=""

if have docker; then
  DOCKER_VERSION="$(timeout 5 docker version --format '{{.Server.Version}}' 2>/dev/null || echo 'installed, daemon not accessible')"
  DOCKER_RUNNING="$(timeout 5 docker ps -q 2>/dev/null | wc -l | tr -d ' ')"
  DOCKER_ALL="$(timeout 5 docker ps -aq 2>/dev/null | wc -l | tr -d ' ')"
  DOCKER_PS="$(timeout 5 docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}' 2>/dev/null | head -n 20)"
  DOCKER_INFO="Docker version: ${DOCKER_VERSION}; running: $(num "$DOCKER_RUNNING"); total: $(num "$DOCKER_ALL")"
fi

PODMAN_INFO="not installed"
PODMAN_RUNNING=0
PODMAN_ALL=0
PODMAN_PS=""

if have podman; then
  PODMAN_RUNNING="$(timeout 5 podman ps -q 2>/dev/null | wc -l | tr -d ' ')"
  PODMAN_ALL="$(timeout 5 podman ps -aq 2>/dev/null | wc -l | tr -d ' ')"
  PODMAN_PS="$(timeout 5 podman ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}' 2>/dev/null | head -n 20)"
  PODMAN_INFO="Podman running: $(num "$PODMAN_RUNNING"); total: $(num "$PODMAN_ALL")"
fi

FW_ACTIVE=0
FW_INFO=""

if have ufw; then
  FW_INFO="$(ufw status verbose 2>/dev/null || echo 'ufw installed, status unavailable')"
  printf '%s' "$FW_INFO" | grep -qi 'Status: active' && FW_ACTIVE=1
elif have firewall-cmd; then
  if timeout 5 firewall-cmd --state 2>/dev/null | grep -qi running; then
    FW_ACTIVE=1
    FW_INFO="firewalld running"
    FW_INFO+=$'\n'"$(firewall-cmd --get-active-zones 2>/dev/null)"
  else
    FW_INFO="firewalld not running"
  fi
elif have nft; then
  NFT_TABLES="$($SUDO nft list tables 2>/dev/null || nft list tables 2>/dev/null || true)"
  if [ -n "$NFT_TABLES" ]; then
    FW_ACTIVE=1
    FW_INFO="nftables tables present:"$'\n'"$NFT_TABLES"
  else
    FW_INFO="nftables installed, no tables found or insufficient permission"
  fi
elif have iptables; then
  IPT_RULES="$($SUDO iptables -S 2>/dev/null || true)"
  if [ -n "$IPT_RULES" ]; then
    FW_ACTIVE=1
    FW_INFO="$IPT_RULES"
  else
    FW_INFO="iptables installed, but rules unavailable without sudo"
  fi
else
  FW_INFO="No common firewall tool found"
fi

[ -z "$FW_INFO" ] && FW_INFO="unknown"

LOADAVG="$(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null || echo '0 0 0')"
LOAD1="$(printf '%s' "$LOADAVG" | awk '{print $1}')"
LOAD_PER_CORE="$(awk -v l="${LOAD1:-0}" -v c="${CPU_LOGICAL:-1}" 'BEGIN{if(c+0>0) printf "%.2f", l/c; else print "0"}')"

TOP_CPU="$(ps aux --sort=-%cpu 2>/dev/null | head -n 11)"
[ -z "$TOP_CPU" ] && TOP_CPU="$(ps aux 2>/dev/null | head -n 11 || echo 'ps unavailable')"

TOP_MEM="$(ps aux --sort=-%mem 2>/dev/null | head -n 11)"
[ -z "$TOP_MEM" ] && TOP_MEM="$(ps aux 2>/dev/null | head -n 11 || echo 'ps unavailable')"

if have ss; then
  if [ -n "$SUDO" ]; then
    OPEN_PORTS="$($SUDO ss -tulnp 2>/dev/null || ss -tuln 2>/dev/null)"
  else
    OPEN_PORTS="$(ss -tuln 2>/dev/null)"
  fi
elif have netstat; then
  OPEN_PORTS="$(netstat -tuln 2>/dev/null)"
else
  OPEN_PORTS="ss/netstat unavailable"
fi

[ -z "$OPEN_PORTS" ] && OPEN_PORTS="No open port data available"

ESTABLISHED_COUNT="$(ss -tan 2>/dev/null | awk 'NR>1 && $1=="ESTAB" {c++} END{print c+0}')"
ESTABLISHED_COUNT="$(num "${ESTABLISHED_COUNT:-0}")"

USERS_LOGGED="$(who 2>/dev/null || echo 'who unavailable')"
LAST_LOGINS="$(last -n 5 2>/dev/null || echo 'last unavailable')"

LAST_UPDATES=""
if [ -r /var/log/apt/history.log ]; then
  LAST_UPDATES="$(tail -n 25 /var/log/apt/history.log 2>/dev/null)"
elif [ -r /var/log/dnf.log ]; then
  LAST_UPDATES="$(tail -n 25 /var/log/dnf.log 2>/dev/null)"
elif [ -r /var/log/yum.log ]; then
  LAST_UPDATES="$(tail -n 25 /var/log/yum.log 2>/dev/null)"
elif [ -r /var/log/pacman.log ]; then
  LAST_UPDATES="$(grep -E 'upgraded|installed|removed' /var/log/pacman.log 2>/dev/null | tail -n 25)"
elif [ -r /var/log/emerge.log ]; then
  LAST_UPDATES="$(tail -n 25 /var/log/emerge.log 2>/dev/null)"
elif [ -r /var/log/zypp/history ]; then
  LAST_UPDATES="$(tail -n 25 /var/log/zypp/history 2>/dev/null)"
else
  LAST_UPDATES="No readable package manager log found."
fi

[ -z "$LAST_UPDATES" ] && LAST_UPDATES="No recent package activity found or logs unreadable."

SSH_CONFIG="$(cat /etc/ssh/sshd_config /etc/ssh/sshd_config.d/* 2>/dev/null | grep -Ei '^[[:space:]]*(Port|PermitRootLogin|PasswordAuthentication|PubkeyAuthentication|AllowUsers|AllowGroups|ListenAddress)' | head -n 30)"
[ -z "$SSH_CONFIG" ] && SSH_CONFIG="No explicit sshd settings found or config not readable. Use 'sudo sshd -T' for effective config."

SELINUX="$(getenforce 2>/dev/null || echo 'not installed')"

AA_ENABLED="$(cat /sys/module/apparmor/parameters/enabled 2>/dev/null || echo unknown)"
AA_PROFILES="$(wc -l < /sys/kernel/security/apparmor/profiles 2>/dev/null | tr -d ' ' || echo unknown)"
[ -z "$AA_PROFILES" ] && AA_PROFILES="unknown"

SYSCTL_INFO=""
if have sysctl; then
  for key in \
    kernel.randomize_va_space \
    net.ipv4.ip_forward \
    net.ipv4.conf.all.accept_redirects \
    net.ipv4.conf.all.send_redirects \
    net.ipv4.conf.all.accept_source_route \
    net.ipv4.tcp_syncookies \
    vm.swappiness \
    vm.overcommit_memory \
    fs.inotify.max_user_watches \
    fs.file-max
  do
    val="$(sysctl -n "$key" 2>/dev/null || echo unavailable)"
    SYSCTL_INFO+="${key} = ${val}"$'\n'
  done
else
  SYSCTL_INFO="sysctl unavailable"
fi

RECS=()

if [ "$ROOT_USE" -ge 90 ]; then
  RECS+=("Root filesystem usage is ${ROOT_USE}% (critical). Free space urgently: clean logs, package cache, old kernels, container images/volumes; then verify with 'df -h' and 'du -xh --max-depth=1 / | sort -rh | head -20'.")
elif [ "$ROOT_USE" -ge 80 ]; then
  RECS+=("Root filesystem usage is ${ROOT_USE}% (high). Plan cleanup or expansion before it causes service failures.")
fi

MEM_LOW=0
if [ "$RAM_AVAIL_M" -lt 500 ]; then
  MEM_LOW=1
fi
if [ "$RAM_TOTAL_M" -gt 0 ] && [ $(( RAM_AVAIL_M * 100 / RAM_TOTAL_M )) -lt 10 ]; then
  MEM_LOW=1
fi
if [ "$MEM_LOW" -eq 1 ]; then
  RECS+=("Available memory is low (${RAM_AVAIL_H:-unknown} available of ${RAM_TOTAL_H:-unknown}). Check top memory processes, consider tuning services, adding swap, or increasing RAM.")
fi

LOAD_HIGH="$(awk -v l="${LOAD1:-0}" -v c="${CPU_LOGICAL:-1}" 'BEGIN{print (c>0 && l/c > 1) ? 1 : 0}')"
if [ "$LOAD_HIGH" -eq 1 ]; then
  RECS+=("Load average per core is ${LOAD_PER_CORE}. Investigate CPU consumers with 'top'/'htop', 'ps aux --sort=-%cpu', and consider systemd resource controls or scaling out.")
fi

if [ "$FAILED_SERVICES_OUT" != "none" ]; then
  FAILED_LIST="$(printf '%s' "$FAILED_SERVICES" | tr '\n' ',' | sed 's/,$//')"
  RECS+=("Failed services detected: ${FAILED_LIST}. Inspect with 'systemctl status <unit>' and 'journalctl -xeu <unit>'.")
fi

if [ "$FW_ACTIVE" -ne 1 ]; then
  RECS+=("No confirmed active firewall. Enable and restrict ingress with ufw/firewalld/nftables; allow only required ports such as SSH, HTTP/HTTPS, VPN.")
fi

if printf '%s' "$SSH_CONFIG" | grep -Eiq '^[^#]*PermitRootLogin[[:space:]]+yes'; then
  RECS+=("SSH PermitRootLogin appears to be yes. Set 'PermitRootLogin no' or 'prohibit-password' and reload sshd after testing.")
fi

if printf '%s' "$SSH_CONFIG" | grep -Eiq '^[^#]*PasswordAuthentication[[:space:]]+yes'; then
  RECS+=("SSH PasswordAuthentication appears to be yes. Prefer key-based auth and set 'PasswordAuthentication no' after confirming key login works.")
fi

DOCKER_ALL_NUM="$(num "${DOCKER_ALL:-0}")"
DOCKER_RUNNING_NUM="$(num "${DOCKER_RUNNING:-0}")"
if have docker && [ "$DOCKER_ALL_NUM" -gt 0 ] && [ "$DOCKER_RUNNING_NUM" -lt "$DOCKER_ALL_NUM" ]; then
  RECS+=("Docker has stopped containers. Review with 'docker ps -a' and prune unused objects cautiously: 'docker system prune -a --volumes' only if you understand the impact.")
fi

if [ ${#RECS[@]} -eq 0 ]; then
  RECS+=("Maintain a regular update and reboot window; verify unattended security updates on your distro.")
  RECS+=("Ensure backups are tested: config, databases, container volumes, and critical service data.")
  RECS+=("Add monitoring/alerting for disk, memory, load, failed services, and certificate/port availability.")
fi

write_full_packages() {
  case "$PKG_FAMILY" in
    dpkg)
      dpkg-query -W -f='${Package}\t${Version}\t${Status}\n' 2>/dev/null \
        | awk -F'\t' '$3 ~ /install ok installed/ {print $1"\t"$2}' \
        | sort
      ;;
    rpm)
      rpm -qa --qf '%{NAME}\t%{VERSION}-%{RELEASE}\t%{ARCH}\n' 2>/dev/null | sort
      ;;
    pacman)
      pacman -Q 2>/dev/null | awk '{print $1"\t"$2}' | sort
      ;;
    apk)
      apk info -v 2>/dev/null | sort
      ;;
    *)
      echo "Unknown package manager; cannot list full packages."
      ;;
  esac
}

write_extra_packages() {
  if have snap; then
    printf '#### Snap packages\n\n'
    printf '```text\n'
    snap list --all 2>/dev/null || echo "snap unavailable"
    printf '```\n\n'
  fi

  if have flatpak; then
    printf '#### Flatpak packages\n\n'
    printf '```text\n'
    flatpak list --app --columns=application,version,branch,origin 2>/dev/null \
      || flatpak list --app 2>/dev/null \
      || echo "flatpak unavailable"
    printf '```\n\n'
  fi
}

generate_report() {
  CPU_D="$(esc_md "$CPU_MODEL")"
  RAM_D="total ${RAM_TOTAL_H:-?}, avail ${RAM_AVAIL_H:-?}"
  STORAGE_D="root ${ROOT_DEV:-?} ${ROOT_FSTYPE:-?} ${ROOT_USE:-?}% used"
  NET_D="${DEFAULT_IF:-net} ${IP_SHORT:-}"
  SVC_D="$(printf '%s' "$RUNNING_SERVICES" | head -n 6 | tr '\n' ',' | sed 's/,$//')"
  [ -z "$SVC_D" ] && SVC_D="services"

  printf '# Ultimate Linux System Context Report\n\n'
  printf '_Generated: %s_\n\n' "$(esc_table "$NOW")"
  printf '> Read-only single-file report. Redact sensitive data before sharing publicly.\n\n'

  printf '## 1. System Overview\n\n'
  printf '| Item | Value |\n'
  printf '|---|---|\n'
  printf '| OS | %s |\n' "$(esc_table "$OS_PRETTY")"
  printf '| Kernel | %s |\n' "$(esc_table "$KERNEL")"
  printf '| Hostname | %s |\n' "$(esc_table "$HOSTNAME_VAL")"
  printf '| Architecture | %s |\n' "$(esc_table "$ARCH")"
  printf '| Uptime | %s |\n' "$(esc_table "$UPTIME_STR")"
  printf '| Current user | %s (UID %s) |\n' "$(esc_table "$USER_NAME")" "$(esc_table "$USER_ID")"
  printf '| Groups | %s |\n' "$(esc_table "$USER_GROUPS")"
  printf '| Shell | %s |\n' "$(esc_table "${SHELL:-unknown}")"
  printf '| Virtualization | %s |\n' "$(esc_table "$VIRT")"
  printf '| Sudo context | %s |\n\n' "$(esc_table "$SUDO_STATUS")"

  printf '## 2. Hardware Summary\n\n'

  printf '### Platform\n\n'
  printf '| Manufacturer | Product | BIOS | Virtualization |\n'
  printf '|---|---|---|---|\n'
  printf '| %s | %s | %s | %s |\n\n' \
    "$(esc_table "$DMI_MANUFACTURER")" \
    "$(esc_table "$DMI_PRODUCT")" \
    "$(esc_table "$DMI_BIOS")" \
    "$(esc_table "$VIRT")"

  printf '### CPU\n\n'
  printf '| Model | Logical CPUs | Physical cores | Threads/core | MHz | Max MHz | Virtualization |\n'
  printf '|---|---:|---:|---:|---:|---:|---|\n'
  printf '| %s | %s | %s | %s | %s | %s | %s |\n\n' \
    "$(esc_table "$CPU_MODEL")" \
    "$(esc_table "$CPU_LOGICAL")" \
    "$(esc_table "$CPU_PHYSICAL")" \
    "$(esc_table "${CPU_THREADS_PER_CORE:-0}")" \
    "$(esc_table "${CPU_MHZ:-unknown}")" \
    "$(esc_table "${CPU_MAX_MHZ:-unknown}")" \
    "$(esc_table "${CPU_VIRT:-unknown}")"

  printf '### Memory\n\n'
  printf '| Total | Used | Free | Buff/Cache | Available |\n'
  printf '|---|---|---|---|---|\n'
  printf '| %s | %s | %s | %s | %s |\n\n' \
    "$(esc_table "${RAM_TOTAL_H:-unknown}")" \
    "$(esc_table "${RAM_USED_H:-unknown}")" \
    "$(esc_table "${RAM_FREE_H:-unknown}")" \
    "$(esc_table "${RAM_BUFF_H:-unknown}")" \
    "$(esc_table "${RAM_AVAIL_H:-unknown}")"

  printf '### Storage\n\n'
  printf '| Mount | Device | Type | Size | Used | Avail | Use%% |\n'
  printf '|---|---|---|---|---|---|---:|\n'
  printf '| / | %s | %s | %s | %s | %s | %s%% |\n\n' \
    "$(esc_table "${ROOT_DEV:-unknown}")" \
    "$(esc_table "${ROOT_FSTYPE:-unknown}")" \
    "$(esc_table "${ROOT_SIZE_H:-unknown}")" \
    "$(esc_table "${ROOT_USED_H:-unknown}")" \
    "$(esc_table "${ROOT_AVAIL_H:-unknown}")" \
    "$(esc_table "$ROOT_USE")"

  printf '#### lsblk\n\n'
  printf '```text\n'
  lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,UUID 2>/dev/null || lsblk 2>/dev/null || cat /proc/partitions 2>/dev/null || echo "lsblk unavailable"
  printf '```\n\n'

  printf '#### df -hT\n\n'
  printf '```text\n'
  df -hT -x tmpfs -x devtmpfs -x squashfs 2>/dev/null || df -h 2>/dev/null || echo "df unavailable"
  printf '```\n\n'

  printf '### GPU\n\n'
  printf '```text\n%s\n```\n\n' "$GPU_INFO"

  printf '### Network\n\n'
  printf '| Default interface | Default gateway | DNS servers |\n'
  printf '|---|---|---|\n'
  printf '| %s | %s | %s |\n\n' \
    "$(esc_table "${DEFAULT_IF:-unknown}")" \
    "$(esc_table "${DEFAULT_GW:-unknown}")" \
    "$(esc_table "${DNS_SERVERS:-unknown}")"

  printf '```text\n%s\n```\n\n' "$IP_BRIEF"

  printf '## 3. Key Software & Services\n\n'

  printf '### Package manager\n\n'
  printf '| Manager | Installed packages |\n'
  printf '|---|---:|\n'
  printf '| %s | %s |\n\n' "$(esc_table "$PKG_MGR")" "$(esc_table "$PKG_COUNT")"

  printf '### Important installed packages\n\n'
  printf '```text\n'
  if [ -n "$IMPORTANT_PKGS" ]; then
    printf '%s' "$IMPORTANT_PKGS"
  else
    echo "None of the tracked common packages found."
  fi
  printf '```\n\n'

  printf '### Full installed packages\n\n'
  printf '_Package count: %s._\n\n' "$(esc_table "$PKG_COUNT")"
  printf '```text\n'
  write_full_packages
  printf '```\n\n'

  write_extra_packages

  printf '### Running services (systemd, first 60)\n\n'
  printf '```text\n%s\n```\n\n' "$RUNNING_SERVICES_OUT"

  printf '### Failed services\n\n'
  printf '```text\n%s\n```\n\n' "$FAILED_SERVICES_OUT"

  printf '### Enabled services count\n\n'
  printf '_Enabled service unit files: %s_\n\n' "$(esc_table "${ENABLED_COUNT:-unknown}")"

  printf '### Container runtimes\n\n'
  printf '| Runtime | Status |\n'
  printf '|---|---|\n'
  printf '| Docker | %s |\n' "$(esc_table "$DOCKER_INFO")"
  printf '| Podman | %s |\n\n' "$(esc_table "$PODMAN_INFO")"

  if [ -n "$DOCKER_PS" ]; then
    printf '#### Docker containers\n\n'
    printf '```text\n%s\n```\n\n' "$DOCKER_PS"
  fi

  if [ -n "$PODMAN_PS" ]; then
    printf '#### Podman containers\n\n'
    printf '```text\n%s\n```\n\n' "$PODMAN_PS"
  fi

  printf '### Firewall\n\n'
  printf '```text\n%s\n```\n\n' "$FW_INFO"

  printf '## 4. Performance & Security\n\n'

  printf '### Load\n\n'
  printf '| Load average (1 5 15) | Load/core | Logical CPUs |\n'
  printf '|---|---:|---:|\n'
  printf '| %s | %s | %s |\n\n' \
    "$(esc_table "$LOADAVG")" \
    "$(esc_table "$LOAD_PER_CORE")" \
    "$(esc_table "$CPU_LOGICAL")"

  printf '### Top CPU processes\n\n'
  printf '```text\n%s\n```\n\n' "$TOP_CPU"

  printf '### Top memory processes\n\n'
  printf '```text\n%s\n```\n\n' "$TOP_MEM"

  printf '### Open ports\n\n'
  printf '```text\n%s\n```\n\n' "$OPEN_PORTS"
  printf '_Established TCP connections: %s_\n\n' "$(esc_table "$ESTABLISHED_COUNT")"

  printf '### Logged in users\n\n'
  printf '```text\n%s\n```\n\n' "$USERS_LOGGED"

  printf '### Recent logins\n\n'
  printf '```text\n%s\n```\n\n' "$LAST_LOGINS"

  printf '### Recent package activity\n\n'
  printf '```text\n%s\n```\n\n' "$LAST_UPDATES"

  printf '### Security posture\n\n'
  printf '| SELinux | AppArmor enabled | AppArmor profiles |\n'
  printf '|---|---|---:|\n'
  printf '| %s | %s | %s |\n\n' \
    "$(esc_table "$SELINUX")" \
    "$(esc_table "$AA_ENABLED")" \
    "$(esc_table "$AA_PROFILES")"

  printf '#### SSH configuration (simple parse)\n\n'
  printf '```text\n%s\n```\n\n' "$SSH_CONFIG"

  printf '#### Key sysctl values\n\n'
  printf '```text\n%s\n```\n\n' "$SYSCTL_INFO"

  printf '## 5. System Diagram\n\n'

  printf '```mermaid\nflowchart LR\n'
  printf '  CPU["CPU: %s (%s threads)"] --> MEM["RAM: %s"]\n' \
    "$CPU_D" \
    "$(esc_md "$CPU_LOGICAL")" \
    "$(esc_md "$RAM_D")"
  printf '  MEM --> STORAGE["Storage: %s"]\n' "$(esc_md "$STORAGE_D")"
  printf '  STORAGE --> NET["Network: %s"]\n' "$(esc_md "$NET_D")"
  printf '  NET --> SVC["Services: %s"]\n' "$(esc_md "$SVC_D")"
  printf '```\n\n'

  printf '```text\nCPU: %s --> RAM: %s --> Storage: %s --> Network: %s --> Services: %s\n```\n\n' \
    "$CPU_D" \
    "$(esc_md "$RAM_D")" \
    "$(esc_md "$STORAGE_D")" \
    "$(esc_md "$NET_D")" \
    "$(esc_md "$SVC_D")"

  printf '## 6. Recommendations\n\n'

  idx=0
  for rec in "${RECS[@]}"; do
    idx=$((idx+1))
    [ "$idx" -gt 3 ] && break
    printf '%s. %s\n\n' "$idx" "$rec"
  done

  printf -- '---\n\n'
  printf 'Report generated by linux-context-report.sh\n'
  printf 'Single-file context report: %s\n' "$REPORT_FILE"
}

generate_report > "$REPORT_FILE" 2>/dev/null
chmod 600 "$REPORT_FILE" 2>/dev/null || true

printf '%s\n' "$REPORT_FILE"
