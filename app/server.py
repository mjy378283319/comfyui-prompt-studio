"""ComfyUI Prompt Studio — 零依赖 HTTP 服务（Python 标准库）。

运行：python -m app.server   或   python app/server.py
默认端口 8199，环境变量见 README。
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from composer import compose  # noqa: E402
from extract import EngineUnavailable, decode_data_url, extract  # noqa: E402
from lexicon import Lexicon, LexiconError  # noqa: E402
from workflow import build_workflow  # noqa: E402

VERSION = "1.0.0"
STATIC_DIR = APP_DIR / "static"
DATA_DIR = Path(os.environ.get("DATA_DIR") or (ROOT_DIR / "data"))
MODEL_DIR = Path(os.environ.get("MODEL_DIR") or (ROOT_DIR / "models" / "wd14"))
USER_PRESETS = DATA_DIR / "user_presets.json"
COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188").rstrip("/")
PORT = int(os.environ.get("PORT", "8199"))
MAX_BODY = 32 * 1024 * 1024
REMOTE_CFG = {
    "url": os.environ.get("REMOTE_VISION_URL", ""),
    "key": os.environ.get("REMOTE_VISION_KEY", ""),
    "model": os.environ.get("REMOTE_VISION_MODEL", "gpt-4o-mini"),
    "mode": os.environ.get("REMOTE_VISION_MODE", "openai"),
    "timeout": int(os.environ.get("REMOTE_VISION_TIMEOUT", "45")),
}
ENGINE_CFG = {
    "model_dir": str(MODEL_DIR),
    "remote": REMOTE_CFG,
    "threshold": float(os.environ.get("WD14_THRESHOLD", "0.35")),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
LOG = logging.getLogger("prompt-studio")

try:
    LEX = Lexicon(DATA_DIR)
except LexiconError as exc:
    LOG.error("词库加载失败: %s", exc)
    raise

_PRESET_LOCK = threading.Lock()


# --------------------------------------------------------------------------- 工具

def _user_presets() -> list[dict]:
    if not USER_PRESETS.exists():
        return []
    try:
        data = json.loads(USER_PRESETS.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        LOG.warning("用户预设文件损坏，已忽略: %s", USER_PRESETS)
        return []


def _builtin_presets() -> list[dict]:
    path = DATA_DIR / "presets.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        LOG.warning("内置预设读取失败: %s", path)
        return []


def _save_user_presets(items: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = USER_PRESETS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(USER_PRESETS)


def _engine_status() -> dict:
    model_ready = (MODEL_DIR / "model.onnx").exists() and (MODEL_DIR / "selected_tags.csv").exists()
    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
        from PIL import Image  # noqa: F401
        runtime_ready = True
    except ImportError:
        runtime_ready = False
    return {
        "metadata": {"available": True, "label": "图片元数据（零依赖）"},
        "wd14": {
            "available": model_ready and runtime_ready,
            "label": "WD14 ONNX 本地反推",
            "model_ready": model_ready,
            "runtime_ready": runtime_ready,
            "hint": "" if model_ready and runtime_ready else (
                "模型未就绪：把 model.onnx 与 selected_tags.csv 放入 models/wd14/" if not model_ready
                else "推理依赖未安装：用 --build-arg WITH_ONNX=1 重建镜像"
            ),
        },
        "remote": {
            "available": bool(REMOTE_CFG["url"]),
            "label": "外部视觉接口",
            "hint": "" if REMOTE_CFG["url"] else "设置 REMOTE_VISION_URL 后启用",
        },
    }


# --------------------------------------------------------------------------- Handler

class Handler(BaseHTTPRequestHandler):
    server_version = f"PromptStudio/{VERSION}"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A002
        LOG.info("%s %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj: dict) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, status: int, message: str, hint: str = "") -> None:
        self._json(status, {"ok": False, "error": message, "hint": hint})

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError(f"请求体 {length} 字节超过 {MAX_BODY} 上限")
        raw = self.rfile.read(length)
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"请求体不是合法 JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return obj

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(204, b"", "text/plain; charset=utf-8")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path in ("/", "/index.html"):
                self._serve_static("index.html")
            elif path.startswith("/static/"):
                self._serve_static(path[len("/static/"):])
            elif path == "/api/health":
                self._json(200, {"ok": True, "version": VERSION, "engines": _engine_status(), "comfyui_url": COMFYUI_URL})
            elif path == "/api/lexicon":
                self._json(200, {"ok": True, "lexicon": LEX.data})
            elif path == "/api/guide":
                self._json(200, {"ok": True, "markdown": LEX.guide()})
            elif path == "/api/presets":
                self._json(200, {"ok": True, "presets": _builtin_presets() + _user_presets()})
            elif path == "/api/engines":
                self._json(200, {"ok": True, "engines": _engine_status()})
            else:
                self._error(404, f"未找到路由: {path}")
        except (OSError, ValueError) as exc:
            self._error(400, str(exc))
        except Exception as exc:  # noqa: BLE001
            LOG.exception("GET %s 失败", path)
            self._error(500, f"服务器内部错误: {exc}")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            payload = self._body()
            if path == "/api/compose":
                self._json(200, {"ok": True, **compose(payload, LEX)})
            elif path == "/api/extract":
                image, mime = decode_data_url(str(payload.get("image") or ""))
                engine = str(payload.get("engine") or "auto")
                cfg = dict(ENGINE_CFG)
                if isinstance(payload.get("threshold"), (int, float)):
                    cfg["threshold"] = max(0.05, min(0.95, float(payload["threshold"])))
                result = extract(image, mime, engine, cfg, LEX, str(payload.get("filename") or ""))
                self._json(200, {"ok": True, **result})
            elif path == "/api/workflow":
                wf = build_workflow(
                    str(payload.get("positive") or ""),
                    str(payload.get("negative") or ""),
                    payload.get("options") or {},
                )
                self._json(200, {"ok": True, "workflow": wf})
            elif path == "/api/presets":
                name = str(payload.get("name") or "").strip()[:60]
                if not name:
                    raise ValueError("预设名称不能为空")
                selections = payload.get("selections") or {}
                if not isinstance(selections, dict):
                    raise ValueError("selections 必须是对象")
                item = {
                    "id": "user_" + str(abs(hash(name + json.dumps(selections, ensure_ascii=False, sort_keys=True))))[:12],
                    "name": name,
                    "builtin": False,
                    "mode": payload.get("mode", "tag"),
                    "model": payload.get("model", "sdxl"),
                    "syntax": payload.get("syntax", "paren_weight"),
                    "selections": selections,
                }
                with _PRESET_LOCK:
                    items = [p for p in _user_presets() if p.get("name") != name]
                    items.append(item)
                    _save_user_presets(items)
                self._json(200, {"ok": True, "preset": item})
            elif path == "/api/push":
                workflow = payload.get("workflow")
                if not isinstance(workflow, dict):
                    raise ValueError("workflow 必须是对象")
                body = json.dumps({"prompt": workflow, "client_id": "prompt-studio"}).encode("utf-8")
                req = urllib.request.Request(
                    COMFYUI_URL + "/prompt", data=body,
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        answer = json.loads(resp.read().decode("utf-8", "replace"))
                    self._json(200, {"ok": True, "response": answer, "comfyui_url": COMFYUI_URL})
                except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                    self._error(502, f"推送 ComfyUI 失败: {exc}", "检查 COMFYUI_URL，并确认 ComfyUI 已启动、允许跨域/API 访问")
            else:
                self._error(404, f"未找到路由: {path}")
        except ValueError as exc:
            self._error(400, str(exc))
        except EngineUnavailable as exc:
            self._json(422, {"ok": False, "error": "引擎不可用", "hint": str(exc)})
        except Exception as exc:  # noqa: BLE001
            LOG.exception("POST %s 失败", path)
            self._error(500, f"服务器内部错误: {exc}")

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/presets":
            self._error(404, f"未找到路由: {parsed.path}")
            return
        query = urllib.parse.parse_qs(parsed.query)
        preset_id = (query.get("id") or [""])[0]
        with _PRESET_LOCK:
            items = _user_presets()
            kept = [p for p in items if p.get("id") != preset_id]
            if len(kept) == len(items):
                self._error(404, "未找到该用户预设（内置预设不可删除）")
                return
            _save_user_presets(kept)
        self._json(200, {"ok": True, "deleted": preset_id})

    def _serve_static(self, relative: str) -> None:
        target = (STATIC_DIR / relative).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            self._error(404, f"静态文件不存在: {relative}")
            return
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    LOG.info("ComfyUI Prompt Studio v%s 已启动: http://0.0.0.0:%s", VERSION, PORT)
    LOG.info("数据目录=%s | 模型目录=%s | ComfyUI=%s", DATA_DIR, MODEL_DIR, COMFYUI_URL)
    LOG.info("引擎状态: %s", json.dumps(_engine_status(), ensure_ascii=False))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("收到中断，正在关闭")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
