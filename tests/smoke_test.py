"""零依赖端到端自测：python tests/smoke_test.py

覆盖词库、四层合成、工作流生成、PNG 元数据反推、全部 HTTP 接口与错误分支。
"""
from __future__ import annotations

import base64
import json
import struct
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zlib
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for path in (ROOT, ROOT / "app"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from auth import (  # noqa: E402
    Auth, AuthError, hash_password, is_https, is_private_host,
    password_strength_error, verify_password,
)
from composer import compose  # noqa: E402
from extract import decode_data_url, extract_metadata, split_tags  # noqa: E402
from lexicon import Lexicon  # noqa: E402
from workflow import build_workflow  # noqa: E402

LEX = Lexicon(ROOT / "data")


def _chunk(ctype: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + ctype + data
            + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF))


def make_png(text_chunks: dict[str, str]) -> bytes:
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    parts = [b"\x89PNG\r\n\x1a\n", _chunk(b"IHDR", ihdr)]
    for key, value in text_chunks.items():
        parts.append(_chunk(b"tEXt", key.encode("utf-8") + b"\x00" + value.encode("utf-8")))
    parts.append(_chunk(b"IEND", b""))
    return b"".join(parts)


def sample_selections() -> dict:
    return {
        "identity": ["28-year-old woman", "dark brown hair", "high cheekbones"],
        "attire": ["linen shirt", "crisp linen"],
        "pose_scene": ["reclining on a bed", "bedroom", "golden hour"],
        "photo": ["soft window light from the left", "85mm f/1.4 portrait lens", "natural skin texture"],
    }


