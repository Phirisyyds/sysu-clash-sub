#!/data/data/com.termux/files/usr/bin/bash
# 在手机上按【手机网络】的真实情况实测节点，产出 clash-phone.yaml / clash-phone-lite.yaml
#
# 用法：
#   bash deploy/termux-healthcheck.sh            # 只测，结果留在仓库目录
#   PUSH=1 bash deploy/termux-healthcheck.sh     # 测完用 API 推回仓库（需令牌文件）
#
# 为什么要单独测：手机和电脑不是一张网，电脑测出来能用的节点在手机上可能全超时。
set -uo pipefail

REPO_DIR="${REPO_DIR:-$HOME/free-vpn-clash-aggregator}"
cd "$REPO_DIR" || { echo "找不到 $REPO_DIR，先跑 deploy/termux-setup.sh"; exit 1; }

echo "提示：测试前先把 FlClash 的 VPN 开关关掉 —— 否则测速流量会绕经它，结果不准。"
echo

ARGS=(--from-repo --concurrency 12 --timeout 6000
      --output output/clash-phone.yaml --lite-output output/clash-phone-lite.yaml)
[ "${PUSH:-0}" = "1" ] && ARGS+=(--push)

python scripts/local_healthcheck.py "${ARGS[@]}"
status=$?

if [ $status -eq 0 ]; then
  echo
  echo "产物："
  ls -la output/clash-phone.yaml output/clash-phone-lite.yaml 2>/dev/null
  if [ -d "$HOME/storage/downloads" ]; then
    cp output/clash-phone-lite.yaml "$HOME/storage/downloads/" 2>/dev/null && \
      echo "已复制到 手机存储/Download/clash-phone-lite.yaml（FlClash：配置 → 从文件导入）"
  else
    echo "（没执行过 termux-setup-storage，写不了手机存储；可直接在 FlClash 里选文件）"
  fi
fi
exit $status
