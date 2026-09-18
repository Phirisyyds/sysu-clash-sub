"""本地验证用包装：把域名解析交给 mihomo 的 DNS（绕过 /etc/hosts 的固定 IP），再跑 update.py。

用法：python3 scripts/_local_run.py
说明：CI 上不需要它；这是给本机（hosts 被钉死 / 走 TUN）验证用的。
"""
import json
import runpy
import socket
import sys
from pathlib import Path

SOCK = "/tmp/verge/verge-mihomo.sock"
_orig = socket.getaddrinfo
_cache = {}


def _resolve(name):
    if name in _cache:
        return _cache[name]
    addresses = []
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(6)
        client.connect(SOCK)
        client.sendall(("GET /dns/query?name=" + name + "&type=A HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n").encode())
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
        for answer in json.loads(body).get("Answer", []):
            if answer.get("type") == 1:
                addresses.append(answer["data"])
    except Exception:
        addresses = []
    _cache[name] = addresses
    return addresses


def patched(host, port, family=0, type=0, proto=0, flags=0):
    if isinstance(host, str) and host and not host.replace(".", "").isdigit():
        addresses = _resolve(host)
        if addresses:
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port)) for ip in addresses]
    return _orig(host, port, family, type, proto, flags)


socket.getaddrinfo = patched
sys.argv = [sys.argv[0]]
runpy.run_path(str(Path(__file__).with_name("update.py")), run_name="__main__")
