"""取每个节点的真实出口 IP / 国家：判断"能不能用"之外，还要知道"出口在哪"。

做法：给每个节点开一个本地入口（mihomo 的 ``listeners`` 支持把某个入口绑到某个节点），
然后从那个入口去访问回显服务。这样可以在一个实例里并发测很多节点，不用串行切换选中项。

用途：
  * 淘汰"能连但出口在国内"的节点（例如标着 🇨🇳 的节点，连得上也翻不了墙）；
  * 按出口 IP 去重 —— 很多"不同"节点其实共用同一个出口；
  * 出口位置与你在哪里测无关（节点在哪出去就是哪），所以 CI 和本机测的结果一致。
"""

from __future__ import annotations

import collections
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from healthcheck import LOOPBACK, find_core

# 回显服务（按顺序尝试）：域名拼接写法，避免工具在解析配置前预解析域名
ECHO_ENDPOINTS = ['https://www.cloudflare.com/cdn-cgi/trace', 'http://ip-api.com/json/?fields=query,countryCode,country']

DEFAULT_BLOCKED_COUNTRIES = ("CN",)
BATCH_SIZE = int(os.getenv("EXIT_BATCH", "48"))


def _free_port():
    with socket.socket() as probe:
        probe.bind(("", 0))
        return probe.getsockname()[1]


def _parse_echo(body):
    """解析回显内容，返回 (ip, country)。支持 cloudflare trace / ip-api 两种格式。"""
    text = body.strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            return None, None
        return data.get("query") or data.get("ip"), (data.get("countryCode") or data.get("country") or "").upper() or None
    info = {}
    for line in text.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            info[key.strip()] = value.strip()
    return info.get("ip"), (info.get("loc") or "").upper() or None


def _fetch_exit(port, timeout):
    proxy = "http://" + LOOPBACK + ":" + str(port)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    for endpoint in ECHO_ENDPOINTS:
        try:
            with opener.open(endpoint, timeout=timeout) as response:
                return _parse_echo(response.read().decode("utf-8", "replace"))
        except Exception:
            continue
    return None, None


def check_exits(proxies, core=None, concurrency=16, timeout_ms=8000, batch_size=BATCH_SIZE, workdir=None):
    """返回 {name: {"ip":..., "country":...}}，测不出来的节点值为 None。"""
    core = find_core(core)
    if not core:
        raise RuntimeError("找不到 mihomo 内核，设置 MIHOMO_BIN 或安装 mihomo")
    timeout = timeout_ms / 1000.0
    results = {}
    workdir = workdir or tempfile.mkdtemp(prefix="exitcheck-")
    os.makedirs(workdir, exist_ok=True)
    import yaml

    try:
        for start in range(0, len(proxies), batch_size):
            chunk = proxies[start:start + batch_size]
            ports = {proxy["name"]: _free_port() for proxy in chunk}
            config = {
                "log-level": "warning",
                "mode": "rule",
                "ipv6": False,
                "dns": {"enable": True, "ipv6": False, "nameserver": ["223.5.5.5", "119.29.29.29"]},
                "proxies": chunk,
                "proxy-groups": [{"name": "PROBE", "type": "select", "proxies": [p["name"] for p in chunk]}],
                "listeners": [
                    {"name": "in-%d" % index, "type": "mixed", "port": ports[proxy["name"]],
                     "listen": LOOPBACK, "proxy": proxy["name"]}
                    for index, proxy in enumerate(chunk)
                ],
                "rules": ["MATCH,DIRECT"],
            }
            config_path = os.path.join(workdir, "exit-config.yaml")
            with open(config_path, "w", encoding="utf-8") as handle:
                yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)
            log_path = os.path.join(workdir, "exit-core.log")
            log = open(log_path, "w", encoding="utf-8")
            process = subprocess.Popen([core, "-d", workdir, "-f", config_path],
                                       stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.time() + 20
                ready = False
                while time.time() < deadline:
                    if process.poll() is not None:
                        break
                    try:
                        with socket.create_connection((LOOPBACK, ports[chunk[0]["name"]]), timeout=1):
                            ready = True
                            break
                    except Exception:
                        time.sleep(0.3)
                if not ready and process.poll() is not None:
                    raise RuntimeError("核心没起来：%s" % open(log_path, encoding="utf-8").read()[-300:])

                def probe(proxy):
                    ip, country = _fetch_exit(ports[proxy["name"]], timeout)
                    return proxy["name"], ({"ip": ip, "country": country} if ip else None)

                with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
                    for name, outcome in pool.map(probe, chunk):
                        results[name] = outcome
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                log.close()
        return results
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def summarize(results):
    countries = collections.Counter()
    ips = set()
    resolved = 0
    for outcome in results.values():
        if outcome:
            resolved += 1
            ips.add(outcome["ip"])
            countries[outcome.get("country") or "??"] += 1
    return {
        "tested": len(results),
        "resolved": resolved,
        "failed": len(results) - resolved,
        "unique_exit_ips": len(ips),
        "by_country": dict(countries.most_common()),
    }


def dedupe_by_exit(entries, exits, block_countries=DEFAULT_BLOCKED_COUNTRIES):
    """``entries`` 需按延迟升序。丢掉出口在封锁名单里的节点，并按出口 IP 去重。"""
    blocked = {code.upper() for code in block_countries if code}
    kept, seen_ips, dropped_country, dropped_dupe = [], set(), 0, 0
    for entry in entries:
        outcome = exits.get(entry["name"])
        country = (outcome or {}).get("country") or ""
        if outcome and country in blocked:
            dropped_country += 1
            continue
        ip = (outcome or {}).get("ip")
        if ip and ip in seen_ips:
            dropped_dupe += 1
            continue
        if ip:
            seen_ips.add(ip)
        kept.append({**entry, "exit_ip": ip, "exit_country": country})
    return kept, {"dropped_country": dropped_country, "dropped_duplicate_exit": dropped_dupe,
                  "blocked_countries": sorted(blocked)}


if __name__ == "__main__":
    import sys
    import yaml

    path = sys.argv[1] if len(sys.argv) > 1 else "output/clash.yaml"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    document = yaml.safe_load(open(path, encoding="utf-8"))
    subset = document["proxies"][:limit]
    outcomes = check_exits(subset, concurrency=16)
    print(json.dumps(summarize(outcomes), ensure_ascii=False, indent=2))
    for name, outcome in list(outcomes.items())[:15]:
        print("  %-40s %s" % (name[:40], outcome if outcome else "FAIL"))
