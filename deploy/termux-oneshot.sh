#!/data/data/com.termux/files/usr/bin/bash
# 手机端一次性测速：不需要 clone 仓库。
#   - 通过 API 拉取需要的 python 脚本
#   - 没有 mihomo 内核就自动下载对应平台版本（Termux 上是 android-arm64）
#   - 按【手机网络】实测节点，产出 clash-phone.yaml / clash-phone-lite.yaml
#
# 用法：
#   bash termux-oneshot.sh              # 只测
#   PUSH=1 bash termux-oneshot.sh       # 测完推回仓库（需令牌文件 ~/.git-credentials-hermes）
set -uo pipefail

WORK="${WORK:-$HOME/clash-phone-test}"
SLUG="Phirisyyds/sysu-clash-sub"
API="https://api.github.com"
FILES="scripts/healthcheck.py scripts/exit_check.py scripts/update.py scripts/local_healthcheck.py scripts/fetch_core.py"

mkdir -p "$WORK/scripts"

if command -v pkg >/dev/null 2>&1; then
  echo "[1/4] Termux：装依赖"
  pkg update -y >/dev/null 2>&1 || true
  pkg install -y python curl >/dev/null 2>&1 || pkg install -y python curl || true
fi

echo "[2/4] 检查 Python 与 PyYAML"
python -c "import yaml" 2>/dev/null || python -m pip install --quiet pyyaml || {
  echo "缺 PyYAML：请执行 python -m pip install pyyaml（慢的话换国内 PyPI 镜像）"; exit 1; }

echo "[3/4] 取脚本（$WORK/scripts）"
python - "$WORK" "$API" "$SLUG" "$FILES" <<'PY'
import base64, json, os, sys, urllib.request
work, api, slug, files = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4].split()
for path in files:
    request = urllib.request.Request(api + "/repos/" + slug + "/contents/" + path,
                                     headers={"User-Agent": "termux-oneshot", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read())
    target = os.path.join(work, path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as handle:
        handle.write(base64.b64decode(payload["content"]))
print("  脚本已就绪:", len(files), "个文件")
PY
[ $? -eq 0 ] || { echo "取脚本失败（网络/令牌）"; exit 1; }

CORE_BIN="${MIHOMO_BIN:-}"
if [ -z "$CORE_BIN" ]; then
  if command -v mihomo >/dev/null 2>&1; then CORE_BIN="$(command -v mihomo)"
  elif [ -n "${PREFIX:-}" ] && [ -x "$PREFIX/bin/mihomo" ]; then CORE_BIN="$PREFIX/bin/mihomo"
  else
    echo "[4/4] 下载 mihomo 内核（按平台自动选包）"
    CORE_BIN="$WORK/mihomo"
    MIHOMO_BIN="$CORE_BIN" python "$WORK/scripts/fetch_core.py" \
      || { echo "  发布包域名打不开（国内常见），改用仓库内置内核"; MIHOMO_BIN="$CORE_BIN" python "$WORK/scripts/fetch_core.py" --from-assets; } \
      || { echo "内核下载失败"; exit 1; }
  fi
else
  echo "[4/4] 使用已有内核: $CORE_BIN"
fi

echo
echo "提示：测速前先关掉 FlClash 的 VPN，否则流量绕经它，结果不准。"
echo
ARGS=(--from-repo --concurrency 12 --timeout 6000 --core "$CORE_BIN"
      --output "$WORK/clash-phone.yaml" --lite-output "$WORK/clash-phone-lite.yaml")
[ "${PUSH:-0}" = "1" ] && ARGS+=(--push)
python "$WORK/scripts/local_healthcheck.py" "${ARGS[@]}"
status=$?

if [ $status -eq 0 ] && [ -d "$HOME/storage/downloads" ]; then
  cp "$WORK/clash-phone-lite.yaml" "$HOME/storage/downloads/" 2>/dev/null && \
    echo "已复制到 手机存储/Download/（FlClash：配置 → 从文件导入）"
fi
[ $status -eq 0 ] && echo "产物: $WORK/clash-phone.yaml、$WORK/clash-phone-lite.yaml"
exit $status
