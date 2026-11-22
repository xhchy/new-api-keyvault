# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 XHTV
"""
keyvault —— New-API 的上游凭证保管与转发层
==========================================

解决什么问题
------------
New-API 后台会把每个渠道填的上游 API key 明文展示给管理员，超管尤其如此。
一旦后台账号给别人看、或者数据库备份外流，全部上游凭证一起完蛋。

本层把上游凭证从 New-API 数据库里彻底剥离：

    * 真实 key 只存在本机 keys.json（POSIX 下 600，不进数据库、不进 git）
    * New-API 里每条渠道的 key 字段都写占位串  vault-only
    * 渠道 base_url 指向  http://127.0.0.1:3997/relay/<route>
    * 本层收到请求后剥掉 /relay/<route>，注入真实 key，转发给真正的上游

因此：拿到超管账号的人，看到的只是一串 vault-only。

路由表（keys.json）
-------------------
{
  "routes": {
    "openai": {
      "upstream":    "https://api.openai.com",
      "key":         "sk-...",
      "auth_header": "Authorization",      // 可选，默认 Authorization
      "auth_prefix": "Bearer ",            // 可选，默认 "Bearer "
      "strip_prefix": "/v1",               // 可选，剥掉请求路径的前缀
      "proxy":       false,                // 可选，走全局代理
      "extra_headers": {"X-Title": "kv"},  // 可选，附加固定头
      "enabled":     true                  // 可选，false 则临时停用
    }
  }
}

字段都存在、但常被忽略的三个：
    auth_header / auth_prefix   Anthropic 用 x-api-key、Azure 用 api-key，
                                不是所有上游都认 Authorization: Bearer
    strip_prefix                Gemini 的 OpenAI 兼容端点是 /v1beta/openai，
                                而 New-API 会再拼一层 /v1，两层叠起来就 404
    extra_headers               有些网关要求 X-Title / HTTP-Referer 之类

用法
----
    python keyvault.py                    # 默认 127.0.0.1:3997
    python keyvault.py --check            # 只做配置自检，不起服务
    KEYVAULT_PORT=4000 python keyvault.py

环境变量
--------
    KEYVAULT_HOST     监听地址，默认 127.0.0.1
    KEYVAULT_PORT     监听端口，默认 3997
    KEYVAULT_KEYS     配置路径，默认同目录 keys.json
    KEYVAULT_PREFIX   路径前缀，默认 /relay/
    KEYVAULT_PROXY    全局代理，默认 http://127.0.0.1:7897
    KEYVAULT_TIMEOUT  上游超时秒数，默认 300
    KEYVAULT_MAX_BODY 请求体上限字节，默认 32MB
    KEYVAULT_LOG      off | info | debug，默认 info

设计取舍
--------
    * 只用标准库。装都不装，扔进目录就能跑。
    * 不做鉴权。它就是给本机 New-API 用的，绑 127.0.0.1 是唯一安全前提。
      真要暴露到网络，请在前面加反代并做鉴权，本程序会拦住并提示。
    * 不记录 key。日志里只有路由名、状态码、耗时、字节数。
    * 支持热重载。keys.json 改动后下一个请求自动生效，不用重启。
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("KEYVAULT_HOST", "127.0.0.1")
PORT = int(os.environ.get("KEYVAULT_PORT", "3997"))
HERE = os.path.dirname(os.path.abspath(__file__))
KEYS_FILE = os.environ.get("KEYVAULT_KEYS", os.path.join(HERE, "keys.json"))
PREFIX = os.environ.get("KEYVAULT_PREFIX", "/relay/")
if not PREFIX.endswith("/"):
    PREFIX += "/"
TIMEOUT = int(os.environ.get("KEYVAULT_TIMEOUT", "300"))
MAX_BODY = int(os.environ.get("KEYVAULT_MAX_BODY", str(32 * 1024 * 1024)))
LOG_LEVEL = os.environ.get("KEYVAULT_LOG", "info").lower()

# 客户端转发的请求头白名单。
# 刻意不含 Authorization —— New-API 给我们的那个 key 是它自己的用户 key，
# 不应该泄露给上游。上游看到的永远是 keys.json 里注入的那一个。
PASS_HEADERS = (
    "Content-Type",
    "Accept",
    "Accept-Language",
    "Accept-Encoding",
    "User-Agent",
)

PLACEHOLDER_PREFIXES = ("YOUR_", "REPLACE_ME", "sk-xxx", "CHANGEME", "<", "${")

_routes = {}
_mtime = 0.0
_opener_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_openers = {}


# ---------------------------------------------------------------- 日志 / 配置


def log(msg, level="info"):
    if LOG_LEVEL == "off":
        return
    if level == "debug" and LOG_LEVEL != "debug":
        return
    sys.stdout.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stdout.flush()


def warn(msg):
    sys.stderr.write("[%s] [warn] %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stderr.flush()


def load_routes():
    """读 keys.json，并为每条路由准备 opener。"""
    global _routes, _mtime, _openers
    with open(KEYS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    routes = data.get("routes", {})
    openers = {}
    for name, r in routes.items():
        if r.get("enabled", True) is False:
            continue
        proxy = r.get("proxy", False)
        proxy_url = r.get("proxy_url") or os.environ.get("KEYVAULT_PROXY", "http://127.0.0.1:7897")
        if proxy:
            openers[name] = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
            )
        else:
            openers[name] = _opener_direct

    _routes = routes
    _openers = openers
    _mtime = os.path.getmtime(KEYS_FILE)

    active = [n for n in sorted(routes) if n in openers]
    idle = [n for n in sorted(routes) if n not in openers]
    log("已加载 %d 条路由：%s" % (len(active), ", ".join(active) or "（空）"))
    if idle:
        log("已停用 %d 条：%s" % (len(idle), ", ".join(idle)))


def maybe_reload():
    """keys.json 变了就重载 —— 改个 key 不用重启进程。"""
    try:
        if os.path.getmtime(KEYS_FILE) != _mtime:
            log("检测到 keys.json 变更，重新加载")
            load_routes()
    except Exception as e:
        warn("重载失败，继续用旧配置：%s" % e)


def opener_for(name):
    return _openers.get(name, _opener_direct)


def strip_prefix(path, strip):
    """剥掉前缀。strip="/v1" 时 /v1/a/b -> /a/b，/v1 -> /。"""
    strip = (strip or "").rstrip("/")
    if not strip:
        return path
    if path == strip:
        return "/"
    if path.startswith(strip + "/"):
        return path[len(strip):]
    return path


# ---------------------------------------------------------------------- 自检


def self_check():
    """--check 模式：不启服务，只把配置能踩的坑提前报出来。"""
    problems = []
    notes = []

    if not os.path.isfile(KEYS_FILE):
        print("✗ 找不到配置文件：%s" % KEYS_FILE)
        print("  先复制一份：copy keys.example.json keys.json（或 cp）")
        return 1

    with open(KEYS_FILE, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            print("✗ keys.json 不是合法 JSON：第 %d 行第 %d 列 %s" % (e.lineno, e.colno, e.msg))
            return 1

    routes = data.get("routes") or {}
    if not routes:
        print("✗ routes 是空的，没有任何东西可转发")
        return 1

    for name, r in routes.items():
        if not isinstance(r, dict):
            problems.append("路由 %s 不是对象" % name)
            continue
        if not r.get("upstream"):
            problems.append("路由 %s 缺 upstream" % name)
        elif not str(r["upstream"]).startswith(("http://", "https://")):
            problems.append("路由 %s 的 upstream 必须以 http:// 或 https:// 开头" % name)

        key = str(r.get("key") or "")
        if key.upper().startswith(PLACEHOLDER_PREFIXES):
            problems.append("路由 %s 的 key 还是占位符，没填真 key" % name)
        if r.get("proxy") and not (r.get("proxy_url") or os.environ.get("KEYVAULT_PROXY")):
            notes.append("路由 %s 开了 proxy，但没配 proxy_url，将用默认 127.0.0.1:7897" % name)

    # 文件权限：POSIX 下 key 文件不该被同机其他用户读
    if os.name == "posix":
        mode = os.stat(KEYS_FILE).st_mode & 0o777
        if mode & 0o077:
            notes.append("keys.json 权限是 %o，建议 chmod 600（同机用户可读）" % mode)

    # 监听地址：非回环等于把注入器借给整个网段
    if HOST not in ("127.0.0.1", "localhost", "::1"):
        notes.append(
            "监听 %s 而不是 127.0.0.1 —— 同网段任何人都能借本程序给上游发请求，"
            "请确认前面有反代和鉴权" % HOST
        )

    active = sum(1 for r in routes.values() if isinstance(r, dict) and r.get("enabled", True) is not False)
    print("配置：%s" % KEYS_FILE)
    print("路由：%d 条（启用 %d）" % (len(routes), active))
    for n in sorted(routes):
        r = routes[n] if isinstance(routes[n], dict) else {}
        flags = []
        if r.get("enabled", True) is False:
            flags.append("已停用")
        if r.get("strip_prefix"):
            flags.append("剥 " + str(r["strip_prefix"]))
        if r.get("proxy"):
            flags.append("走代理")
        h = r.get("auth_header") or "Authorization"
        if h != "Authorization":
            flags.append("认证头 " + h)
        print("  - %-12s %s%s" % (n, r.get("upstream", "?"), ("  [" + ", ".join(flags) + "]") if flags else ""))

    if problems:
        print("")
        print("✗ %d 个问题必须修：" % len(problems))
        for p in problems:
            print("  - " + p)
    if notes:
        print("")
        print("! %d 条提醒（不影响启动，但建议看一眼）：" % len(notes))
        for n in notes:
            print("  - " + n)

    if problems:
        return 1
    print("")
    print("✓ 自检通过")
    return 0


# ---------------------------------------------------------------------- 转发


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return  # 默认日志太吵，自己按 LOG_LEVEL 打

    def _relay(self, method):
        started = time.time()
        maybe_reload()  # 一次 stat 的开销，换来改 key 不用重启
        path = self.path

        if not path.startswith(PREFIX):
            return self._json(404, {"error": {"message": "unknown path, use %s<route>/..." % PREFIX}})

        rest = path[len(PREFIX):]
        route_name, _, tail = rest.partition("/")
        route = _routes.get(route_name)
        if not isinstance(route, dict):
            return self._json(404, {"error": {"message": "unknown route: %s" % route_name}})
        if route.get("enabled", True) is False:
            return self._json(503, {"error": {"message": "route disabled: %s" % route_name}})

        sub_path = "/" + tail if tail else "/"
        sub_path = strip_prefix(sub_path, route.get("strip_prefix"))
        url = str(route["upstream"]).rstrip("/") + sub_path
        if "?" in self.path:
            url += "?" + self.path.split("?", 1)[1]

        # 请求体：必须设上限，否则一个超大上传就能把进程内存吃干
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._json(
                413,
                {"error": {"message": "request body %d bytes exceeds KEYVAULT_MAX_BODY=%d" % (length, MAX_BODY)}},
            )
        body = self.rfile.read(length) if length else None

        req = urllib.request.Request(url, data=body, method=method)
        for h in PASS_HEADERS:
            v = self.headers.get(h)
            if v:
                req.add_header(h, v)

        # 认证头：字段可配，因为不是所有上游都认 Authorization: Bearer
        real_key = route.get("key") or ""
        if real_key:
            header = route.get("auth_header") or "Authorization"
            prefix = route.get("auth_prefix")
            if prefix is None:
                prefix = "Bearer "
            req.add_header(header, str(prefix) + str(real_key))

        for k, v in (route.get("extra_headers") or {}).items():
            req.add_header(str(k), str(v))

        timeout = int(route.get("timeout") or TIMEOUT)

        try:
            resp = opener_for(route_name).open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            detail = e.read()[:4000]
            self.send_response(e.code)
            ctype = e.headers.get("Content-Type") or "application/json"
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(detail)))
            self.end_headers()
            self.wfile.write(detail)
            log("%s %s → 上游 %s（%.0fms，已回传错误体）" % (method, route_name, e.code, (time.time() - started) * 1000))
            return
        except Exception as e:
            log("%s %s → 连接失败：%s" % (method, route_name, e))
            return self._json(502, {"error": {"message": "keyvault upstream error: %s" % e}})

        # 流式回写：SSE 必须逐块吐出，攒完再发就等于把流式废了
        self.send_response(resp.status)
        ctype = resp.headers.get("Content-Type") or "application/json"
        self.send_header("Content-Type", ctype)
        for h in ("Cache-Control", "Content-Encoding"):
            if resp.headers.get(h):
                self.send_header(h, resp.headers[h])
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        total = 0
        try:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                total += len(chunk)
        except Exception as e:
            log("回写中断：%s（已写 %d 字节）" % (e, total), "debug")
            return

        log(
            "%s %s → %s（%.0fms，%d 字节）"
            % (method, route_name, resp.status, (time.time() - started) * 1000, total),
            "debug",
        )

    def do_GET(self):
        clean = self.path.split("?", 1)[0].rstrip("/")
        if clean == "/health":
            return self._json(200, {"status": "ok", "routes": sorted(_routes)})
        if clean == "/__routes":
            # 排查用：给出路由全貌，但绝不含 key
            out = {}
            for n, r in _routes.items():
                if not isinstance(r, dict):
                    continue
                out[n] = {
                    "upstream": r.get("upstream"),
                    "enabled": r.get("enabled", True) is not False,
                    "has_key": bool(r.get("key")),
                    "auth_header": r.get("auth_header") or "Authorization",
                    "strip_prefix": r.get("strip_prefix") or "",
                    "proxy": bool(r.get("proxy")),
                }
            return self._json(200, out)
        return self._relay("GET")

    def do_POST(self):
        return self._relay("POST")

    def do_PUT(self):
        return self._relay("PUT")

    def do_PATCH(self):
        return self._relay("PATCH")

    def do_DELETE(self):
        return self._relay("DELETE")

    def _json(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main():
    if "--check" in sys.argv:
        sys.exit(self_check())

    load_routes()

    if HOST not in ("127.0.0.1", "localhost", "::1"):
        warn("监听 %s，非回环地址。任何能访问它的人都能借本程序调用你的上游账号。" % HOST)
        warn("如果不是前面挂了带鉴权的反代，请改回 KEYVAULT_HOST=127.0.0.1")

    try:
        srv = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        warn("端口 %d 起不来：%s" % (PORT, e))
        warn("可能是上一个实例还在跑，或者端口被占。换一个：KEYVAULT_PORT=4000")
        sys.exit(1)

    log("keyvault 就绪  http://%s:%d%s<route>/..." % (HOST, PORT, PREFIX))
    log("真实 key 只在 %s" % KEYS_FILE)
    log("自检：http://%s:%d/health  ·  路由概览（不含 key）：/__routes" % (HOST, PORT))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("退出")


if __name__ == "__main__":
    main()
