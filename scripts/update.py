from __future__ import annotations

import ipaddress
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "sources.yaml"
OUTPUT = ROOT / "output" / os.getenv("OUTPUT_NAME", "clash.yaml")  # CI 用 clash-ci.yaml，避免与本机产物打架
STATUS = ROOT / "output" / "source-status.json"

MAX_NODES = int(os.getenv("MAX_NODES", "400"))
PER_SOURCE_LIMIT = int(os.getenv("PER_SOURCE_LIMIT", "150"))
FETCH_TIMEOUT = int(os.getenv("FETCH_TIMEOUT", "20"))
FETCH_RETRIES = int(os.getenv("FETCH_RETRIES", "3"))
MIXED_PORT = int(os.getenv("MIXED_PORT", "7890"))
UA = "free-vpn-clash-aggregator/2.0"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from exit_check import check_exits, dedupe_by_exit, summarize  # noqa: E402  （同目录模块）
from healthcheck import DEFAULT_TEST_URL, measure, set_system_tun, system_tun_state  # noqa: E402
from sub_convert import convert  # noqa: E402  （把分享链接订阅转成 Clash 节点）

# 可用性实测开关：默认关；CI 与本地脚本会打开（见 README「可用性实测」）
HEALTH_CHECK = os.getenv("HEALTH_CHECK", "0").lower() not in ("", "0", "false", "no")
HEALTH_TIMEOUT_MS = int(os.getenv("HEALTH_TIMEOUT_MS", "5000"))
HEALTH_CONCURRENCY = int(os.getenv("HEALTH_CONCURRENCY", "16"))
HEALTH_MAX_TEST = int(os.getenv("HEALTH_MAX_TEST", "1200"))
HEALTH_MIN_ALIVE = int(os.getenv("HEALTH_MIN_ALIVE", "60"))

# 出口检测：淘汰出口在国内的节点，并按出口 IP 去重（出口位置与在哪测无关，CI 也能做）
EXIT_CHECK = os.getenv("EXIT_CHECK", "0").lower() not in ("", "0", "false", "no")
EXIT_TIMEOUT_MS = int(os.getenv("EXIT_TIMEOUT_MS", "8000"))
EXIT_BLOCK_COUNTRIES = tuple(code.strip().upper() for code in os.getenv("EXIT_BLOCK_COUNTRIES", "CN").split(",") if code.strip())

# mihomo 支持的协议 -> 必填字段（任一为空即丢弃；脏节点会让整份订阅在客户端加载失败）
REQUIRED = {
    "ss": ("cipher", "password"),
    "ssr": ("cipher", "password", "protocol"),
    "vmess": ("uuid",),
    "vless": ("uuid",),
    "trojan": ("password",),
    "hysteria": ("auth-str", "auth_str"),
    "hysteria2": ("password",),
    "tuic": ("uuid", "password"),
    "snell": ("psk",),
    "anytls": ("password",),
    "wireguard": ("private-key",),
    "http": (),
    "socks5": (),
    "ssh": ("private-key",),
}

# 参与去重指纹的字段：这些也一致才算同一个节点（否则同服务器不同伪装参数会被误判重复）
FINGERPRINT_FIELDS = (
    "uuid", "password", "public-key", "private-key", "psk", "token",
    "cipher", "network", "servername", "sni", "path", "host", "ws-opts", "grpc-opts",
    "reality-opts", "flow", "obfs", "obfs-param", "plugin", "plugin-opts", "alpn",
    "auth-str", "auth_str", "up", "down", "ports",
)


def _ip(*octets):
    return ".".join(str(part) for part in octets)


# 私网/保留网段一律直连（否则内网、校园平台、Tailscale 都会被丢到境外节点）
PRIVATE_CIDRS = [
    (_ip(0, 0, 0, 0), 8),
    (_ip(127, 0, 0, 0), 8),
    (_ip(10, 0, 0, 0), 8),
    (_ip(172, 16, 0, 0), 12),
    (_ip(192, 168, 0, 0), 16),
    (_ip(169, 254, 0, 0), 16),
    (_ip(100, 64, 0, 0), 10),
    (_ip(224, 0, 0, 0), 4),
]

DNS_SECTION = {
    "enable": True,
    "ipv6": False,
    "enhanced-mode": "fake-ip",
    "fake-ip-range": _ip(198, 18, 0, 1) + "/16",
    "fake-ip-filter": ["*.lan", "*.local", "time.*.com", "time.*.apple.com"],
}

# 分流规则：内网直连 -> 广告拦截 -> 国内直连 -> 兜底走节点
# 在本机直连比走节点更快更稳的域名（校园网实测 GitHub 直连 0.3-0.8s）
DIRECT_DOMAINS = [
    "github.com",
    "githubusercontent.com",
    # LLM API：经节点反而慢、易断流
    "deepseek.com",
]

