"""ComfyUI Prompt Studio — 零依赖 HTTP 服务（Python 标准库）。

运行：python -m app.server   或   python app/server.py
默认端口 8199，环境变量见 README。
"""
from __future__ import annotations

import base64
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

from auth import Auth, AuthError, is_https, is_private_host  # noqa: E402
from poses import PoseIndex  # noqa: E402
from studio import ComfyClient, StudioLibrary  # noqa: E402
from composer import compose  # noqa: E402
from extract import EngineUnavailable, decode_data_url, extract  # noqa: E402
from gallery import Gallery  # noqa: E402
from lexicon import Lexicon, LexiconError  # noqa: E402
from workflow import (  # noqa: E402
    WORKFLOW_ENGINES, build_for_engine, build_workflow,
    engine_defaults, is_zimage_workflow,
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in ("1", "true", "yes", "on")


VERSION = "1.2.0"
STATIC_DIR = APP_DIR / "static"
DATA_DIR = Path(os.environ.get("DATA_DIR") or (ROOT_DIR / "data"))
MODEL_DIR = Path(os.environ.get("MODEL_DIR") or (ROOT_DIR / "models" / "wd14"))
USER_PRESETS = DATA_DIR / "user_presets.json"

# COMFYUI_URL 允许内嵌凭据 http://user:pass@host:port（ComfyUI 开 --auth 时必需）。
# 凭据只用于生成 Authorization 头，绝不会出现在日志或 API 响应里。
_c = urllib.parse.urlsplit(os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188").rstrip("/"))
_netloc = _c.hostname or "127.0.0.1"
if ":" in _netloc and not _netloc.startswith("["):  # IPv6
    _netloc = f"[{_netloc}]"
if _c.port:
    _netloc += f":{_c.port}"
COMFYUI_URL = urllib.parse.urlunsplit((_c.scheme or "http", _netloc, _c.path.rstrip("/"), "", ""))
_COMFY_AUTH = ""
if _c.username is not None:
    _cred = _c.username + ((":" + _c.password) if _c.password else "")
    _COMFY_AUTH = "Basic " + base64.b64encode(urllib.parse.unquote(_cred).encode()).decode()


def _comfy_headers(extra: dict | None = None) -> dict:
    """所有到 ComfyUI 的请求（/prompt、/history、/view）统一带鉴权头。"""
    headers = dict(extra or {})
    if _COMFY_AUTH:
        headers["Authorization"] = _COMFY_AUTH
    return headers


# 出图引擎：sd（原行为，默认） / zimage-gguf（8188） / zimage-int8（8189，推荐）
WORKFLOW_ENGINE = os.environ.get("WORKFLOW_ENGINE", "sd").strip().lower()
if WORKFLOW_ENGINE not in WORKFLOW_ENGINES:
    WORKFLOW_ENGINE = "sd"

PORT = _env_int("PORT", 8199)
MAX_BODY = 32 * 1024 * 1024
REMOTE_CFG = {
    "url": os.environ.get("REMOTE_VISION_URL", ""),
    "key": os.environ.get("REMOTE_VISION_KEY", ""),
    "model": os.environ.get("REMOTE_VISION_MODEL", "gpt-4o-mini"),
    "mode": os.environ.get("REMOTE_VISION_MODE", "openai"),
    "timeout": _env_int("REMOTE_VISION_TIMEOUT", 45),
}
try:
    _threshold = float(os.environ.get("WD14_THRESHOLD", "0.35"))
except ValueError:
    _threshold = 0.35
ENGINE_CFG = {
    "model_dir": str(MODEL_DIR),
    "remote": REMOTE_CFG,
    "threshold": _threshold,
}

# 跨站白名单：默认空 = 完全同源，不再放行任意站点调用 API。
ALLOWED_ORIGINS = [
    origin.strip().rstrip("/")
    for origin in os.environ.get("ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]

# 可选：挂载 anhao/ai-pose-gallery-zh 仓库（只读）。镜像内不含任何上游数据/图片。
REFERENCE_DIR = Path(os.environ.get("REFERENCE_DIR") or (ROOT_DIR / "reference"))
POSE_REFERENCE_ENABLED = _env_bool("POSE_REFERENCE_ENABLED", True)
POSE_ADULT_MODE = _env_bool("POSE_ADULT_MODE", False)
DEFAULT_VIEW = "workbench" if os.environ.get("DEFAULT_VIEW", "studio").strip().lower() == "workbench" else "studio"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
LOG = logging.getLogger("prompt-studio")

try:
    LEX = Lexicon(DATA_DIR)
except LexiconError as exc:
    LOG.error("词库加载失败: %s", exc)
    raise

AUTH = Auth(
    DATA_DIR,
    app_password=os.environ.get("APP_PASSWORD", ""),
    app_username=os.environ.get("APP_USERNAME", "admin"),
    session_ttl_days=_env_int("SESSION_TTL_DAYS", 30),
    trust_proxy=_env_bool("TRUST_PROXY", True),
    require_https=_env_bool("REQUIRE_HTTPS", False),
    allow_public_setup=_env_bool("ALLOW_PUBLIC_SETUP", False),
    max_attempts=_env_int("LOGIN_MAX_ATTEMPTS", 5),
    lockout_minutes=_env_int("LOGIN_LOCKOUT_MINUTES", 5),
)
LOG.info("登录保护：%s", "已启用（管理员已创建）" if AUTH.initialized else "待首次在内网创建管理员")

POSES = PoseIndex(REFERENCE_DIR, POSE_REFERENCE_ENABLED, POSE_ADULT_MODE)
STUDIO = StudioLibrary(POSES)
COMFY = ComfyClient(COMFYUI_URL, headers_fn=_comfy_headers)
LOG.info("姿势参考库：%s", POSES.status()["reason"] or f"已载入 {len(POSES.items)} 个姿势")
LOG.info("AI 合成台：三库 %s | ComfyUI=%s | 默认视图=%s",
         STUDIO.visible_counts(), COMFYUI_URL, DEFAULT_VIEW)

_PRESET_LOCK = threading.Lock()

# 出图自动归档：图片落在 DATA_DIR/gallery（已持久化卷），B 房关机后仍可查看
GALLERY = Gallery(
    DATA_DIR,
    COMFYUI_URL,
    _comfy_headers,
    enabled=_env_bool("GALLERY_ENABLED", True),
    poll_interval=_env_float("GALLERY_POLL_INTERVAL", 8.0),
    history_limit=_env_int("GALLERY_HISTORY_LIMIT", 64),
    timeout=_env_float("GALLERY_TIMEOUT", 15.0),
    max_image_bytes=_env_int("GALLERY_MAX_IMAGE_MB", 20) * 1024 * 1024,
    max_items=_env_int("GALLERY_MAX_ITEMS", 0),
    init_mode=os.environ.get("GALLERY_INIT_MODE", "from_now"),
)


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
        origin = self.headers.get("Origin")
        if origin and origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj: dict, extra: dict | None = None) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", extra)

    def _error(self, status: int, message: str, hint: str = "") -> None:
        self._json(status, {"ok": False, "error": message, "hint": hint})

    @property
    def peer_ip(self) -> str:
        return self.client_address[0] if self.client_address else ""

    @property
    def secure(self) -> bool:
        return is_https(self.headers, AUTH.trust_proxy)

    def _redirect(self, location: str, status: int = 302) -> None:
        self._send(status, b"", "text/plain; charset=utf-8", {"Location": location})

    def _enforce_transport(self, parsed) -> bool:
        """REQUIRE_HTTPS 时把 HTTP 请求 308 到 HTTPS；健康检查例外，避免容器被判不健康。"""
        if AUTH.require_https and parsed.path != "/api/health" and not self.secure:
            host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or ""
            target = "https://" + host + parsed.path + (("?" + parsed.query) if parsed.query else "")
            self._redirect(target, 308)
            return True
        return False

    def _check_origin(self) -> bool:
        """写操作只接受同源或白名单 Origin，挡掉跨站调用。"""
        if self.command in ("GET", "HEAD", "OPTIONS"):
            return True
        origin = self.headers.get("Origin")
        if not origin:  # 非浏览器客户端（curl / 健康检查）无 Origin
            return True
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or ""
        if origin in (f"http://{host}", f"https://{host}") or origin in ALLOWED_ORIGINS:
            return True
        self._error(403, "跨站请求被拒绝")
        return False

    def _guard(self, path: str) -> bool:
        if path in ("/api/health", "/login") or path.startswith("/api/auth/"):
            return True
        if AUTH.initialized and AUTH.resolve(AUTH.token_from(self.headers)):
            return True
        if path.startswith("/api/"):
            self._json(401, {"ok": False, "error": "未登录或会话已过期", "code": "unauthorized"})
        else:
            self._redirect("/login")
        return False

    def _serve_login(self) -> None:
        path = STATIC_DIR / "login.html"
        if not path.is_file():
            self._error(404, "登录页缺失")
            return
        self._send(200, path.read_bytes(), "text/html; charset=utf-8")

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
            if self._enforce_transport(parsed):
                return
            if path == "/login":
                if AUTH.initialized and AUTH.resolve(AUTH.token_from(self.headers)):
                    self._redirect("/")
                else:
                    self._serve_login()
                return
            if path == "/api/auth/status":
                self._json(200, {"ok": True, **AUTH.status(self.peer_ip)})
                return
            if not self._guard(path):
                return
            if path == "/api/health":
                # engines 是前端 boot() 用来画反推引擎徽章的，之前漏了会抛异常
                self._json(200, {
                    "ok": True,
                    "version": VERSION,
                    "auth": AUTH.status(self.peer_ip),
                    "engines": _engine_status(),
                    "workflow_engine": WORKFLOW_ENGINE,
                    "workflow_defaults": engine_defaults(WORKFLOW_ENGINE),
                    "gallery": {"enabled": GALLERY.enabled, "online": GALLERY.status()["online"]},
                })
            elif path in ("/", "/index.html"):
                self._serve_static("index.html")
            elif path.startswith("/static/"):
                self._serve_static(path[len("/static/"):])
            elif path == "/api/lexicon":
                self._json(200, {"ok": True, "lexicon": LEX.data})
            elif path == "/api/guide":
                self._json(200, {"ok": True, "markdown": LEX.guide()})
            elif path == "/api/presets":
                self._json(200, {"ok": True, "presets": _builtin_presets() + _user_presets()})
            elif path == "/api/engines":
                self._json(200, {"ok": True, "engines": _engine_status()})
            elif path == "/api/poses/status":
                self._json(200, {"ok": True, **POSES.status()})
            elif path == "/api/poses":
                query = urllib.parse.parse_qs(parsed.query)
                try:
                    limit = max(1, min(200, int((query.get("limit") or ["60"])[0])))
                except ValueError:
                    limit = 60
                items = POSES.search(
                    (query.get("q") or [""])[0],
                    (query.get("category") or [""])[0],
                    limit,
                )
                self._json(200, {"ok": True, "items": items, "status": POSES.status()})
            elif path == "/api/pose/image":
                query = urllib.parse.parse_qs(parsed.query)
                target = POSES.resolve_image((query.get("path") or [""])[0])
                if target is None:
                    self._error(404, "图片不存在或不在 SFW 索引内")
                else:
                    self._send(200, target.read_bytes(), POSES.content_type(target))
            elif path == "/api/gallery":
                query = urllib.parse.parse_qs(parsed.query)
                try:
                    limit = max(1, min(200, int((query.get("limit") or ["60"])[0])))
                    offset = max(0, int((query.get("offset") or ["0"])[0]))
                except ValueError:
                    limit, offset = 60, 0
                self._json(200, GALLERY.list_records(
                    (query.get("q") or [""])[0], limit, offset
                ))
            elif path == "/api/gallery/image":
                query = urllib.parse.parse_qs(parsed.query)
                target = GALLERY.resolve_image((query.get("file") or [""])[0])
                if target is None:
                    self._error(404, "图片不存在或不在归档索引内")
                else:
                    self._send(200, target.read_bytes(), Gallery.content_type(target))
            elif path == "/api/studio/library":
                self._json(200, {"ok": True, **STUDIO.status()})
            elif path == "/api/studio/pick":
                query = urllib.parse.parse_qs(parsed.query)
                item = STUDIO.pick(
                    (query.get("kind") or ["portrait"])[0],
                    (query.get("same") or [""])[0],
                    (query.get("exclude") or [""])[0],
                )
                self._json(200, {"ok": True, "item": item})
            elif path == "/api/studio/image":
                query = urllib.parse.parse_qs(parsed.query)
                target = STUDIO.resolve_image((query.get("path") or [""])[0])
                if target is None:
                    self._error(404, "图片不存在或不在当前可见索引内")
                else:
                    self._send(200, target.read_bytes(), StudioLibrary.content_type(target))
            elif path == "/api/studio/workflow":
                self._json(200, {"ok": True, "workflow": build_workflow("", "", {})})
            elif path == "/api/comfy/status":
                self._json(200, {"ok": True, "comfy": COMFY.status()})
            elif path == "/api/comfy/result":
                query = urllib.parse.parse_qs(parsed.query)
                prompt_id = (query.get("id") or [""])[0]
                if not prompt_id:
                    raise ValueError("缺少 id")
                try:
                    self._json(200, {"ok": True, **COMFY.result(prompt_id)})
                except Exception as exc:  # noqa: BLE001
                    self._error(502, "查询出图结果失败", COMFY.diagnose(exc))
            elif path == "/api/comfy/view":
                query = urllib.parse.parse_qs(parsed.query)
                try:
                    raw, ctype = COMFY.view(
                        (query.get("filename") or [""])[0],
                        (query.get("subfolder") or [""])[0],
                        (query.get("type") or ["output"])[0],
                    )
                    self._send(200, raw, ctype)
                except Exception as exc:  # noqa: BLE001
                    self._error(502, "读取出图失败", COMFY.diagnose(exc))
            else:
                self._error(404, f"未找到路由: {path}")
        except (OSError, ValueError) as exc:
            self._error(400, str(exc))
        except Exception as exc:  # noqa: BLE001
            LOG.exception("GET %s 失败", path)
            self._error(500, f"服务器内部错误: {exc}")

    def _handle_auth(self, path: str, payload: dict) -> None:
        ip = AUTH.client_ip(self.headers, self.peer_ip)
        ua = self.headers.get("User-Agent", "") or ""
        token = AUTH.token_from(self.headers)
        secure = self.secure
        if path == "/api/auth/status":
            self._json(200, {"ok": True, **AUTH.status(self.peer_ip)})
        elif path == "/api/auth/setup":
            issued = AUTH.setup(
                str(payload.get("username") or "admin"),
                str(payload.get("password") or ""),
                self.peer_ip, ip, ua,
            )
            self._json(200, {"ok": True, "message": "管理员已创建"},
                       {"Set-Cookie": AUTH.cookie(issued, secure, True)})
        elif path == "/api/auth/login":
            issued = AUTH.login(
                str(payload.get("username") or ""),
                str(payload.get("password") or ""),
                ip, ua,
            )
            self._json(200, {"ok": True, "message": "登录成功"},
                       {"Set-Cookie": AUTH.cookie(issued, secure, bool(payload.get("remember", True)))})
        elif path == "/api/auth/logout":
            if not AUTH.resolve(token):
                self._json(401, {"ok": False, "error": "未登录", "code": "unauthorized"})
                return
            AUTH.logout(token)
            self._json(200, {"ok": True}, {"Set-Cookie": AUTH.clear_cookie(secure)})
        elif path == "/api/auth/password":
            if not AUTH.resolve(token):
                self._json(401, {"ok": False, "error": "未登录", "code": "unauthorized"})
                return
            AUTH.change_password(
                str(payload.get("old_password") or ""),
                str(payload.get("new_password") or ""),
            )
            self._json(200, {"ok": True, "message": "口令已修改，请重新登录"},
                       {"Set-Cookie": AUTH.clear_cookie(secure)})
        else:
            self._error(404, f"未找到路由: {path}")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if self._enforce_transport(parsed):
                return
            if not self._check_origin():
                return
            payload = self._body()
            if path.startswith("/api/auth/"):
                self._handle_auth(path, payload)
                return
            if not self._guard(path):
                return
            if path == "/api/poses/adult":
                if payload.get("confirm") is not True or str(payload.get("ack") or "") != "adult":
                    raise ValueError("需要显式确认：confirm=true 且 ack=adult")
                if not POSES.adult_mode:
                    raise PermissionError("成人模式未启用：在 compose 中设置 POSE_ADULT_MODE=true")
                if not is_private_host(self.peer_ip):
                    raise PermissionError("成人模式只能在内网直连时解锁")
                if payload.get("unlock") is False:
                    POSES.lock()
                else:
                    POSES.unlock()
                STUDIO.load()  # 三库可见范围随成人模式变化
                self._json(200, {"ok": True, "status": POSES.status()})
                return
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
                engine = str(payload.get("engine") or WORKFLOW_ENGINE).strip().lower()
                if engine not in WORKFLOW_ENGINES:
                    engine = WORKFLOW_ENGINE
                options = payload.get("options") or {}
                wf = build_for_engine(
                    engine,
                    str(payload.get("positive") or ""),
                    str(payload.get("negative") or ""),
                    options,
                )
                self._json(200, {"ok": True, "workflow": wf, "engine": engine, "options": options})
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
                engine = str(payload.get("engine") or WORKFLOW_ENGINE).strip().lower()
                if engine not in WORKFLOW_ENGINES:
                    engine = WORKFLOW_ENGINE

                # 兼容旧前端 / 外部调用传来的 SD 工作流：抽出文本后换成 Z-Image 节点
                if engine.startswith("zimage") and not is_zimage_workflow(workflow):
                    def _text(node_id: str, fallback: str) -> str:
                        try:
                            return workflow[node_id]["inputs"]["text"] or ""
                        except (KeyError, TypeError, AttributeError):
                            return str(payload.get(fallback) or "")

                    workflow = build_for_engine(
                        engine,
                        _text("2", "positive"),
                        _text("3", "negative"),
                        payload.get("options") or {},
                    )

                body = json.dumps({"prompt": workflow, "client_id": "prompt-studio"}).encode("utf-8")
                req = urllib.request.Request(
                    COMFYUI_URL + "/prompt", data=body,
                    headers=_comfy_headers({"Content-Type": "application/json"}), method="POST",
                )
                try:
                    # /prompt 只是入队，不会等出图；30s 覆盖隧道冷启动的抖动
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        answer = json.loads(resp.read().decode("utf-8", "replace"))
                    # 立刻唤醒归档线程，出完图后不用等下一个轮询周期
                    GALLERY.trigger()
                    self._json(200, {
                        "ok": True, "response": answer,
                        "comfyui_url": COMFYUI_URL, "engine": engine,
                    })
                except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                    self._error(502, f"推送 ComfyUI 失败: {exc}",
                                "检查 COMFYUI_URL / WORKFLOW_ENGINE、frp 隧道与 ComfyUI --auth 凭据")
            elif path == "/api/comfy/upload":
                image, _mime = decode_data_url(str(payload.get("image") or ""))
                if len(image) > 20 * 1024 * 1024:
                    raise ValueError("参考图超过 20 MB 上限")
                try:
                    uploaded = COMFY.upload_image(image, str(payload.get("name") or "reference.png"))
                    self._json(200, {"ok": True, "uploaded": uploaded})
                except Exception as exc:  # noqa: BLE001
                    self._error(502, "上传参考图到 ComfyUI 失败", COMFY.diagnose(exc))
            elif path == "/api/comfy/submit":
                workflow = payload.get("workflow")
                if not isinstance(workflow, dict):
                    raise ValueError("workflow 必须是对象")
                uploaded = payload.get("images") or []
                if not isinstance(uploaded, list):
                    raise ValueError("images 必须是数组")
                try:
                    result = COMFY.submit(
                        workflow,
                        str(payload.get("positive") or ""),
                        str(payload.get("negative") or ""),
                        uploaded,
                    )
                    GALLERY.trigger()  # 与已有出图归档联动
                    self._json(200, {"ok": True, **result})
                except Exception as exc:  # noqa: BLE001
                    self._error(502, "提交 ComfyUI 失败", COMFY.diagnose(exc))
            elif path == "/api/gallery/scan":
                # 手动全量扫描：把 ComfyUI 里所有没归档过的历史出图补回来
                result = GALLERY.scan_once(limit=0)
                self._json(200, {"ok": True, **result, "status": GALLERY.status()})
            else:
                self._error(404, f"未找到路由: {path}")
        except ValueError as exc:
            self._error(400, str(exc))
        except AuthError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
        except PermissionError as exc:
            self._error(403, str(exc))
        except EngineUnavailable as exc:
            self._json(422, {"ok": False, "error": "引擎不可用", "hint": str(exc)})
        except Exception as exc:  # noqa: BLE001
            LOG.exception("POST %s 失败", path)
            self._error(500, f"服务器内部错误: {exc}")

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        try:
            if self._enforce_transport(parsed):
                return
            if not self._check_origin():
                return
            if not self._guard(parsed.path):
                return
            query = urllib.parse.parse_qs(parsed.query)
            if parsed.path == "/api/gallery":
                record_id = (query.get("id") or [""])[0]
                if not GALLERY.delete(record_id):
                    self._error(404, "未找到该出图记录")
                    return
                self._json(200, {"ok": True, "deleted": record_id})
                return
            if parsed.path != "/api/presets":
                self._error(404, f"未找到路由: {parsed.path}")
                return
            preset_id = (query.get("id") or [""])[0]
            with _PRESET_LOCK:
                items = _user_presets()
                kept = [p for p in items if p.get("id") != preset_id]
                if len(kept) == len(items):
                    self._error(404, "未找到该用户预设（内置预设不可删除）")
                    return
                _save_user_presets(kept)
            self._json(200, {"ok": True, "deleted": preset_id})
        except Exception as exc:  # noqa: BLE001
            LOG.exception("DELETE %s 失败", parsed.path)
            self._error(500, f"服务器内部错误: {exc}")

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
    LOG.info("出图引擎=%s | 默认参数=%s | 自动归档=%s", WORKFLOW_ENGINE,
             json.dumps(engine_defaults(WORKFLOW_ENGINE), ensure_ascii=False), GALLERY.enabled)
    LOG.info("引擎状态: %s", json.dumps(_engine_status(), ensure_ascii=False))
    GALLERY.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("收到中断，正在关闭")
    finally:
        GALLERY.stop()
        server.server_close()


if __name__ == "__main__":
    main()
