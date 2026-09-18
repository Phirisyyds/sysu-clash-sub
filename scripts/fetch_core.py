"""下载 mihomo 内核。

两种来源：
  1. GitHub Release（默认，海外/有代理时最省事）
  2. 本仓库内置的 assets/（--from-assets）—— 国内网络下 release 的下载域名常被墙，
     而 api 通道一般还通，所以 CI/手机端优先用它兜底。
"""

from __future__ import annotations

import gzip
import json
import os
import re
import stat
import subprocess
import sys
import urllib.request

API_LATEST = "https://api.github.com/repos/Metacubex/mihomo/releases/latest"
ASSET_REPO = os.getenv("ASSET_REPO", "Phirisyyds/sysu-clash-sub")

DEFAULT_PATTERN = r"^mihomo-linux-amd64-v[\d.]+(?:-alpha)?\.gz$"
ANDROID_ARM64_PATTERN = r"^mihomo-android-arm64-v8-v[\d.]+(?:-alpha)?\.gz$"
ANDROID_ARMV7_PATTERN = r"^mihomo-android-armv7-v[\d.]+(?:-alpha)?\.gz$"
LINUX_ARM64_PATTERN = r"^mihomo-linux-arm64-v[\d.]+(?:-alpha)?\.gz$"

# 仓库内置资源名（assets/ 目录下）：按关键词匹配，自定义 MIHOMO_ASSET_PATTERN 也能命中
ASSET_KEYWORDS = (
    ("android-arm64", "mihomo-android-arm64.gz"),
    ("android-armv7", "mihomo-android-armv7.gz"),
    ("linux-arm64", "mihomo-linux-arm64.gz"),
    ("linux-amd64", "mihomo-linux-amd64.gz"),
)


def asset_name_for(pattern):
    for keyword, name in ASSET_KEYWORDS:
        if keyword in pattern:
            return name
    return None


def detect_pattern():
    """按当前平台挑发行包：Termux/Android 用 android-arm64，其它 arm64 用 linux-arm64。"""
    env = os.getenv("MIHOMO_ASSET_PATTERN")
    if env:
        return env
    machine = (os.uname().machine or "").lower()
    is_termux = "termux" in os.getenv("PREFIX", "") or os.path.exists("/system/bin")
    if is_termux:
        return ANDROID_ARM64_PATTERN if machine in ("aarch64", "arm64") else ANDROID_ARMV7_PATTERN
    if machine in ("aarch64", "arm64"):
        return LINUX_ARM64_PATTERN
    return DEFAULT_PATTERN


def install(blob, target):
    payload = gzip.decompress(blob)
    with open(target, "wb") as handle:
        handle.write(payload)
    os.chmod(target, os.stat(target).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print("已安装:", target, len(payload), "字节")
    try:
        print(subprocess.run([target, "-v"], capture_output=True, text=True).stdout.strip()[:120])
    except OSError:
        print("（当前宿主架构跑不了这个内核，属正常：装到目标机器上才能执行）")


def from_assets(target, pattern):
    """从本仓库 assets/ 取内置内核（走 api 通道）。"""
    name = asset_name_for(pattern)
    if not name:
        raise SystemExit("assets 里没有匹配 %s 的内置内核" % pattern)
    url = "https://api.github.com/repos/" + ASSET_REPO + "/contents/assets/" + name + "?ref=" + os.getenv("ASSET_REF", "main")
    print("从仓库内置资源下载:", name)
    request = urllib.request.Request(url, headers={"User-Agent": "free-vpn-clash-aggregator",
                                                   "Accept": "application/vnd.github.raw"})
    with urllib.request.urlopen(request, timeout=900) as response:
        install(response.read(), target)


def from_release(target, pattern):
    regex = re.compile(pattern)
    request = urllib.request.Request(API_LATEST, headers={"User-Agent": "free-vpn-clash-aggregator"})
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.loads(response.read())
    asset = next((item for item in release.get("assets", []) if regex.match(item["name"])), None)
    if not asset:
        names = [item["name"] for item in release.get("assets", [])]
        raise SystemExit("没找到匹配 %s 的资源，可选: %s" % (regex.pattern, names))
    print("从 Release 下载:", asset["name"], asset["size"], "字节")
    request = urllib.request.Request(asset["browser_download_url"],
                                     headers={"User-Agent": "free-vpn-clash-aggregator"})
    with urllib.request.urlopen(request, timeout=900) as response:
        install(response.read(), target)


def main():
    target = os.getenv("MIHOMO_BIN", "/usr/local/bin/mihomo")
    pattern = detect_pattern()
    print("平台:", (os.uname().machine or "?"), "| 资源匹配:", pattern)
    if "--from-assets" in sys.argv:
        from_assets(target, pattern)
    else:
        from_release(target, pattern)
    return 0


if __name__ == "__main__":
    sys.exit(main())
