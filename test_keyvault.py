# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
keyvault 冒烟测试。零依赖，只用标准库。

    python -m unittest test_keyvault -v

起一个假上游 + 一个 keyvault 实例，验证真正要紧的几条：
注入对不对、前缀剥得对不对、认证头能不能换、超限会不会拦、停用会不会拒。
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import keyvault


# ------------------------------------------------------------------ 假上游


class EchoHandler(BaseHTTPRequestHandler):
    """把收到的请求原样回显，方便断言 keyvault 到底发了什么出去。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def _echo(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        out = json.dumps(
            {
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "x_api_key": self.headers.get("x-api-key"),
                "x_extra": self.headers.get("X-Extra"),
                "body": body.decode("utf-8", "replace"),
            },
            ensure_ascii=False,
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = _echo
    do_POST = _echo


# ------------------------------------------------------------------ 测试体


class KeyvaultTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), EchoHandler)
        up_port = cls.upstream.server_address[1]
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        cls.up_base = "http://127.0.0.1:%d" % up_port

        cfg = {
            "routes": {
                "plain": {"upstream": cls.up_base, "key": "sk-real-secret"},
                "strip": {
                    "upstream": cls.up_base + "/v1beta/openai",
                    "key": "sk-strip",
                    "strip_prefix": "/v1",
                },
                "xkey": {
                    "upstream": cls.up_base,
                    "key": "sk-anthropic",
                    "auth_header": "x-api-key",
                    "auth_prefix": "",
                },
                "keyless": {"upstream": cls.up_base, "key": ""},
                "extras": {
                    "upstream": cls.up_base,
                    "key": "sk-x",
                    "extra_headers": {"X-Extra": "hello"},
                },
                "off": {"upstream": cls.up_base, "key": "sk-x", "enabled": False},
            }
        }
        fd, cls.cfg_path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f)

        # 让模块指向临时配置
        keyvault.KEYS_FILE = cls.cfg_path
        keyvault.load_routes()

        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), keyvault.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.upstream.shutdown()
        os.unlink(cls.cfg_path)

    # ---------------- 工具 ----------------

    def call(self, path, method="POST", body=b"", headers=None):
        req = urllib.request.Request(self.base + path, data=body or None, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw.decode())
            except Exception:
                return e.code, {"raw": raw.decode("utf-8", "replace")}

    # ---------------- 用例 ----------------

    def test_01_注入_bearer(self):
        _, d = self.call("/relay/plain/chat/completions", body=b'{"a":1}')
        self.assertEqual(d["auth"], "Bearer sk-real-secret")
        self.assertEqual(d["path"], "/chat/completions")
        self.assertEqual(d["body"], '{"a":1}')

    def test_02_剥前缀(self):
        _, d = self.call("/relay/strip/v1/chat/completions")
        # upstream 已含 /v1beta/openai，请求里的 /v1 必须被削掉。
        # 假上游收到的是完整路径，所以正确的样子是 /v1beta/openai/chat/completions；
        # 如果出现 /v1beta/openai/v1/chat/completions，就是没剥干净 —— 线上表现为 404。
        self.assertEqual(d["path"], "/v1beta/openai/chat/completions")
        self.assertNotIn("/v1beta/openai/v1", d["path"])

    def test_03_自定义认证头(self):
        _, d = self.call("/relay/xkey/v1/messages")
        self.assertEqual(d["x_api_key"], "sk-anthropic")
        self.assertIsNone(d["auth"], "不该同时发 Authorization")

    def test_04_keyless_不发认证头(self):
        _, d = self.call("/relay/keyless/v1/models", method="GET")
        self.assertIsNone(d["auth"])
        self.assertIsNone(d["x_api_key"])

    def test_05_附加固定头(self):
        _, d = self.call("/relay/extras/x")
        self.assertEqual(d["x_extra"], "hello")

    def test_06_未知路由(self):
        code, d = self.call("/relay/nope/x")
        self.assertEqual(code, 404)
        self.assertIn("unknown route", d["error"]["message"])

    def test_07_停用路由(self):
        code, d = self.call("/relay/off/x")
        self.assertEqual(code, 503)
        self.assertIn("disabled", d["error"]["message"])

    def test_08_请求体超限(self):
        old = keyvault.MAX_BODY
        keyvault.MAX_BODY = 16
        try:
            code, d = self.call("/relay/plain/x", body=b"x" * 64)
            self.assertEqual(code, 413)
            self.assertIn("exceeds", d["error"]["message"])
        finally:
            keyvault.MAX_BODY = old

    def test_09_health_与路由概览(self):
        code, d = self.call("/health", method="GET")
        self.assertEqual(code, 200)
        self.assertIn("plain", d["routes"])

        code, d = self.call("/__routes", method="GET")
        self.assertEqual(code, 200)
        # 概览里只允许出现 has_key 布尔，不许出现 key 本体
        self.assertNotIn("sk-real-secret", json.dumps(d))
        self.assertTrue(d["plain"]["has_key"])

    def test_10_客户端_Authorization_不透传(self):
        """New-API 发给我们的 key 不该原样泄露给上游。"""
        _, d = self.call("/relay/plain/x", headers={"Authorization": "Bearer sk-newapi-user-key"})
        self.assertEqual(d["auth"], "Bearer sk-real-secret")


class StripPrefixUnit(unittest.TestCase):
    """strip_prefix 的边界，单独测，不用起服务。"""

    def test_边界(self):
        f = keyvault.strip_prefix
        self.assertEqual(f("/v1/a/b", "/v1"), "/a/b")
        self.assertEqual(f("/v1", "/v1"), "/")
        self.assertEqual(f("/v1/", "/v1"), "/")
        self.assertEqual(f("/a/b", "/v1"), "/a/b")
        self.assertEqual(f("/a/b", ""), "/a/b")
        self.assertEqual(f("/a/b", None), "/a/b")
        # 结尾带斜杠的写法也要能用
        self.assertEqual(f("/v1/a", "/v1/"), "/a")
        # 不能误伤 /v10
        self.assertEqual(f("/v10/a", "/v1"), "/v10/a")


if __name__ == "__main__":
    unittest.main(verbosity=2)
