"""节点可用性实测：借 mihomo 内核自己的 ``/proxies/<name>/delay`` 接口，逐节点真连一次。

为什么用内核而不是手写协议探测：ss/vmess/vless/trojan/hysteria2 的握手各不相同，
手写等于重实现一个客户端；内核和 Clash Verge 用的就是同一套实现，测出来的结果与客户端一致。

视角说明（重要）：
  * 在 GitHub Actions 上跑 = 机房（境外）视角，只能剔除"彻底死掉"的节点；
  * 想知道 **你所在网络**（例如国内校园网）能不能用，就在本机跑
    ``scripts/local_healthcheck.py`` —— 那才是你真实使用时的视角。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# 测速目标：返回 204 的轻量地址（写成拼接形式，避免某些工具在解析配置前预解析域名）
DEFAULT_TEST_URL = "http://www.gstatic.com/generate_204"

CORE_NAMES = ("mihomo", "verge-mihomo", "clash-meta", "clash.meta")
CORE_PATHS = (
    "~/.local/share/io.github.clash-verge-rev.clash-verge-rev/verge-mihomo",
    "/usr/local/bin/mihomo",
    "/usr/bin/verge-mihomo",
)
LOOPBACK = "127.0.0.1"


# 本机开着 Clash 的 TUN 时，测试实例的出站会被 TUN 抓走、绕经代理 —— 实测同一批节点
# TUN 开 22/40 可用、TUN 关 7/40，虚高约 3 倍。所以测速期间要临时关掉 TUN。
DEFAULT_API_SOCKET = os.getenv("MIHOMO_API_SOCKET", "/tmp/verge/verge-mihomo.sock")


def _api(socket_path, method, path, payload=None, timeout=20):
    body = json.dumps(payload).encode() if payload is not None else b""
    request = ("%s %s HTTP/1.1\r\nHost: x\r\nConnection: close\r\n"
               "Content-Type: application/json\r\nContent-Length: %d\r\n\r\n" % (method, path, len(body))).encode() + body
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    client.connect(socket_path)
    client.sendall(request)
    raw = b""
    while True:
        chunk = client.recv(65536)
        if not chunk:
            break
        raw += chunk
    client.close()
    head, _, payload_bytes = raw.partition(b"\r\n\r\n")
    if b"chunked" in head.lower():
        out, rest = b"", payload_bytes
        while True:
            line, _, rest = rest.partition(b"\r\n")
            try:
                size = int(line.strip().split(b";")[0], 16)
            except Exception:
                break
            if size == 0:
                break
            out += rest[:size]
            rest = rest[size + 2:]
        payload_bytes = out
    try:
        return json.loads(payload_bytes or b"{}")
    except Exception:
        return {}


def detect_bind_ip():
    """返回物理出口网卡的 IPv4，用于让测试实例绕开本机 TUN 直连。

    原理：本机 TUN 的 ip rule 只把 **未绑定源地址** 的本地流量（``from 0.0.0.0 iif lo``）
    送进 TUN；一旦把 socket 源地址绑到物理网卡 IP，规则不匹配、直接落到主路由表走网卡，
    于是"直连口径"不再需要临时关掉 TUN（关 TUN 会打断正在走代理的下载）。

    返回 None 表示探测失败/被禁用（调用方应回退到旧的"临时关 TUN"）。
    ``HERMES_HEALTHCHECK_BIND=0`` 关闭；``HERMES_HEALTHCHECK_BIND_IP=1.2.3.4`` 指定。
    """
    if os.environ.get("HERMES_HEALTHCHECK_BIND", "auto").lower() in ("0", "off", "no", "false"):
        return None
    explicit = os.environ.get("HERMES_HEALTHCHECK_BIND_IP", "").strip()
    if explicit:
        return explicit
    skip_prefix = ("tun", "utun", "Meta", "tailscale", "docker", "br-", "virbr", "lo")
    try:
        routes = subprocess.run(["ip", "-4", "route", "show", "default", "table", "main"],
                                capture_output=True, text=True, timeout=5).stdout
        for line in routes.splitlines():
            parts = line.split()
            if "dev" not in parts:
                continue
            dev = parts[parts.index("dev") + 1]
            if dev.startswith(skip_prefix):
                continue
            addr = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", dev],
                                  capture_output=True, text=True, timeout=5).stdout
            match = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+)", addr)
            if match:
                return match.group(1)
    except Exception:
        return None
    return None


def detect_egress():
    """返回物理出口 (IPv4, 网卡名)；探测失败返回 (None, None)。"""
    skip_prefix = ("tun", "utun", "Meta", "tailscale", "docker", "br-", "virbr", "lo")
    try:
        routes = subprocess.run(["ip", "-4", "route", "show", "default", "table", "main"],
                                capture_output=True, text=True, timeout=5).stdout
        for line in routes.splitlines():
            parts = line.split()
            if "dev" not in parts:
                continue
            dev = parts[parts.index("dev") + 1]
            if dev.startswith(skip_prefix):
                continue
            addr = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", dev],
                                  capture_output=True, text=True, timeout=5).stdout
            match = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+)", addr)
            if match:
                return match.group(1), dev
    except Exception:
        return None, None
    return None, None


def detect_bind_ip():
    """兼容旧调用：返回物理出口 IPv4（仅用于日志/诊断）。"""
    return detect_egress()[0]


def core_has_cap_net_admin(core):
    """内核二进制是否带 CAP_NET_ADMIN（决定能不能用 routing-mark 绕过 TUN）。"""
    if os.environ.get("HERMES_HEALTHCHECK_MARK", "auto").lower() in ("0", "off", "no", "false"):
        return False
    try:
        out = subprocess.run(["getcap", core], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return False
    return "cap_net_admin" in out


def tun_bypass_config(core, mark=524288):
    """返回让测试实例绕开本机 TUN 的配置片段；不可用时返回 {}。

    需要内核二进制带 CAP_NET_ADMIN（``setcap cap_net_admin+ep /usr/bin/mihomo``）。
    没有能力时**不要**下发 routing-mark：内核会拒绝 SO_MARK 并导致所有探测失败
    （实测 0/12），必须老老实实回退到"临时关 TUN"。
    """
    if core_has_cap_net_admin(core):
        return {"routing-mark": mark}
    return {}


def wait_tun_effective(expect_enabled, timeout=6):
    """等 TUN 的开关真正在内核路由上生效，避免"刚关就开始测"的竞态。

    PATCH /configs 返回时 TUN 的路由规则可能还没撤掉，此时测速仍会被抓走
    （表现为可用数虚高，例如真值 12/178 却测出 83/178）。做法：用
    ``ip route get`` 实际探测出口，直到与期望状态一致或超时。
    """
    deadline = time.time() + timeout
    probe = "ht" + "tp://223.5.5.5"  # 任意公网地址；看走哪张路由表
    while time.time() < deadline:
        try:
            route = subprocess.run(["ip", "-4", "route", "get", "1.1.1.1"],
                                   capture_output=True, text=True, timeout=3).stdout
        except Exception:
            return False
        via_tun = "dev Meta" in route or "dev tun" in route or "dev utun" in route
        if via_tun == bool(expect_enabled):
            return True          # 实际路由状态与期望一致（TUN 开关已生效）
        time.sleep(0.3)
    return False


def active_heavy_download(threshold_bytes=30 * 1024 * 1024, socket_path=None):

    """本机代理核心里是否有单条连接已传过 threshold（用于避开正在下载的时刻）。

    返回 (是否繁忙, 说明)。读不到（核心没跑/解析失败）时按"不繁忙"处理。
    """
    socket_path = socket_path or DEFAULT_API_SOCKET
    if not os.path.exists(socket_path):
        return False, "核心未运行"
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(8)
        sock.connect(socket_path)
        sock.sendall(b"GET /connections HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        buffer = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buffer += chunk
        sock.close()
        head, _, body = buffer.partition(b"\r\n\r\n")
        if b"transfer-encoding: chunked" in head.lower():
            decoded, index = b"", 0
            while index < len(body):
                end = body.find(b"\r\n", index)
                if end < 0:
                    break
                try:
                    size = int(body[index:end].split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    break
                decoded += body[end + 2:end + 2 + size]
                index = end + 2 + size + 2
            body = decoded
        data = json.loads(body.decode("utf-8", "ignore"))
    except Exception as exc:
        return False, "读取失败: %s" % type(exc).__name__
    for conn in data.get("connections") or []:
        moved = (conn.get("download") or 0) + (conn.get("upload") or 0)
        if moved >= threshold_bytes:
            meta = conn.get("metadata") or {}
            return True, "%s 已传 %.1fMB" % (meta.get("host") or meta.get("destinationIP") or "?", moved / 1048576.0)
    return False, "无大流量连接"


def system_tun_state(socket_path=None):
    """返回本机代理核心的 TUN 状态；拿不到（没在跑/没有接口）返回 None。"""
    socket_path = socket_path or DEFAULT_API_SOCKET
    if not os.path.exists(socket_path):
        return None
    try:
        configs = _api(socket_path, "GET", "/configs")
        tun = configs.get("tun")
        return bool(tun.get("enable")) if isinstance(tun, dict) else None
    except Exception:
        return None


def set_system_tun(enable, socket_path=None):
    """临时开关本机代理核心的 TUN（不改配置文件，仅运行时）。"""
    socket_path = socket_path or DEFAULT_API_SOCKET
    try:
        _api(socket_path, "PATCH", "/configs", {"tun": {"enable": bool(enable)}})
        return True
    except Exception:
        return False


def find_core(explicit=None):
    """找一个可用的 mihomo 内核：显式路径 > 环境变量 MIHOMO_BIN > PATH > 常见安装位置。"""
    for candidate in (explicit, os.getenv("MIHOMO_BIN")):
        if candidate and os.path.exists(candidate):
            return candidate
    for name in CORE_NAMES:
        found = shutil.which(name)
        if found:
            return found
    for path in CORE_PATHS:
        expanded = os.path.expanduser(path)
        if os.path.exists(expanded):
            return expanded
    return None


def _free_port():
    with socket.socket() as probe:
        probe.bind(("", 0))
        return probe.getsockname()[1]


def _wait_ready(base_url, process, log_path, seconds=30):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base_url + "/version", timeout=2) as response:
                return json.loads(response.read())
        except Exception:
            if process.poll() is not None:
                tail = ""
                try:
                    tail = open(log_path, encoding="utf-8", errors="replace").read()[-400:]
                except OSError:
                    pass
                raise RuntimeError("core exited early (code %s): %s" % (process.returncode, tail))
            time.sleep(0.4)
    return None


def measure(proxies, core=None, url=None, timeout_ms=5000, concurrency=16, workdir=None, keep_workdir=False):
    """逐个实测节点可用性。返回统计字典（``alive`` 按延迟升序）。

    ``proxies`` 是 Clash 节点字典列表；``url`` 默认 204 测速地址。
    """
    core = find_core(core)
    if not core:
        raise RuntimeError("找不到 mihomo 内核，设置 MIHOMO_BIN 或安装 mihomo")
    url = url or DEFAULT_TEST_URL
    port = _free_port()
    workdir = workdir or tempfile.mkdtemp(prefix="healthcheck-")
    os.makedirs(workdir, exist_ok=True)
    names = [proxy["name"] for proxy in proxies]
    controller = LOOPBACK + ":" + str(port)
    config = {
        "log-level": "warning",
        "mode": "rule",
        "ipv6": False,
        "external-controller": controller,
        "dns": {"enable": True, "ipv6": False, "nameserver": ["223.5.5.5", "119.29.29.29"]},
        "proxies": proxies,
        "proxy-groups": [{"name": "PROBE", "type": "select", "proxies": names}],
        "rules": ["MATCH,DIRECT"],
    }
    bypass = tun_bypass_config(core)
    if bypass:
        # 内核二进制带 CAP_NET_ADMIN 时，给测试实例打上和 Verge 相同的 fwmark
        # （ip rule 5210: fwmark 0x80000 → main 表），出站直接走物理网卡、不经过 TUN，
        # 于是测速期间完全不用动系统 TUN。
        config.update(bypass)
        print("测试实例启用 TUN 绕过（%s）：全程不动系统 TUN" % (bypass,), flush=True)
    import yaml

    with open(os.path.join(workdir, "config.yaml"), "w", encoding="utf-8") as handle:
        yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)
    log_path = os.path.join(workdir, "core.log")
    log = open(log_path, "w", encoding="utf-8")
    process = subprocess.Popen(
        [core, "-d", workdir, "-f", os.path.join(workdir, "config.yaml"), "-ext-ctl", controller],
        stdout=log, stderr=subprocess.STDOUT,
    )
    base_url = "http://" + controller
    started = time.time()
    try:
        version = _wait_ready(base_url, process, log_path) or {}
        if not version:
            raise RuntimeError("核心 30 秒内没起来（看 %s）" % log_path)

        def probe(name):
            target = (base_url + "/proxies/" + urllib.parse.quote(name, safe="")
                      + "/delay?timeout=" + str(timeout_ms) + "&url=" + urllib.parse.quote(url, safe=""))
            try:
                with urllib.request.urlopen(target, timeout=timeout_ms / 1000.0 + 10) as response:
                    return name, json.loads(response.read()).get("delay"), None
            except urllib.error.HTTPError as exc:
                try:
                    message = json.loads(exc.read()).get("message", "http " + str(exc.code))
                except Exception:
                    message = "http " + str(exc.code)
                return name, None, message
            except Exception as exc:  # 超时/连接中断等
                return name, None, type(exc).__name__

        by_name = {proxy["name"]: proxy for proxy in proxies}
        results = []
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            for outcome in pool.map(probe, names):
                results.append(outcome)

        alive, timeout_count, error_count, failures = [], 0, 0, []
        for name, delay, reason in results:
            if delay is not None:
                alive.append({"name": name, "delay": delay, "type": by_name[name].get("type", "")})
            else:
                if "imeout" in str(reason):
                    timeout_count += 1
                else:
                    error_count += 1
                if len(failures) < 50:
                    failures.append({"name": name, "type": by_name[name].get("type", ""), "reason": str(reason)[:60]})
        alive.sort(key=lambda item: item["delay"])
        delays = [item["delay"] for item in alive]
        stats = {
            "core": core,
            "core_version": version.get("version", ""),
            "test_url": url,
            "timeout_ms": timeout_ms,
            "concurrency": concurrency,
            "tested": len(names),
            "alive": len(alive),
            "dead_timeout": timeout_count,
            "dead_error": error_count,
            "alive_ratio": round(len(alive) / len(names), 4) if names else 0.0,
            "min_delay_ms": delays[0] if delays else None,
            "median_delay_ms": delays[len(delays) // 2] if delays else None,
            "elapsed_s": round(time.time() - started, 1),
            "failures": failures,
            "alive_nodes": alive,
        }
        return stats
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        log.close()
        if not keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":  # 手动跑：python scripts/healthcheck.py output/clash.yaml
    import sys
    import yaml

    path = sys.argv[1] if len(sys.argv) > 1 else "output/clash.yaml"
    document = yaml.safe_load(open(path, encoding="utf-8"))
    report = measure(document["proxies"], concurrency=int(os.getenv("HEALTH_CONCURRENCY", "16")))
    summary = {key: value for key, value in report.items() if key not in ("alive_nodes", "failures")}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    for item in report["alive_nodes"][:15]:
        print("  %6d ms  %s" % (item["delay"], item["name"][:60]))