class CoreTests(unittest.TestCase):
    def test_lexicon(self):
        self.assertEqual(len(LEX.layers), 4)
        self.assertEqual([l["id"] for l in LEX.layers], ["identity", "attire", "pose_scene", "photo"])
        self.assertTrue(LEX.lookup("28-year-old woman"))
        self.assertTrue(LEX.is_blocked("loli, nsfw"))
        kept, removed = LEX.sanitize_tags(["linen shirt", "loli", "child", "85mm f/1.4 portrait lens"])
        self.assertEqual(kept, ["linen shirt", "85mm f/1.4 portrait lens"])
        self.assertEqual(len(removed), 2)

    def test_compose_tag(self):
        result = compose({
            "selections": sample_selections(), "mode": "tag", "model": "sdxl",
            "syntax": "paren_weight", "quality_prefix": True,
        }, LEX)
        self.assertIn("(28-year-old woman:1.25)", result["positive"])
        self.assertIn("masterpiece", result["positive"])
        self.assertLess(result["positive"].index("28-year-old woman"), result["positive"].index("linen shirt"))
        self.assertLess(result["positive"].index("linen shirt"), result["positive"].index("reclining on a bed"))
        self.assertIn("lowres", result["negative"])
        self.assertIn("child", result["negative"])
        self.assertEqual(len(result["segments"]), 5)
        self.assertEqual(result["stats"]["tag_count"], 11)

    def test_compose_natural(self):
        result = compose({
            "selections": sample_selections(), "mode": "natural", "model": "flux",
            "syntax": "paren_weight",
        }, LEX)
        self.assertIn("A high-quality adult portrait of", result["positive"])
        self.assertIn("Photography:", result["positive"])
        self.assertNotIn("(28-year-old woman", result["positive"])
        self.assertEqual(result["negative"], "")
        self.assertIn("权重语法不生效", " ".join(result["warnings"]))

    def test_compose_safety_and_conflicts(self):
        result = compose({
            "selections": {
                "identity": ["adult woman", "loli"],
                "photo": ["soft window light from the left", "Rembrandt lighting"],
            },
            "mode": "tag", "extra_positive": "nsfw, sheer chiffon",
        }, LEX)
        self.assertNotIn("loli", result["positive"])
        self.assertNotIn("nsfw", result["positive"])
        self.assertIn("sheer chiffon", result["positive"])
        joined = " ".join(result["warnings"])
        self.assertIn("过滤", joined)
        self.assertIn("光影方向", joined)

    def test_workflow(self):
        wf = build_workflow("A, B", "lowres", {"width": 1024, "height": 1024, "steps": 20, "cfg": 5, "seed": 42})
        self.assertEqual(wf["2"]["inputs"]["text"], "A, B")
        self.assertEqual(wf["3"]["inputs"]["text"], "lowres")
        self.assertEqual(wf["5"]["inputs"]["positive"], ["2", 0])
        self.assertEqual(wf["5"]["inputs"]["negative"], ["3", 0])
        self.assertEqual(wf["4"]["inputs"]["width"], 1024)
        self.assertEqual(wf["5"]["inputs"]["seed"], 42)

    def test_split_tags(self):
        self.assertEqual(split_tags("(tag:1.2), ((other)), {third}, [fourth]"),
                         ["tag", "other", "third", "fourth"])
        self.assertEqual(split_tags(""), [])

    def test_png_parameters(self):
        png = make_png({
            "parameters": "masterpiece, best quality, 1girl, linen shirt\nNegative prompt: lowres, bad hands\nSteps: 20",
        })
        meta = extract_metadata(png, "image/png")
        self.assertEqual(meta["format"], "png")
        self.assertIn("masterpiece", meta["positive"])
        self.assertEqual(meta["negative"], "lowres, bad hands")
        self.assertEqual(meta["source"], "png:parameters")

    def test_png_comfy_workflow(self):
        nodes = {
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "x.safetensors"}},
            "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "adult woman, window light", "clip": ["1", 1]}},
            "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "lowres, bad anatomy", "clip": ["1", 1]}},
            "5": {"class_type": "KSampler", "inputs": {"positive": ["2", 0], "negative": ["3", 0]}},
        }
        png = make_png({"prompt": json.dumps(nodes), "workflow": "{}"})
        meta = extract_metadata(png, "image/png")
        self.assertEqual(meta["positive"], "adult woman, window light")
        self.assertEqual(meta["negative"], "lowres, bad anatomy")
        self.assertEqual(meta["workflow_nodes"], 2)

    def test_decode_data_url(self):
        raw = b"hello"
        data, mime = decode_data_url("data:image/png;base64," + base64.b64encode(raw).decode())
        self.assertEqual(data, raw)
        self.assertEqual(mime, "image/png")
        with self.assertRaises(ValueError):
            decode_data_url("!!!not-base64!!!")

    def test_auth_primitives(self):
        stored = hash_password("Passw0rd!")
        self.assertTrue(stored.startswith("pbkdf2_sha256$"))
        self.assertNotIn("Passw0rd!", stored)
        self.assertTrue(verify_password("Passw0rd!", stored))
        self.assertFalse(verify_password("passw0rd!", stored))
        self.assertFalse(verify_password("Passw0rd!", "broken"))
        self.assertIn("8 位", password_strength_error("abc1") or "")
        self.assertIn("纯数字", password_strength_error("12345678") or "")
        self.assertIsNone(password_strength_error("Passw0rd!"))
        self.assertTrue(is_private_host("127.0.0.1"))
        self.assertTrue(is_private_host("192.168.1.20"))
        self.assertFalse(is_private_host("8.8.8.8"))
        self.assertTrue(is_https({"X-Forwarded-Proto": "https"}, True))
        self.assertFalse(is_https({"X-Forwarded-Proto": "https"}, False))

        with tempfile.TemporaryDirectory() as folder:
            auth = Auth(folder, trust_proxy=True)
            with self.assertRaises(AuthError):
                auth.setup("admin", "Passw0rd!", "8.8.8.8", "8.8.8.8", "test")  # 公网禁止初始化
            token = auth.setup("admin", "Passw0rd!", "127.0.0.1", "127.0.0.1", "test")
            self.assertTrue(auth.resolve(token))
            self.assertIn("HttpOnly", auth.cookie(token, True, True))
            self.assertIn("Secure", auth.cookie(token, True, True))
            self.assertNotIn("Secure", auth.cookie(token, False, True))
            auth.change_password("Passw0rd!", "N3wPassw0rd!")
            self.assertFalse(auth.resolve(token))  # 改口令后旧会话失效
            with self.assertRaises(AuthError):
                auth.login("admin", "Passw0rd!", "127.0.0.1", "test")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server
        cls.tmp = tempfile.TemporaryDirectory()
        server.USER_PRESETS = Path(cls.tmp.name) / "user_presets.json"
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        import server
        auth_file = Path(self.tmp.name) / "auth.json"
        if auth_file.exists():
            auth_file.unlink()
        server.AUTH = server.Auth(self.tmp.name, trust_proxy=True, allow_public_setup=False)
        self.cookie = ""

    def raw(self, path, method="GET", body=None, headers=None, follow=True):
        hdrs = {"Content-Type": "application/json"}
        if self.cookie:
            hdrs["Cookie"] = self.cookie
        hdrs.update(headers or {})
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers=hdrs, method=method,
        )
        opener = urllib.request.build_opener() if follow else urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(req, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def call(self, path, method="GET", body=None, headers=None, follow=True):
        status, hdrs, raw = self.raw(path, method, body, headers, follow)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = {"_raw": raw.decode("utf-8", "replace")}
        return status, data, hdrs

    def request_text(self, path):
        status, _, raw = self.raw(path)
        return status, raw.decode("utf-8", "replace")

    def _capture_cookie(self, hdrs):
        self.cookie = hdrs.get("Set-Cookie", "").split(";")[0]
        self.assertTrue(self.cookie.startswith("ps_session="), hdrs.get("Set-Cookie", ""))

    def do_setup(self):
        status, data, hdrs = self.call("/api/auth/setup", "POST", {
            "username": "admin", "password": "Passw0rd!",
        })
        self.assertEqual(status, 200, data)
        self._capture_cookie(hdrs)

    def do_login(self, password="Passw0rd!"):
        status, data, hdrs = self.call("/api/auth/login", "POST", {
            "username": "admin", "password": password, "remember": True,
        })
        self.assertEqual(status, 200, data)
        self._capture_cookie(hdrs)

    def test_auth_flow(self):
        status, data, _ = self.call("/api/auth/status")
        self.assertEqual(status, 200)
        self.assertTrue(data["need_setup"])
        self.assertTrue(data["setup_allowed"])

        status, data, _ = self.call("/api/lexicon")
        self.assertEqual(status, 401)
        self.assertEqual(data["code"], "unauthorized")

        status, hdrs, _ = self.raw("/", follow=False)
        self.assertEqual(status, 302)
        self.assertEqual(hdrs.get("Location"), "/login")
        status, text = self.request_text("/login")
        self.assertEqual(status, 200)
        self.assertIn("初始化管理员", text)

        status, data, _ = self.call("/api/auth/setup", "POST", {"username": "admin", "password": "12345678"})
        self.assertEqual(status, 400)
        self.assertIn("纯数字", data["error"])

        self.do_setup()
        status, data, _ = self.call("/api/lexicon")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["lexicon"]["layers"]), 4)
        status, hdrs, _ = self.raw("/login", follow=False)
        self.assertEqual(status, 302)
        self.assertEqual(hdrs.get("Location"), "/")

        status, _, _ = self.call("/api/auth/logout", "POST", {})
        self.assertEqual(status, 200)
        status, _, _ = self.call("/api/lexicon")
        self.assertEqual(status, 401)

        status, data, _ = self.call("/api/auth/login", "POST", {"username": "admin", "password": "wrong"})
        self.assertEqual(status, 400)
        self.do_login()
        status, _, _ = self.call("/api/lexicon")
        self.assertEqual(status, 200)

        status, data, _ = self.call("/api/auth/password", "POST", {
            "old_password": "Passw0rd!", "new_password": "N3wPassw0rd!",
        })
        self.assertEqual(status, 200, data)
        status, _, _ = self.call("/api/lexicon")
        self.assertEqual(status, 401)  # 改口令后旧会话全部作废
        self.do_login("N3wPassw0rd!")
        status, _, _ = self.call("/api/lexicon")
        self.assertEqual(status, 200)

    def test_all_endpoints(self):
        self.do_setup()
        status, health, _ = self.call("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["ok"])
        self.assertTrue(health["auth"]["initialized"])

        status, guide, _ = self.call("/api/guide")
        self.assertEqual(status, 200)
        self.assertIn("角色固定层", guide["markdown"])

        status, presets, _ = self.call("/api/presets")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(presets["presets"]), 4)

        status, engines, _ = self.call("/api/engines")
        self.assertEqual(status, 200)
        self.assertIn("metadata", engines["engines"])

        status, text = self.request_text("/")
        self.assertEqual(status, 200)
        self.assertIn("模块化模板", text)
        status, text = self.request_text("/static/style.css")
        self.assertEqual(status, 200)
        self.assertIn("layer-card", text)

        status, composed, _ = self.call("/api/compose", "POST", {"selections": sample_selections()})
        self.assertEqual(status, 200)
        self.assertIn("linen shirt", composed["positive"])

        status, wf, _ = self.call("/api/workflow", "POST", {"positive": "a", "negative": "b"})
        self.assertEqual(status, 200)
        self.assertEqual(wf["workflow"]["2"]["inputs"]["text"], "a")

        png = make_png({"parameters": "masterpiece, adult woman, linen shirt\nNegative prompt: lowres"})
        data_url = "data:image/png;base64," + base64.b64encode(png).decode()
        status, extracted, _ = self.call("/api/extract", "POST", {"image": data_url, "engine": "auto"})
        self.assertEqual(status, 200)
        self.assertEqual(extracted["engine"], "metadata")
        self.assertGreaterEqual(len(extracted["tags"]), 3)
        self.assertIn("masterpiece", extracted["positive"])

        status, saved, _ = self.call("/api/presets", "POST", {
            "name": "自测预设", "selections": sample_selections(), "mode": "tag", "model": "sdxl",
        })
        self.assertEqual(status, 200)
        preset_id = saved["preset"]["id"]
        _, presets, _ = self.call("/api/presets")
        self.assertTrue(any(p["id"] == preset_id for p in presets["presets"]))
        status, _, _ = self.call(f"/api/presets?id={preset_id}", "DELETE")
        self.assertEqual(status, 200)

    def test_error_branches(self):
        self.do_setup()
        status, data, _ = self.call("/api/extract", "POST", {"image": "!!!bad!!!"})
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        status, _, _ = self.call("/api/extract", "POST", {"image": "", "engine": "unknown"})
        self.assertEqual(status, 400)
        status, _, _ = self.call("/api/presets", "POST", {"name": "  "})
        self.assertEqual(status, 400)
        status, _, _ = self.call("/api/no-such-route")
        self.assertEqual(status, 404)
        status, data, _ = self.call("/api/workflow", "POST", {"positive": ""})
        self.assertEqual(status, 200)
        self.assertEqual(data["workflow"]["2"]["inputs"]["text"], "")

    def test_security(self):
        # 跨站 Origin 直接拒绝，不再有 Access-Control-Allow-Origin: *
        status, data, _ = self.call("/api/compose", "POST", {"selections": {}},
                                    headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        status, hdrs, _ = self.raw("/api/health", headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 200)
        self.assertNotIn("Access-Control-Allow-Origin", hdrs)

        # REQUIRE_HTTPS：普通页面 308 跳 https，健康检查例外
        import server
        server.AUTH.require_https = True
        status, hdrs, _ = self.raw("/", follow=False)
        self.assertEqual(status, 308)
        self.assertTrue(hdrs.get("Location", "").startswith("https://"))
        status, _, _ = self.raw("/api/health")
        self.assertEqual(status, 200)
        server.AUTH.require_https = False

        # 公网来源不能初始化管理员
        status, data, _ = self.call("/api/auth/setup", "POST",
                                    {"username": "admin", "password": "Passw0rd!"},
                                    headers={"X-Forwarded-For": "8.8.8.8"})
        self.assertEqual(status, 200)  # TCP 对端仍是本机内网，与拓竹口径一致
        server.AUTH = server.Auth(self.tmp.name, allow_public_setup=False)
        self.assertFalse(server.AUTH.setup_allowed("8.8.8.8"))
        self.assertTrue(server.AUTH.setup_allowed("192.168.1.10"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
