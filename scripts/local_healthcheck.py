"""在本机实测订阅里每个节点能不能用（真实网络视角）。

为什么要有它：GitHub Actions 跑在境外机房，"能连"不等于你在国内校园网能用 ——
真正有效的可用性判断只能在**你自己的网络**上做，这个脚本就是干这个的。

用法：
    python3 scripts/local_healthcheck.py                    # 读本地 output/clash.yaml
    python3 scripts/local_healthcheck.py --from-repo        # 先取仓库里最新的 output/clash.yaml 再测
    python3 scripts/local_healthcheck.py --push             # 测完把 output/clash-cn.yaml 推回仓库（走 API）

令牌来源：环境变量 GITHUB_TOKEN；或凭据文件 ~/.git-credentials-hermes 里每行的「用户名:令牌」部分。
为什么推回要走 API：校园网下 git push 传 pack 会失败（index-pack failed），而 API 通道是通的。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from exit_check import check_exits, dedupe_by_exit, summarize  # noqa: E402
from update import clean_proxy  # noqa: E402  （复用同一套清洗：控制字符会让客户端拒绝整份配置）
from healthcheck import (DEFAULT_TEST_URL, active_heavy_download, find_core,  # noqa: E402
                         measure, set_system_tun, system_tun_state, tun_bypass_config,  # noqa: E402
                         wait_tun_effective)

DEFAULT_REPO = "Phirisyyds/sysu-clash-sub"
DEFAULT_BRANCH = "main"
SOURCE_PATH = "output/clash.yaml"
TARGET_PATH = "output/clash-cn.yaml"
TOKEN_FILE = os.path.expanduser("~/.git-credentials-hermes")
MIHOMO_SOCKET = "/tmp/verge/verge-mihomo.sock"
API_BASE = "https://api.github.com"
API_HOST = "api.github.com"


def read_token():
    token = (os.getenv("GITHUB_TOKEN") or "").strip()
    if token:
        return token
    if os.path.exists(TOKEN_FILE):
        for line in open(TOKEN_FILE, encoding="utf-8"):
            if "@" in line and "://" in line:
                credentials = line.split("//", 1)[1].split("@", 1)[0]
                if ":" in credentials:
                    return credentials.split(":", 1)[1].strip()
    return ""


def patch_dns_for_github():
    """本机 /etc/hosts 若把接口域名钉到别的 IP，就改用 mihomo 核心里查到的地址。"""
    resolved = []
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(6)
        client.connect(MIHOMO_SOCKET)
        client.sendall(("GET /dns/query?name=" + API_HOST + "&type=A HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n").encode())
        raw = b""
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            raw += chunk
        client.close()
        body = raw.partition(b"\r\n\r\n")[2]
        if b"chunked" in raw.lower():
            out, rest = b"", body
            while True:
                line, _, rest = rest.partition(b"\r\n")
                size = int(line.strip().split(b";")[0], 16)
                if size == 0:
                    break
                out += rest[:size]
                rest = rest[size + 2:]
            body = out
        resolved = [answer["data"] for answer in json.loads(body).get("Answer", []) if answer.get("type") == 1]
    except Exception:
        return None
    if not resolved:
        return None
    # 核心里查到的地址未必能连（校园网 DNS 会给被封的 GitHub IP）：
    # 逐个 TCP 443 探活，全都连不上就别接管 —— 让系统解析走 Clash 的 hosts 钉好的可用 IP。
    reachable = []
    probe_ctx = ssl.create_default_context()
    probe_ctx.check_hostname = False
    probe_ctx.verify_mode = ssl.CERT_NONE
    for ip in resolved:
        sock = None
        try:
            sock = socket.create_connection((ip, 443), timeout=4)
            probe_ctx.wrap_socket(sock, server_hostname=API_HOST).close()   # TLS 握手才算通
            reachable.append(ip)
        except Exception:
            pass
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
    if not reachable:
        return None
    resolved = reachable
    original = socket.getaddrinfo

    def patched(name, port, family=0, kind=0, proto=0, flags=0):
        if name == API_HOST:
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port)) for ip in resolved]
        return original(name, port, family, kind, proto, flags)

    socket.getaddrinfo = patched
    return resolved


def github(path, method="GET", payload=None, token=""):
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "free-vpn-clash-aggregator", "Content-Type": "application/json"}
    if token:  # 公开仓库读取不需要令牌；只有推送（以及想避开匿名限流）才带
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(API_BASE + path, method=method,
        data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise RuntimeError("API %s %s -> %s %s" % (method, path, exc.code,
                                                       exc.read().decode("utf-8", "replace")[:200]))
        except Exception as exc:   # TLS 被掐断/瞬时超时：退避重试，别让整轮测速白跑
            last = exc
            print("  推送通道抖动（%s），%.0fs 后重试 %d/3" % (type(exc).__name__, 2 * (attempt + 1), attempt + 1))
            time.sleep(2 * (attempt + 1))
    raise last


def fetch_remote_config(repo, branch, token):
    data = github("/repos/" + repo + "/contents/" + SOURCE_PATH + "?ref=" + urllib.parse.quote(branch), token=token)
    return yaml.safe_load(base64.b64decode(data["content"]))


def push_text(repo, branch, token, text, message, path=TARGET_PATH):
    try:
        current = github("/repos/" + repo + "/contents/" + path + "?ref=" + urllib.parse.quote(branch), token=token)
    except RuntimeError as exc:
        if "-> 404" not in str(exc):
            raise
        current = None  # 目标文件还不存在 = 新建
    payload = {"message": message, "content": base64.b64encode(text.encode("utf-8")).decode(), "branch": branch}
    if isinstance(current, dict) and current.get("sha"):
        payload["sha"] = current["sha"]
    result = github("/repos/" + repo + "/contents/" + path, "PUT", payload, token)
    return (result.get("commit") or {}).get("sha", "")


def build_lite(document, kept, entries, per_country=3, max_countries=5, max_nodes=18):
    """生成手机友好的精简配置：按出口国家分组，每组只留最快的几个。

    手机端（FlClash 等）没必要塞一百多个节点：一次 url-test 扫全部会又慢又费电，
    而且手机所在网络和电脑不同，能用的本来就更少。给一份"每组最快几条"的短名单更实用。
    """
    by_country = {}
    for proxy, entry in zip(kept, entries):
        country = entry.get("exit_country") or "??"
        by_country.setdefault(country, []).append(proxy)
    ordered = sorted(by_country.items(), key=lambda item: min(p.get("__delay", 0) for p in item[1]))
    selected, groups, summary = [], [], []
    for country, proxies in ordered[:max_countries]:
        picked = proxies[:per_country]
        if len(selected) + len(picked) > max_nodes:
            picked = picked[:max_nodes - len(selected)]
        if not picked:
            break
        selected.extend({key: value for key, value in proxy.items() if not key.startswith("__")} for proxy in picked)
        names = [proxy["name"] for proxy in picked]
        label = "未知出口" if country in ("??", "", None) else country
        groups.append({"name": label + " 节点", "type": "select", "proxies": names})
        summary.append("%s×%d" % ("未知" if country in ("??", "", None) else country, len(picked)))
    lite = {
        "mixed-port": document.get("mixed-port", 7890),
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        "ipv6": False,
        "unified-delay": True,
        "tcp-concurrent": True,
        "dns": document.get("dns"),
        "proxies": selected,
        "proxy-groups": [
            {"name": "PROXY", "type": "select",
             "proxies": ["AUTO"] + [group["name"] for group in groups] + ["DIRECT"]},
            {"name": "AUTO", "type": "url-test", "url": DEFAULT_TEST_URL, "interval": 300,
             "tolerance": 50, "lazy": True, "proxies": [proxy["name"] for proxy in selected]},
        ] + groups,
        "rules": document.get("rules"),
    }
    header = ("# 手机精简版（由 scripts/local_healthcheck.py 生成）：按出口国家分组，每组只留最快几条\n"
              "# 组成: %s | 共 %d 个节点\n" % ("、".join(summary), len(selected)))
    return header + yaml.safe_dump(lite, allow_unicode=True, sort_keys=False, width=4096)


def main():
    parser = argparse.ArgumentParser(description="本机实测节点可用性并生成筛选后的配置")
    parser.add_argument("--input", default="output/clash.yaml", help="输入配置（默认 output/clash.yaml）")
    parser.add_argument("--output", default=TARGET_PATH, help="输出配置（默认 output/clash-cn.yaml）")
    parser.add_argument("--from-repo", action="store_true", help="先取仓库里最新的 output/clash.yaml 当输入")
    parser.add_argument("--push", action="store_true", help="把结果推回仓库（需要令牌）")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--branch", default=DEFAULT_BRANCH)
    parser.add_argument("--core", default=None, help="mihomo 内核路径（默认自动找/MIHOMO_BIN）")
    parser.add_argument("--concurrency", type=int, default=int(os.getenv("HEALTH_CONCURRENCY", "24")))
    parser.add_argument("--timeout", type=int, default=int(os.getenv("HEALTH_TIMEOUT_MS", "5000")))
    parser.add_argument("--no-latency-tag", action="store_true", help="不在节点名前加延迟前缀")
    parser.add_argument("--keep-tun", action="store_true", help="不临时关闭本机 TUN（结果会虚高，仅调试用）")
    parser.add_argument("--push-candidates", action="store_true",
                        help="顺带把候选表 output/clash.yaml 与 source-status.json 也推上去（本机聚合模式用）")
    parser.add_argument("--toggle-tun", action="store_true",
                        help="强制使用旧的\"测速期间临时关 TUN\"方式（默认：能探测到物理出口就绑源地址绕过 TUN）")
    parser.add_argument("--no-exit-check", action="store_true", help="跳过出口 IP 检测（更快，但会保留出口在国内的节点）")
    parser.add_argument("--exit-timeout", type=int, default=int(os.getenv("EXIT_TIMEOUT_MS", "8000")), help="出口检测单节点超时（毫秒）")
    parser.add_argument("--lite-output", default="output/clash-lite.yaml", help="手机精简版输出路径")
    parser.add_argument("--lite-per-country", type=int, default=int(os.getenv("LITE_PER_COUNTRY", "3")), help="精简版每国留几个节点")
    parser.add_argument("--lite-max-countries", type=int, default=int(os.getenv("LITE_MAX_COUNTRIES", "5")), help="精简版最多几个国家组")
    parser.add_argument("--lite-max-nodes", type=int, default=int(os.getenv("LITE_MAX_NODES", "18")), help="精简版总节点上限")
    parser.add_argument("--block-countries", default=os.getenv("EXIT_BLOCK_COUNTRIES", "CN"), help="要淘汰的出口国家（逗号分隔，默认 CN）")
    args = parser.parse_args()

    token = read_token() if args.push else ""
    if args.push and not token:
        raise SystemExit("推送需要令牌：设置 GITHUB_TOKEN，或写入 ~/.git-credentials-hermes")
    resolved = patch_dns_for_github()  # 本机 /etc/hosts 钉死接口域名时绕过；手机上会自然跳过
    if resolved:
        print("接口域名解析已接管:", resolved, flush=True)
    if args.from_repo and not token:
        print("（未提供令牌：以匿名方式读取公开仓库，够用）", flush=True)

    if args.from_repo:
        print("从仓库取最新节点列表: %s@%s" % (args.repo, args.branch), flush=True)
        document = fetch_remote_config(args.repo, args.branch, token)
    else:
        if not os.path.exists(args.input):
            raise SystemExit("找不到输入文件: %s" % args.input)
        document = yaml.safe_load(open(args.input, encoding="utf-8"))
    proxies = document.get("proxies") or []
    if not proxies:
        raise SystemExit("输入里没有 proxies")

    core = find_core(args.core)
    if not core:
        raise SystemExit("找不到 mihomo 内核：装一个 mihomo，或设置 MIHOMO_BIN，或用 --core 指定")
    print("内核: %s | 待测: %d 个节点（%d 并发，单节点超时 %dms）"
          % (core, len(proxies), args.concurrency, args.timeout), flush=True)

    # 让测试实例的出站绕开本机 TUN，两条路：
    #   1) 内核带 CAP_NET_ADMIN → 打 fwmark 直接走物理网卡（最优，全程不动 TUN）；
    #   2) 没有能力 → 回退旧的"测速期间临时关 TUN"，但先检查有没有正在下载，有就跳过本轮。
    bypass = {} if args.toggle_tun else tun_bypass_config(core)
    tun_was_on = None
    if bypass:
        print("TUN 绕过已启用（%s）：测速不会影响正在走代理的下载" % bypass, flush=True)
    elif not args.keep_tun:
        busy, why = active_heavy_download()
        if busy:
            print("检测到正在下载（%s）：本轮跳过，避免关 TUN 打断传输" % why, flush=True)
            raise SystemExit(0)
        tun_was_on = system_tun_state()
        if tun_was_on:
            import atexit
            print("未启用 TUN 绕过；本机 TUN 开着：测速期间临时关闭（否则结果虚高）", flush=True)
            set_system_tun(False)
            if wait_tun_effective(expect_enabled=False):
                print("TUN 已关闭且路由生效，开始测速", flush=True)
            else:
                print("警告：TUN 关闭后路由未在 6 秒内生效，结果可能虚高", flush=True)
            atexit.register(set_system_tun, True)

    report = measure(proxies, core=core, url=DEFAULT_TEST_URL, timeout_ms=args.timeout,
                     concurrency=args.concurrency)
    print(json.dumps({key: value for key, value in report.items() if key not in ("alive_nodes", "failures")},
                     ensure_ascii=False, indent=2), flush=True)
    alive = report["alive_nodes"]
    if not alive:
        raise SystemExit("没有测出可用节点（检查本机网络/内核）")
    if bypass and len(alive) < max(3, int(len(proxies) * 0.02)):
        raise SystemExit("启用了 TUN 绕过但几乎全灭（%d/%d）—— 疑似 routing-mark 被内核拒绝，"
                         "本轮不写产物；请检查 getcap 或改用 --toggle-tun" % (len(alive), len(proxies)))

    by_name = {proxy["name"]: proxy for proxy in proxies}

    # 出口检测：淘汰"能连但出口在国内"的节点，并按出口 IP 去掉共用同一出口的重复项
    entries = list(alive)
    exit_stats = None
    if not args.no_exit_check:
        targets = [by_name[item["name"]] for item in entries]
        print("\n出口检测：%d 个可用节点各开一个本地入口取真实出口 IP…" % len(targets), flush=True)
        exits = check_exits(targets, core=core, concurrency=args.concurrency, timeout_ms=args.exit_timeout)
        exit_stats = summarize(exits)
        print(json.dumps(exit_stats, ensure_ascii=False, indent=2), flush=True)
        blocked = [code.strip().upper() for code in args.block_countries.split(",") if code.strip()]
        entries, dedupe_stats = dedupe_by_exit(entries, exits, block_countries=blocked)
        exit_stats.update(dedupe_stats)
        print("出口筛选:", json.dumps(dedupe_stats, ensure_ascii=False), flush=True)

    # 测速结束后立刻恢复 TUN —— 后面的推送/下载还要走代理，否则在校园网里会连不上
    if tun_was_on:
        set_system_tun(True)
        if not wait_tun_effective(expect_enabled=True):
            print("警告：TUN 恢复后路由未在 6 秒内生效", flush=True)
        print("已恢复 TUN（测速结束）", flush=True)

    kept = []
    for item in entries:
        proxy = dict(by_name[item["name"]])
        proxy["__delay"] = item["delay"]
        if not args.no_latency_tag:
            tag = "[%d ms %s]" % (item["delay"], item["exit_country"]) if item.get("exit_country") else "[%d ms]" % item["delay"]
            proxy["name"] = (tag + " " + proxy["name"])[:120]
        kept.append(proxy)
    names = [proxy["name"] for proxy in kept]

    # 洗一遍所有字符串字段：控制字符会让客户端拒绝整份配置；顺便去掉 __delay 等内部字段
    kept = [clean_proxy(proxy) for proxy in kept]
    document["proxies"] = kept
    document["proxy-groups"] = [
        {"name": "AUTO", "type": "url-test", "url": DEFAULT_TEST_URL, "interval": 300,
         "tolerance": 50, "lazy": True, "proxies": names},
        {"name": "PROXY", "type": "select", "proxies": ["AUTO", "DIRECT"] + names},
    ]
    document.pop("proxy-providers", None)
    header = ("# 由 scripts/local_healthcheck.py 在本机实测生成：真能连上 + 出口不在封锁区 + 按出口去重，按延迟升序。\n"
              "# 原始可用 %d/%d | 中位延迟 %s ms | 内核 %s\n"
              % (report["alive"], report["tested"], report.get("median_delay_ms"), report.get("core_version", "")))
    if exit_stats:
        header += ("# 出口国家分布: %s | 唯一出口 IP %d 个 | 淘汰国内出口 %d 个 | 出口重复 %d 个\n"
                   % (json.dumps(exit_stats.get("by_country", {}), ensure_ascii=False),
                      exit_stats.get("unique_exit_ips", 0), exit_stats.get("dropped_country", 0),
                      exit_stats.get("dropped_duplicate_exit", 0)))
    text = header + yaml.safe_dump(document, allow_unicode=True, sort_keys=False, width=4096)
    illegal = sorted({hex(ord(ch)) for ch in text if ord(ch) < 0x20 and ch not in "\n\r\t"})
    if illegal:
        raise SystemExit("产物里出现控制字符 %s，已中止写入" % illegal)
    with open(args.output, "w", encoding="utf-8") as handle:
        handle.write(text)
    print("\n已写入 %s（%d 个节点）" % (args.output, len(kept)), flush=True)
    if exit_stats:
        sidecar = os.path.splitext(args.output)[0] + "-exits.json"
        with open(sidecar, "w", encoding="utf-8") as handle:
            json.dump({"generated_at": __import__("datetime").datetime.now().astimezone().isoformat(),
                       "summary": exit_stats,
                       "exits": {item["name"]: {"ip": item.get("exit_ip"), "country": item.get("exit_country"),
                                                "delay_ms": item["delay"]} for item in entries}},
                      handle, ensure_ascii=False, indent=2)
        print("出口明细已写入 %s" % sidecar, flush=True)
    for item in entries[:12]:
        country = item.get("exit_country") or "??"
        print("   %6d ms  %-3s %s" % (item["delay"], country, item["name"][:52]))
    lite_path = args.lite_output
    lite_text = build_lite(document, kept, entries, per_country=args.lite_per_country, max_countries=args.lite_max_countries, max_nodes=args.lite_max_nodes)
    with open(lite_path, "w", encoding="utf-8") as handle:
        handle.write(lite_text)
    print("已写入 %s（手机精简版）" % lite_path, flush=True)

    if args.push:
        commit = push_text(args.repo, args.branch, token, text,
                           "chore: 本机实测可用节点（%d/%d 可用，中位 %s ms）"
                           % (report["alive"], report["tested"], report.get("median_delay_ms")))
        print("\n已推送到 %s@%s: %s" % (args.repo, args.branch, commit[:10]), flush=True)
        lite_commit = push_text(args.repo, args.branch, token, lite_text,
                                "chore: 手机精简版（按出口国家分组）", path=lite_path)
        print("精简版已推送: %s" % lite_commit[:10], flush=True)
        if args.push_candidates:
            for extra, label in (("output/clash.yaml", "候选表"), ("output/source-status.json", "源状态")):
                if not os.path.exists(extra):
                    continue
                try:
                    sha = push_text(args.repo, args.branch, token, open(extra, encoding="utf-8").read(),
                                    "chore: 本机聚合刷新（%s）" % label, path=extra)
                    print("%s已推送: %s" % (label, sha[:10]), flush=True)
                except Exception as exc:
                    print("%s推送失败: %s" % (label, exc), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
