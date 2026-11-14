"""出图自动归档 + Z-Image 一键推送的端到端回归测试（纯标准库）。

跑法：python tests/test_gallery.py
用本地假 ComfyUI 顶替 B 房服务，覆盖：
  /history → /view 拉图 → 落盘 → 画廊列表 / 搜索 / 图片读取 / 删除
  ComfyUI --auth 凭据透传、凭据不回显、路径穿越防护、未登录 401
  Z-Image 工作流生成与 /prompt 推送
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
PROMPT_ID = "e2e-zimage-0001"
WORKFLOW = {
    "1": {"class_type": "UNETLoader",
          "inputs": {"unet_name": "z_image_turbo_int8_convrot.safetensors", "weight_dtype": "default"}},
    "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen_3_4b_fp8_mixed.safetensors"}},
    "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "a cinematic portrait, soft window light"}},
    "4": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
    "6": {"class_type": "KSampler", "inputs": {"seed": 12345, "steps": 8, "cfg": 1.0}},
    "5": {"class_type": "EmptySD3LatentImage", "inputs": {"width": 1024, "height": 1024}},
}
HISTORY = {
    PROMPT_ID: {
        "prompt": [0, PROMPT_ID, WORKFLOW, {}, []],
        "outputs": {"9": {"images": [
            {"filename": "PromptStudio_00001_.png", "subfolder": "", "type": "output"}
        ]}},
        "status": {"status_str": "success", "completed": True},
    }
}
COMFY_HITS: list[tuple] = []


class FakeComfyUI(BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: A002
        pass

    def _reply(self, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        COMFY_HITS.append((path, self.headers.get("Authorization")))
        if path == "/history":
            self._reply(json.dumps(HISTORY).encode())
        elif path == "/view":
            self._reply(PNG, "image/png")
        elif path == "/system_stats":
            self._reply(b"{}")
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length)
        path = urllib.parse.urlparse(self.path).path
        COMFY_HITS.append((path, self.headers.get("Authorization")))
        if path == "/prompt":
            payload = json.loads(raw.decode("utf-8"))
            assert "UNETLoader" in json.dumps(payload["prompt"]), "推送的不是 Z-Image 工作流"
            self._reply(json.dumps({"prompt_id": "pushed-1"}).encode())
        else:
            self.send_error(404)


def http(url: str, method: str = "GET", body: dict | None = None,
         cookie: str = "", timeout: float = 8.0):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="prompt-studio-e2e-"))
    tmp2 = None
    comfy = ThreadingHTTPServer(("127.0.0.1", 0), FakeComfyUI)
    comfy.daemon_threads = True
    threading.Thread(target=comfy.serve_forever, daemon=True).start()
    comfy_port = comfy.server_address[1]

    for name in ("lexicon.json", "guide.md", "presets.json"):
        shutil.copy(ROOT / "data" / name, tmp / name)

    os.environ["DATA_DIR"] = str(tmp)
    os.environ["COMFYUI_URL"] = f"http://u:p@127.0.0.1:{comfy_port}"
    os.environ["WORKFLOW_ENGINE"] = "zimage-int8"
    os.environ["GALLERY_ENABLED"] = "true"
    os.environ["GALLERY_POLL_INTERVAL"] = "2"
    os.environ["GALLERY_INIT_MODE"] = "all"
    os.environ["PORT"] = "0"
    os.environ["REQUIRE_HTTPS"] = "false"

    import app.server as s  # noqa: E402 — 必须在环境变量设置之后导入

    studio = ThreadingHTTPServer(("127.0.0.1", 0), s.Handler)
    studio.daemon_threads = True
    threading.Thread(target=studio.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{studio.server_address[1]}"
    s.GALLERY.start()

    checks: list[str] = []
    try:
        # 凭据剥离：URL 里不能出现口令，但请求头要带 Basic
        assert "u:p" not in s.COMFYUI_URL, s.COMFYUI_URL
        assert s._COMFY_AUTH == "Basic " + base64.b64encode(b"u:p").decode()
        checks.append("凭据剥离 + Authorization 头")

        # 等归档线程把假 ComfyUI 的历史图拉回来
        deadline = time.time() + 12
        while time.time() < deadline and (tmp / "gallery.json").exists() is False:
            time.sleep(0.2)
        assert (tmp / "gallery.json").is_file(), "归档索引没有生成"
        index = json.loads((tmp / "gallery.json").read_text(encoding="utf-8"))
        assert index["items"], "归档索引为空"
        record = index["items"][0]
        assert record["positive"] == "a cinematic portrait, soft window light"
        assert record["engine"] == "zimage-int8" and record["seed"] == 12345
        saved = tmp / "gallery" / record["images"][0]["file"]
        assert saved.is_file() and saved.read_bytes() == PNG
        checks.append("/history → /view 拉图 → 落盘 → 元数据反解")

        assert any(auth == s._COMFY_AUTH for _, auth in COMFY_HITS), COMFY_HITS
        checks.append("ComfyUI --auth 凭据透传")

        # 未登录访问画廊必须 401
        status, _, _ = http(base + "/api/gallery")
        assert status == 401, status
        checks.append("未登录 401")

        # 建管理员，拿会话 Cookie
        status, headers, _ = http(base + "/api/auth/setup", "POST",
                                  {"username": "admin", "password": "test-pass-1234"})
        assert status == 200, status
        cookie = headers.get("Set-Cookie", "").split(";")[0]
        assert cookie, "没有拿到会话 Cookie"
        checks.append("管理员初始化 + 会话 Cookie")

        status, _, raw = http(base + "/api/health", cookie=cookie)
        health = json.loads(raw)
        assert status == 200 and "engines" in health, health
        assert health["workflow_engine"] == "zimage-int8"
        assert health["workflow_defaults"]["steps"] == 8
        checks.append("/api/health 含 engines / 引擎默认值（修旧版 boot 崩溃）")

        # 工作流生成 = Z-Image
        status, _, raw = http(base + "/api/workflow", "POST",
                              {"positive": "a red fox", "negative": "blur",
                               "options": {"width": 1024, "height": 1024, "steps": 8, "cfg": 1.0}},
                              cookie=cookie)
        wf = json.loads(raw)["workflow"]
        assert status == 200 and wf["1"]["class_type"] == "UNETLoader"
        assert wf["3"]["inputs"]["text"] == "a red fox"
        checks.append("/api/workflow 生成 Z-Image-Turbo INT8")

        # 一键推送（服务端不再二次转换已有 Z-Image 工作流）
        status, _, raw = http(base + "/api/push", "POST", {"workflow": wf}, cookie=cookie)
        pushed = json.loads(raw)
        assert status == 200 and pushed["response"]["prompt_id"] == "pushed-1", pushed
        assert "u:p" not in raw.decode(), "凭据回显到了响应里"
        checks.append("/api/push 推送 Z-Image 且不回显口令")

        # 画廊列表 / 搜索 / 图片 / 路径穿越 / 手动扫描 / 删除
        status, _, raw = http(base + "/api/gallery?limit=60", cookie=cookie)
        data = json.loads(raw)
        assert status == 200 and data["total"] >= 1, data
        assert data["items"][0]["images"][0]["url"].startswith("/api/gallery/image?file=")
        checks.append("/api/gallery 列表与图片 URL")

        status, _, raw = http(base + "/api/gallery?q=cinematic", cookie=cookie)
        assert json.loads(raw)["total"] >= 1
        status, _, raw = http(base + "/api/gallery?q=zzz-not-found", cookie=cookie)
        assert json.loads(raw)["total"] == 0
        checks.append("画廊关键词搜索")

        image_url = base + data["items"][0]["images"][0]["url"]
        status, _, raw = http(image_url, cookie=cookie)
        assert status == 200 and raw == PNG
        checks.append("/api/gallery/image 返回原图")

        status, _, _ = http(base + "/api/gallery/image?file=../../../../etc/passwd", cookie=cookie)
        assert status == 404, status
        status, _, _ = http(base + "/api/gallery/image", cookie=cookie)
        assert status == 404
        checks.append("图片接口路径穿越 / 越权读取防护")

        status, _, raw = http(base + "/api/gallery/scan", "POST", {}, cookie=cookie)
        assert status == 200 and "added" in json.loads(raw)
        checks.append("手动全量扫描接口")

        status, _, _ = http(base + f"/api/gallery?id={PROMPT_ID}", "DELETE", cookie=cookie)
        assert status == 200
        assert not saved.is_file(), "删除记录后图片文件仍在"
        assert PROMPT_ID in json.loads(
            (tmp / "gallery.json").read_text(encoding="utf-8"))["deleted_ids"]
        checks.append("删除记录 + 清理图片 + 重启后不复活")

        # from_now：容器启动时不回填已有历史，只归档之后新出的图
        tmp2 = Path(tempfile.mkdtemp(prefix="prompt-studio-fromnow-"))
        g2 = s.Gallery(tmp2, s.COMFYUI_URL, s._comfy_headers,
                       enabled=False, poll_interval=2, init_mode="from_now", timeout=5)
        g2._prime()
        assert g2.status()["items"] == 0, "from_now 不该回填已有历史"
        new_id = "e2e-zimage-0002"
        HISTORY[new_id] = {
            "prompt": [0, new_id, WORKFLOW, {}, []],
            "outputs": {"9": {"images": [
                {"filename": "PromptStudio_00002_.png", "subfolder": "", "type": "output"}
            ]}},
            "status": {"status_str": "success", "completed": True},
        }
        result = g2.scan_once()
        assert result["added"] == 1, result
        assert g2.list_records()["items"][0]["id"] == new_id
        assert (tmp2 / "gallery.json").is_file()
        checks.append("from_now 只归档新图、跳过启动时的历史")

        print(f"PASS  {len(checks)} 项检查全部通过")
        for name in checks:
            print(f"  ✓ {name}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL  {type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        s.GALLERY.stop()
        studio.shutdown()
        comfy.shutdown()
        studio.server_close()
        comfy.server_close()
        shutil.rmtree(tmp, ignore_errors=True)
        if tmp2:
            shutil.rmtree(tmp2, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
