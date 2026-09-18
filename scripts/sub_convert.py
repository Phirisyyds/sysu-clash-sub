"""把 base64 / 明文的分享链接订阅（ss:// vmess:// vless:// trojan:// hysteria2://）转成 Clash 节点字典。

很多公开源不是 Clash YAML，而是机场常见的分享链接列表（整体 base64 或一行一条）。
不转换的话这些源一个节点都用不上 —— 实测这类源能额外贡献上百个候选节点。
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import urllib.parse

PARSERS = {}


def _b64decode(data: str) -> str:
    data = re.sub(r"\s+", "", data).replace("-", "+").replace("_", "/")
    data += "=" * (-len(data) % 4)
    return base64.b64decode(data).decode("utf-8", "ignore")


def _int(value, default=None):
    try:
        return int(str(value).strip())
    except Exception:
        return default


def looks_like_base64(text: str) -> bool:
    head = re.sub(r"\s+", "", text.strip()[:200])
    return bool(head) and "://" not in head and bool(re.fullmatch(r"[A-Za-z0-9+/=_-]+", head))


def split_links(text: str) -> list:
    body = text.strip()
    if looks_like_base64(body):
        try:
            body = _b64decode(body)
        except (binascii.Error, ValueError):
            return []
    return [line.strip() for line in body.splitlines() if "://" in line]


def _ws_opts(query, host_key="host", path_key="path"):
    opts = {"path": query.get(path_key) or "/"}
    if query.get(host_key):
        opts["headers"] = {"Host": query[host_key]}
    return opts


def parse_ss(link):
    body = link.split("://", 1)[1]
    name = ""
    if "#" in body:
        body, name = body.split("#", 1)
        name = urllib.parse.unquote(name)
    if "?" in body:
        body = body.split("?", 1)[0]
    if "@" in body:
        userinfo, hostpart = body.rsplit("@", 1)
        try:
            decoded = _b64decode(userinfo)
            userinfo = decoded if ":" in decoded else urllib.parse.unquote(userinfo)
        except (binascii.Error, ValueError):
            userinfo = urllib.parse.unquote(userinfo)
    else:
        decoded = _b64decode(body)
        if "@" not in decoded:
            raise ValueError("bad ss link")
        userinfo, hostpart = decoded.rsplit("@", 1)
    cipher, _, password = userinfo.partition(":")
    host, _, port = hostpart.rpartition(":")
    proxy = {"type": "ss", "server": host.strip("[]").strip(), "port": _int(port),
             "cipher": cipher.strip(), "password": password.strip(), "udp": True}
    if name:
        proxy["name"] = name
    return proxy


def parse_vmess(link):
    data = json.loads(_b64decode(link.split("://", 1)[1]))
    proxy = {
        "type": "vmess",
        "server": str(data.get("add", "")).strip(),
        "port": _int(data.get("port")),
        "uuid": str(data.get("id", "")).strip(),
        "alterId": _int(data.get("aid"), 0),
        "cipher": data.get("scy") or "auto",
        "udp": True,
    }
    if data.get("ps"):
        proxy["name"] = str(data["ps"])
    net = str(data.get("net") or "tcp").lower()
    host, path = data.get("host"), data.get("path") or "/"
    if net == "ws":
        proxy["network"] = "ws"
        proxy["ws-opts"] = {"path": path}
        if host:
            proxy["ws-opts"]["headers"] = {"Host": host}
    elif net == "grpc":
        proxy["network"] = "grpc"
        proxy["grpc-opts"] = {"grpc-service-name": str(data.get("path") or "")}
    elif net in ("h2", "http"):
        proxy["network"] = "h2"
        proxy["h2-opts"] = {"path": [path], "host": [host] if host else []}
    if str(data.get("tls") or "").lower() in ("tls", "true", "1"):
        proxy["tls"] = True
        server_name = data.get("sni") or host
        if server_name:
            proxy["servername"] = server_name
    if data.get("fp"):
        proxy["client-fingerprint"] = str(data["fp"])
    return proxy


def parse_vless(link):
    parsed = urllib.parse.urlsplit(link)
    query = dict(urllib.parse.parse_qsl(parsed.query))
    proxy = {"type": "vless", "server": parsed.hostname or "", "port": parsed.port,
             "uuid": urllib.parse.unquote(parsed.username or ""), "udp": True}
    if parsed.fragment:
        proxy["name"] = urllib.parse.unquote(parsed.fragment)
    if query.get("security") in ("tls", "reality", "xtls"):
        proxy["tls"] = True
    if query.get("sni"):
        proxy["servername"] = query["sni"]
    if query.get("flow"):
        proxy["flow"] = query["flow"]
    if query.get("pbk"):
        proxy["reality-opts"] = {"public-key": query["pbk"], "short-id": query.get("sid", "")}
    if query.get("fp"):
        proxy["client-fingerprint"] = query["fp"]
    net = query.get("type", "tcp")
    if net == "ws":
        proxy["network"] = "ws"
        proxy["ws-opts"] = _ws_opts(query)
    elif net == "grpc":
        proxy["network"] = "grpc"
        proxy["grpc-opts"] = {"grpc-service-name": query.get("serviceName", "")}
    elif net in ("h2", "http"):
        proxy["network"] = "h2"
        proxy["h2-opts"] = {"path": [query.get("path", "/")], "host": [query["host"]] if query.get("host") else []}
    if query.get("allowInsecure") == "1":
        proxy["skip-cert-verify"] = True
    return proxy


def parse_trojan(link):
    parsed = urllib.parse.urlsplit(link)
    query = dict(urllib.parse.parse_qsl(parsed.query))
    proxy = {"type": "trojan", "server": parsed.hostname or "", "port": parsed.port,
             "password": urllib.parse.unquote(parsed.username or ""), "udp": True}
    if parsed.fragment:
        proxy["name"] = urllib.parse.unquote(parsed.fragment)
    if query.get("sni"):
        proxy["sni"] = query["sni"]
    if query.get("type") == "ws":
        proxy["network"] = "ws"
        proxy["ws-opts"] = _ws_opts(query)
    if query.get("allowInsecure") == "1":
        proxy["skip-cert-verify"] = True
    return proxy


def parse_hysteria2(link):
    parsed = urllib.parse.urlsplit(link)
    query = dict(urllib.parse.parse_qsl(parsed.query))
    password = urllib.parse.unquote(parsed.username or "")
    if parsed.password:
        password = password + ":" + urllib.parse.unquote(parsed.password)
    proxy = {"type": "hysteria2", "server": parsed.hostname or "", "port": parsed.port or 443,
             "password": password}
    if parsed.fragment:
        proxy["name"] = urllib.parse.unquote(parsed.fragment)
    if query.get("sni"):
        proxy["sni"] = query["sni"]
    if query.get("obfs"):
        proxy["obfs"] = query["obfs"]
        proxy["obfs-password"] = query.get("obfs-password", "")
    if query.get("insecure") in ("1", "true"):
        proxy["skip-cert-verify"] = True
    return proxy


PARSERS = {
    "ss": parse_ss,
    "vmess": parse_vmess,
    "vless": parse_vless,
    "trojan": parse_trojan,
    "hysteria2": parse_hysteria2,
    "hy2": parse_hysteria2,
}


def link_to_proxy(link):
    scheme = link.split("://", 1)[0].strip().lower()
    parser = PARSERS.get(scheme)
    if not parser:
        return None
    try:
        proxy = parser(link)
    except Exception:
        return None
    if not proxy.get("server") or not proxy.get("port"):
        return None
    return proxy


def convert(text):
    """返回 (节点列表, 统计)。"""
    stats = {"lines": 0, "parsed": 0, "failed": 0, "by_type": {}, "unsupported": 0}
    proxies = []
    for link in split_links(text):
        stats["lines"] += 1
        scheme = link.split("://", 1)[0].strip().lower()
        if scheme not in PARSERS:
            stats["unsupported"] += 1
            continue
        proxy = link_to_proxy(link)
        if proxy:
            proxies.append(proxy)
            stats["parsed"] += 1
            stats["by_type"][proxy["type"]] = stats["by_type"].get(proxy["type"], 0) + 1
        else:
            stats["failed"] += 1
    return proxies, stats