RULES = (
    ["DOMAIN-SUFFIX,local,DIRECT"]
    + ["DOMAIN-SUFFIX," + x + ",DIRECT" for x in DIRECT_DOMAINS]
    + ["IP-CIDR," + cidr + "/" + str(prefix) + ",DIRECT,no-resolve" for cidr, prefix in PRIVATE_CIDRS]
    + ["GEOSITE,category-ads-all,REJECT", "GEOSITE,cn,DIRECT", "GEOIP,CN,DIRECT", "MATCH,PROXY"]
)


def fetch_text(url: str) -> str:
    """取原始文本（分享链接订阅用）。"""
    last_error = None
    for attempt in range(FETCH_RETRIES):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
                return response.read().decode("utf-8", "ignore")
        except Exception as exc:
            last_error = exc
            if attempt + 1 < FETCH_RETRIES:
                time.sleep(1.5 * (attempt + 1))
    raise last_error if last_error else RuntimeError("fetch failed")


def clean_text(value):
    """清掉不可见字符（C0/C1 控制符、DEL、零宽字符等）。

    上游节点里确实混着这类脏数据（实测有节点的 sni 里带私有区字符和乱码），
    而 go-yaml（mihomo 和客户端用的解析器）碰到控制字符会拒绝整份配置：
    "control characters are not allowed"。所以每个字符串字段都要洗，不只是节点名。
    """
    if not isinstance(value, str):
        return value
    return "".join(ch for ch in value if ch.isprintable())


# 这些字段就算为空也保留；其它字段为空值（None/空串）直接删掉 —— 上游常见 password: None / sni: '' 这类垃圾
KEEP_EMPTY_FIELDS = ("name", "server", "password", "uuid", "psk", "private-key")


def clean_proxy(proxy):
    """递归清洗一个节点字典：去掉控制字符、内部字段（__ 开头）、以及空值垃圾字段。"""
    cleaned = {}
    for key, value in proxy.items():
        if key.startswith("__"):
            continue
        if value is None:
            continue
        if isinstance(value, str):
            text = clean_text(value)
            if not text and key not in KEEP_EMPTY_FIELDS:
                continue
            cleaned[key] = text
        elif isinstance(value, list):
            cleaned[key] = [clean_text(item) if isinstance(item, str) else item for item in value]
        elif isinstance(value, dict):
            cleaned[key] = {sub_key: (clean_text(sub_value) if isinstance(sub_value, str) else sub_value)
                            for sub_key, sub_value in value.items()}
        else:
            cleaned[key] = value
    return cleaned


def fetch(url):
    last_error = None
    for attempt in range(FETCH_RETRIES):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
                body = response.read()
            parsed = yaml.safe_load(body) or {}
            if not isinstance(parsed, dict):
                raise ValueError("top-level YAML value is not a mapping")
            return parsed
        except Exception as exc:
            last_error = exc
            if attempt + 1 < FETCH_RETRIES:
                time.sleep(1.5 * (attempt + 1))
    raise last_error if last_error else RuntimeError("fetch failed")


def is_usable_server(server: str) -> bool:
    """拒掉保留地址（上游订阅里常有 127.0.0.0 这种公告占位节点）。"""
    try:
        address = ipaddress.ip_address(server)
    except ValueError:
        return "." in server
    return not (address.is_private or address.is_loopback or address.is_reserved
                or address.is_multicast or address.is_unspecified or address.is_link_local)


def is_blank(value):
    return value is None or (isinstance(value, str) and not value.strip())


def normalize(proxy):
    """校验 + 规范化一个节点。返回 (节点, None) 或 (None, 丢弃原因)。"""
    if not isinstance(proxy, dict):
        return None, "not-a-mapping"
    kind = str(proxy.get("type", "")).strip().lower()
    if kind not in REQUIRED:
        return None, "unsupported-type"
    server = str(proxy.get("server", "")).strip().lower()
    if not server:
        return None, "no-server"
    if not is_usable_server(server):
        return None, "reserved-server"
    try:
        port = int(str(proxy.get("port")).strip())
    except (TypeError, ValueError):
        return None, "bad-port"
    if not 0 < port < 65536:
        return None, "bad-port"
    fields = REQUIRED[kind]
    if fields and not any(not is_blank(proxy.get(key)) for key in fields):
        return None, "missing-" + fields[0]
    item = dict(proxy)
    item["type"] = kind
    item["server"] = server
    item["port"] = port
    name = str(proxy.get("name") or "").strip()
    if not name:
        name = kind + "-" + server + ":" + str(port)
    item["name"] = clean_text(name)[:120]
    item = clean_proxy(item)
    return item, None


