"""AI 角色合成台自测：三库索引、ComfyUI 服务端代理、界面所需 API。

跑法：python tests/test_studio.py
用假的 ComfyClient / Gallery，不依赖真实 ComfyUI 或上游仓库。
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

from poses import PoseIndex  # noqa: E402
from studio import ComfyClient, StudioLibrary  # noqa: E402


def make_reference(root: Path) -> None:
    for sub in ("data", "thumbs", "img"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "data" / "pose-zh.json").write_text(json.dumps([
        {"id": "std_P001", "体系": "全身单人・标准系", "姿势编号": "P001", "分类": "站姿",
         "姿势名称": "站立・双手撩发", "角度": "正面",
         "缩略图": "thumbs/pose.jpg", "原图": "img/pose.png"},
        {"id": "sexy_S001", "体系": "全身单人・性感系", "姿势编号": "S001", "分类": "站姿",
         "姿势名称": "S形站姿・胯部侧顶", "角度": "正面",
         "缩略图": "thumbs/adult.jpg", "原图": "img/adult.png"},
    ], ensure_ascii=False), encoding="utf-8")
    (root / "data" / "costume-zh.json").write_text(json.dumps([
        {"id": "SPR-0001", "分类": "春季穿搭", "服装名称": "象牙白针织衫・牛仔裙",
         "标签": ["象牙白"], "关键词": "ivory knit denim skirt",
         "缩略图": "thumbs/costume.jpg", "原图": "img/costume.png"},
        {"id": "SEX-0001", "分类": "性感穿搭", "服装名称": "丝绸吊带裙",
         "标签": ["丝绸"], "关键词": "silk slip dress",
         "缩略图": "thumbs/costume-adult.jpg", "原图": "img/costume-adult.png"},
    ], ensure_ascii=False), encoding="utf-8")
    (root / "data" / "portrait-zh.json").write_text(json.dumps([
        {"id": "f71", "分区": "正脸参考", "发长": "短发", "发型名称": "标准款",
         "标题": "短发・标准款", "缩略图": "thumbs/portrait.jpg", "原图": "img/portrait.png"},
    ], ensure_ascii=False), encoding="utf-8")
    for name in ("pose.jpg", "adult.jpg", "costume.jpg", "costume-adult.jpg", "portrait.jpg"):
        (root / "thumbs" / name).write_bytes(b"x")


class LibraryTests(unittest.TestCase):
    def test_library_index_and_adult_gate(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            make_reference(root)
            poses = PoseIndex(root, True, True)
            library = StudioLibrary(poses)
            counts = library.visible_counts()
            self.assertEqual(counts["portrait"], 1)
            self.assertEqual(counts["costume"], 1)   # 性感穿搭默认隐藏
            self.assertEqual(counts["pose"], 1)      # 性感系默认隐藏
            self.assertIsNotNone(library.pick("portrait"))
            self.assertIsNotNone(library.pick("costume"))
            self.assertEqual(library.pick("costume")["category"], "春季穿搭")
            self.assertIsNone(library.resolve_image("thumbs/adult.jpg"))
            self.assertIsNone(library.resolve_image("../data/pose-zh.json"))
            self.assertEqual(library.resolve_image("thumbs/portrait.jpg").read_bytes(), b"x")

            poses.unlock()
            library.load()
            self.assertEqual(library.visible_counts()["costume"], 2)
            self.assertEqual(library.visible_counts()["pose"], 2)
            self.assertEqual(library.resolve_image("thumbs/adult.jpg").read_bytes(), b"x")
            poses.lock()
            library.load()
            self.assertIsNone(library.resolve_image("thumbs/adult.jpg"))

    def test_hard_filter_never_allows_blocked_records(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "data").mkdir(parents=True)
            (root / "data" / "costume-zh.json").write_text(json.dumps([
                {"id": "bad1", "分类": "内衣睡衣", "服装名称": "loli 风格套装",
                 "关键词": "x", "缩略图": "thumbs/a.jpg", "原图": "img/a.png"},
                {"id": "bad2", "分类": "春季穿搭", "服装名称": "捆绑绳结上衣",
                 "关键词": "x", "缩略图": "thumbs/b.jpg", "原图": "img/b.png"},
            ], ensure_ascii=False), encoding="utf-8")
            library = StudioLibrary(PoseIndex(root, True, True))
            self.assertEqual(library.visible_counts()["costume"], 0)


class ComfyLogicTests(unittest.TestCase):
    def test_inject_workflow(self):
        client = ComfyClient("http://127.0.0.1:8188")
        workflow = {
            "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "old"}},
            "3": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
            "4": {"class_type": "LoadImage", "inputs": {"image": "placeholder.png"}},
            "5": {"class_type": "KSampler", "inputs": {"positive": ["2", 0], "negative": ["3", 0]}},
        }
        nodes = client.inject(workflow, "NEW POSITIVE", "NEW NEGATIVE",
                              [{"name": "uploaded.png", "subfolder": "", "type": "input"}])
        self.assertEqual(nodes["2"]["inputs"]["text"], "NEW POSITIVE")
        self.assertEqual(nodes["3"]["inputs"]["text"], "NEW NEGATIVE")
        self.assertEqual(nodes["4"]["inputs"]["image"], "uploaded.png")
        self.assertEqual(workflow["2"]["inputs"]["text"], "old")  # 原对象不被修改

    def test_result_parsing(self):
        client = ComfyClient("http://127.0.0.1:8188")
        payload = json.dumps({"pid": {
            "outputs": {"9": {"images": [{"filename": "out.png", "subfolder": "", "type": "output"}]}},
            "status": {"status_str": "success", "completed": True},
        }}).encode("utf-8")
        client.request = lambda *a, **k: (200, {}, payload)
        result = client.result("pid")
        self.assertEqual(result["status"], "done")
        self.assertEqual(len(result["images"]), 1)
        self.assertIn("/api/comfy/view?", result["images"][0]["url"])
        client.request = lambda *a, **k: (200, {}, b"{}")
        self.assertEqual(client.result("pid")["status"], "running")

    def test_view_rejects_traversal(self):
        client = ComfyClient("http://127.0.0.1:8188")
        with self.assertRaises(ValueError):
            client.view("../secret.png")


class FakeComfy:
    def status(self):
        return {"ok": True, "url": "http://fake", "version": "test"}

    def submit(self, workflow, positive, negative, uploaded):
        return {"prompt_id": "fake-001", "node_count": len(workflow)}

    def result(self, prompt_id):
        return {"status": "done", "images": [
            {"filename": "out.png", "subfolder": "", "type": "output",
             "url": "/api/comfy/view?filename=out.png&subfolder=&type=output"}
        ]}

    def view(self, filename, subfolder="", image_type="output"):
        if ".." in filename or "/" in filename:
            raise ValueError("非法文件名")
        return b"PNG", "image/png"

    def upload_image(self, data, filename):
        return {"name": filename, "subfolder": "", "type": "input"}

    def diagnose(self, exc):
        return "诊断：" + str(exc)


class FakeGallery:
    def trigger(self): pass
    def status(self): return {"online": False}
    def list_records(self, *a): return {"records": []}
    def resolve_image(self, *a): return None
    def scan_once(self, **k): return {}


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        make_reference(root / "reference")
        server.AUTH = server.Auth(root / "data", trust_proxy=True, allow_public_setup=False)
        server.POSES = server.PoseIndex(root / "reference", True, False)
        server.STUDIO = server.StudioLibrary(server.POSES)
        server.COMFY = FakeComfy()
        server.GALLERY = FakeGallery()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.httpd.server_close(); cls.tmp.cleanup()

    def call(self, path, method="GET", body=None, cookie=""):
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode()), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode()), dict(exc.headers)

    def login(self):
        status, _, hdrs = self.call("/api/auth/setup", "POST",
                                    {"username": "admin", "password": "Passw0rd!"})
        self.assertEqual(status, 200)
        return hdrs.get("Set-Cookie", "").split(";")[0]

    def test_studio_routes(self):
        status, _, _ = self.call("/api/studio/library")
        self.assertEqual(status, 401)
        cookie = self.login()

        status, data, _ = self.call("/api/studio/library", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertTrue(data["available"])
        self.assertEqual(data["counts"]["portrait"], 1)

        status, data, _ = self.call("/api/studio/pick?kind=portrait", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(data["item"]["kind"], "portrait")
        self.assertIn("/api/studio/image?", data["item"]["thumb_url"])

        status, data, _ = self.call("/api/studio/workflow", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("2", data["workflow"])

        status, data, _ = self.call("/api/comfy/status", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertTrue(data["comfy"]["ok"])

        status, data, _ = self.call("/api/comfy/submit", "POST",
                                    {"workflow": {"2": {"class_type": "CLIPTextEncode", "inputs": {}}},
                                     "positive": "a", "negative": "b", "images": []}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(data["prompt_id"], "fake-001")

        status, data, _ = self.call("/api/comfy/result?id=fake-001", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "done")

        status, _, _ = self.call("/api/comfy/view?filename=..%2Fsecret.png", cookie=cookie)
        self.assertEqual(status, 502)
        status, _, _ = self.call("/api/studio/image?path=..%2Fdata%2Fpose-zh.json", cookie=cookie)
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
