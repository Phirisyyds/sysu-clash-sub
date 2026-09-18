#!/data/data/com.termux/files/usr/bin/bash
# Termux 一键准备：Python + PyYAML + Android 版 mihomo 内核 + 仓库脚本
# 用法： bash deploy/termux-setup.sh
set -uo pipefail

REPO_DIR="${REPO_DIR:-$HOME/free-vpn-clash-aggregator}"
REPO_SLUG="Phirisyyds/sysu-clash-sub"
API="https://api.github.com"
CLONE_URL="https://github.com/Phirisyyds/sysu-clash-sub.git"

echo "[1/4] 安装依赖（python / curl / git）"
pkg update -y >/dev/null 2>&1 || true
pkg install -y python curl git || { echo "pkg 安装失败：检查网络，或换 Termux 镜像源（termux-change-repo）"; exit 1; }

echo "[2/4] 安装 PyYAML"
python -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
python -m pip install --quiet pyyaml || { echo "pip 装 pyyaml 失败：换国内 PyPI 镜像再试（见 README）"; exit 1; }

echo "[3/4] 准备仓库脚本 -> $REPO_DIR"
if [ -d "$REPO_DIR/scripts" ]; then
  echo "  已存在，跳过"
else
  mkdir -p "$REPO_DIR"
  if git clone "$CLONE_URL" "$REPO_DIR" >/dev/null 2>&1; then
    echo "  git clone 完成"
  else
    echo "  git 拉取失败（手机网络常见），改用 API 逐文件下载"
    python - "$REPO_DIR" "$API" "$REPO_SLUG" <<'PY'
import base64, json, os, sys, urllib.request
dest, api, slug = sys.argv[1], sys.argv[2], sys.argv[3]
files = ["requirements.txt", "sources.yaml", "README.md",
         "scripts/healthcheck.py", "scripts/exit_check.py", "scripts/update.py",
         "scripts/local_healthcheck.py", "scripts/fetch_core.py"]
for path in files:
    request = urllib.request.Request(api + "/repos/" + slug + "/contents/" + path,
                                     headers={"User-Agent": "termux-setup", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read())
    target = os.path.join(dest, path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as handle:
        handle.write(base64.b64decode(payload["content"]))
    print("  写入", path)
PY
  fi
fi

echo "[4/4] 下载 Android 版 mihomo 内核"
cd "$REPO_DIR" || exit 1
MIHOMO_BIN="$PREFIX/bin/mihomo" python scripts/fetch_core.py \
  || { echo "  发布包域名打不开（国内常见），改用仓库内置内核"; MIHOMO_BIN="$PREFIX/bin/mihomo" python scripts/fetch_core.py --from-assets; } \
  || { echo "内核下载失败"; exit 1; }
"$PREFIX/bin/mihomo" -v

echo
echo "完成。下一步：bash deploy/termux-healthcheck.sh"
echo "（可选）想让它自动推回仓库：按 scripts/local_healthcheck.py 顶部说明准备令牌文件，权限 600。"