def fingerprint(proxy):
    parts = [str(proxy.get("type", "")), str(proxy.get("server", "")), str(proxy.get("port", ""))]
    for field in FINGERPRINT_FIELDS:
        value = proxy.get(field)
        if value is None:
            continue
        parts.append(field + "=" + json.dumps(value, sort_keys=True, ensure_ascii=False, default=str))
    return "|".join(parts)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    config = yaml.safe_load(SOURCES.read_text(encoding="utf-8"))
    entries = config.get("sources", [])

    per_source = []
    status = []
    seen = set()
    for source in entries:
        name, url = source["name"], source["url"]
        record = {
            "name": name, "url": url, "ok": False, "received": 0, "added": 0,
            "dropped_invalid": 0, "dropped_duplicate": 0, "dropped_limit": 0,
        }
        try:
            fmt = str(source.get("format") or "clash").strip().lower()
            record["format"] = fmt
            if fmt == "clash":
                document = fetch(url)
                candidates = document.get("proxies", [])
                if not isinstance(candidates, list):
                    raise ValueError("proxies is not a list")
                record["received"] = len(candidates)
            else:
                # 分享链接订阅（整体 base64 或一行一条）：先转成 Clash 节点
                text = fetch_text(url)
                candidates, conv = convert(text)
                record["received"] = conv["lines"]
                record["converted"] = conv["parsed"]
                record["convert_failed"] = conv["failed"]
                record["unsupported_scheme"] = conv["unsupported"]
                if not candidates:
                    raise ValueError("no usable links in subscription")
            kept = []
            for proxy in candidates:
                item, reason = normalize(proxy)
                if item is None:
                    record["dropped_invalid"] += 1
                    continue
                key = fingerprint(item)
                if key in seen:
                    record["dropped_duplicate"] += 1
                    continue
                seen.add(key)
                kept.append(item)
            if len(kept) > PER_SOURCE_LIMIT:
                record["dropped_limit"] = len(kept) - PER_SOURCE_LIMIT
                kept = kept[:PER_SOURCE_LIMIT]
            record["ok"] = True
            record["added"] = len(kept)
            per_source.append(kept)
        except Exception as exc:
            record["error"] = str(exc)
        status.append(record)

    # 按源轮流填充：单源节点再多也只占 PER_SOURCE_LIMIT，避免少数上游垄断整个订阅
    merged = []
    cursor = 0
    merge_cap = max(MAX_NODES, HEALTH_MAX_TEST) if HEALTH_CHECK else MAX_NODES
    while len(merged) < merge_cap and any(cursor < len(items) for items in per_source):
        for items in per_source:
            if cursor < len(items) and len(merged) < merge_cap:
                merged.append(items[cursor])
        cursor += 1

    if not merged:
        raise RuntimeError("all upstream sources failed or returned no usable proxies")

    used_names = set()
    for proxy in merged:
        base = proxy["name"]
        candidate = base
        suffix = 2
        while candidate in used_names:
            candidate = base + " #" + str(suffix)
            suffix += 1
        used_names.add(candidate)
        proxy["name"] = candidate

    # 可用性实测：拿内核自己的 delay 接口真连一次，只留能用的
    health = None
    if HEALTH_CHECK:
        # 本机跑时先关 TUN（会抓走测试流量导致虚高）；CI 上没有这个 socket，自动跳过
        tun_was_on = system_tun_state()
        if tun_was_on:
            import atexit
            print("本机 TUN 开着：测速期间临时关闭", flush=True)
            set_system_tun(False)
            atexit.register(set_system_tun, True)
        candidates = merged[:HEALTH_MAX_TEST]
        print("健康检查：实测 %d 个节点（%d 并发，超时 %dms，目标 %s）"
              % (len(candidates), HEALTH_CONCURRENCY, HEALTH_TIMEOUT_MS, DEFAULT_TEST_URL), flush=True)
        report = measure(candidates, url=DEFAULT_TEST_URL, timeout_ms=HEALTH_TIMEOUT_MS,
                         concurrency=HEALTH_CONCURRENCY)
        health = {key: report[key] for key in (
            "core", "core_version", "test_url", "tested", "alive", "dead_timeout", "dead_error",
            "alive_ratio", "min_delay_ms", "median_delay_ms", "elapsed_s")}
        health["min_alive_threshold"] = HEALTH_MIN_ALIVE
        print("健康检查结果: %s" % json.dumps(
            {k: v for k, v in health.items() if k not in ("core", "test_url")}, ensure_ascii=False), flush=True)
        if report["alive"] >= HEALTH_MIN_ALIVE:
            alive_names = {item["name"] for item in report["alive_nodes"]}
            order = {item["name"]: index for index, item in enumerate(report["alive_nodes"])}
            merged = [proxy for proxy in candidates if proxy["name"] in alive_names]
            merged.sort(key=lambda proxy: order[proxy["name"]])
            health["applied"] = True
        else:
            health["applied"] = False
            health["note"] = ("可用节点只有 %d 个（低于 HEALTH_MIN_ALIVE=%d），保留未筛选列表"
                              % (report["alive"], HEALTH_MIN_ALIVE))
            print("警告:", health["note"], flush=True)
    # 出口检测（CI 也能做：出口在哪是节点的属性，和你从哪测无关）
    exit_stats = None
    if EXIT_CHECK and merged:
        targets = merged[:HEALTH_MAX_TEST]
        print("出口检测：%d 个节点各开一个本地入口取真实出口 IP…" % len(targets), flush=True)
        exits = check_exits(targets, concurrency=HEALTH_CONCURRENCY, timeout_ms=EXIT_TIMEOUT_MS)
        exit_stats = summarize(exits)
        entries = [{"name": proxy["name"], "delay": index, "type": proxy.get("type", "")}
                   for index, proxy in enumerate(targets)]
        entries, dedupe_stats = dedupe_by_exit(entries, exits, block_countries=EXIT_BLOCK_COUNTRIES)
        exit_stats.update(dedupe_stats)
        keep = {entry["name"] for entry in entries}
        merged = [proxy for proxy in targets if proxy["name"] in keep]
        exit_stats["kept"] = len(merged)
        print("出口筛选:", json.dumps(exit_stats, ensure_ascii=False), flush=True)

    if HEALTH_CHECK and 'tun_was_on' in locals() and tun_was_on:
        set_system_tun(True)
        print("已恢复 TUN（测速结束）", flush=True)
    merged = merged[:MAX_NODES]

    node_names = [proxy["name"] for proxy in merged]
    generated = {
        "mixed-port": MIXED_PORT,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        "ipv6": False,
        "unified-delay": True,
        "tcp-concurrent": True,
        "dns": DNS_SECTION,
        "proxies": merged,
        "proxy-groups": [
            {
                "name": "AUTO",
                "type": "url-test",
                "url": DEFAULT_TEST_URL,
                "interval": 300,
                "tolerance": 50,
                "lazy": True,
                "proxies": node_names,
            },
            {"name": "PROXY", "type": "select", "proxies": ["AUTO", "DIRECT"] + node_names},
        ],
        "rules": RULES,
    }

    text = "# Generated by scripts/update.py; do not edit.\n" + yaml.safe_dump(
        generated, allow_unicode=True, sort_keys=False, width=4096
    )

    # 写盘前自检：确认能解析回来且结构完整；不通过就抛错，保留上一版可用文件
    check = yaml.safe_load(text)
    if len(check.get("proxies", [])) != len(merged):
        raise RuntimeError("self-check failed: proxy count mismatch")
    if check.get("rules", [])[-1] != "MATCH,PROXY":
        raise RuntimeError("self-check failed: missing MATCH fallback")
    # 字节级自检：产物里绝不能有控制字符（客户端解析器会整份拒绝）
    illegal = sorted({hex(ord(ch)) for ch in text if ord(ch) < 0x20 and ch not in "\n\r\t"} | ({"0x7f"} if "\x7f" in text else set()))
    if illegal:
        raise RuntimeError("self-check failed: output has control characters %s" % illegal)

    OUTPUT.write_text(text, encoding="utf-8")
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "proxy_count": len(merged),
        "max_nodes": MAX_NODES,
        "per_source_limit": PER_SOURCE_LIMIT,
        "sources_ok": sum(1 for item in status if item["ok"]),
        "sources_failed": sum(1 for item in status if not item["ok"]),
        "dropped_invalid": sum(item["dropped_invalid"] for item in status),
        "dropped_duplicate": sum(item["dropped_duplicate"] for item in status),
        "dropped_limit": sum(item["dropped_limit"] for item in status),
        "rules_count": len(RULES),
        "health_check": health,
        "exit_check": exit_stats,
        "sources": status,
    }
    STATUS.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "sources"}, ensure_ascii=False))
    for item in status:
        print("  [%s] %-40s recv=%-5s add=%-4s invalid=%-4s dup=%-4s limit=%-4s %s" % (
            "ok " if item["ok"] else "FAIL", item["name"], item["received"], item["added"],
            item["dropped_invalid"], item["dropped_duplicate"], item["dropped_limit"], item.get("error", "")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
