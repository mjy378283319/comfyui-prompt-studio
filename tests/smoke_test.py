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

    def call(self, path, method="GET", body=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={"Content-Type": "application/json"}, method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def request_text(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as resp:
            return resp.read().decode("utf-8")

    def test_all_endpoints(self):
        status, health = self.call("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["ok"])
        self.assertIn("metadata", health["engines"])

        status, lex = self.call("/api/lexicon")
        self.assertEqual(status, 200)
        self.assertEqual(len(lex["lexicon"]["layers"]), 4)

        status, guide = self.call("/api/guide")
        self.assertEqual(status, 200)
        self.assertIn("角色固定层", guide["markdown"])

        status, presets = self.call("/api/presets")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(presets["presets"]), 4)

        self.assertIn("ComfyUI 提示词工作台", self.request_text("/"))
        self.assertIn("layer-card", self.request_text("/static/style.css"))

        status, composed = self.call("/api/compose", "POST", {"selections": sample_selections()})
        self.assertEqual(status, 200)
        self.assertIn("linen shirt", composed["positive"])

        status, wf = self.call("/api/workflow", "POST", {"positive": "a", "negative": "b"})
        self.assertEqual(status, 200)
        self.assertEqual(wf["workflow"]["2"]["inputs"]["text"], "a")

        png = make_png({"parameters": "masterpiece, adult woman, linen shirt\nNegative prompt: lowres"})
        data_url = "data:image/png;base64," + base64.b64encode(png).decode()
        status, extracted = self.call("/api/extract", "POST", {"image": data_url, "engine": "auto"})
        self.assertEqual(status, 200)
        self.assertEqual(extracted["engine"], "metadata")
        self.assertGreaterEqual(len(extracted["tags"]), 3)
        self.assertIn("masterpiece", extracted["positive"])

        status, saved = self.call("/api/presets", "POST", {
            "name": "自测预设", "selections": sample_selections(), "mode": "tag", "model": "sdxl",
        })
        self.assertEqual(status, 200)
        preset_id = saved["preset"]["id"]
        _, presets = self.call("/api/presets")
        self.assertTrue(any(p["id"] == preset_id for p in presets["presets"]))
        status, _ = self.call(f"/api/presets?id={preset_id}", "DELETE")
        self.assertEqual(status, 200)

    def test_error_branches(self):
        status, data = self.call("/api/extract", "POST", {"image": "!!!bad!!!"})
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        status, data = self.call("/api/extract", "POST", {"image": "", "engine": "unknown"})
        self.assertEqual(status, 400)
        status, data = self.call("/api/presets", "POST", {"name": "  "})
        self.assertEqual(status, 400)
        status, data = self.call("/api/no-such-route")
        self.assertEqual(status, 404)
        status, data = self.call("/api/workflow", "POST", {"positive": ""})
        self.assertEqual(status, 200)
        self.assertEqual(data["workflow"]["2"]["inputs"]["text"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
