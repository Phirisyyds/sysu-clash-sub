#!/usr/bin/env bash
# 本机定时实测 Clash 节点可用性并推回仓库（由 systemd user timer 调用）
set -uo pipefail
REPO_DIR="$HOME/projects/free-vpn-clash-aggregator"
PY="/usr/bin/python3"
LOG="$HOME/.local/state/clash-cn-healthcheck.log"
mkdir -p "$(dirname "$LOG")"
{
  echo "===== $(date '+%F %T') 开始 ====="
  cd "$REPO_DIR" || exit 1
  "$PY" scripts/local_healthcheck.py --from-repo --push --concurrency 32
  echo "===== $(date '+%F %T') 结束，退出码 $? ====="
} >>"$LOG" 2>&1
tail -n 600 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
