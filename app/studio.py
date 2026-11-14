"""AI 角色合成台：三库参考图索引 + 本地 ComfyUI 服务端代理。

镜像与仓库不含任何上游代码、数据或图片，全部来自用户自行挂载的只读卷。
ComfyUI 的所有请求都由本服务端转发：浏览器不直连 ComfyUI，
因此不需要 --enable-cors-header，也不存在浏览器里 127.0.0.1 指向自身的问题。
"""
from __future__ import annotations

import base64
import copy
import json
import logging
import mimetypes
import random
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from poses import HARD_BLOCKED, SFW_BLOCKED, PoseIndex

LOG = logging.getLogger("prompt-studio.studio")

COSTUME_ADULT_PARTS = ("内衣", "睡衣", "性感", "泳装")
PORTRAIT_SAFE_PARTS = ("正脸参考", "造型氛围")
IMAGE_FIELDS = ("缩略图", "原图", "预览图")


def _clean_path(value: str) -> str:
    value = str(value or "").strip()
    if not value or value.startswith("/") or ".." in Path(value).parts:
        return ""
    return value


class StudioLibrary:
    """人像 / 服装 / 姿势三库索引；可见范围跟随 PoseIndex 的成人模式。"""

    def __init__(self, poses: PoseIndex):
        self.dir = poses.dir
        self.poses = poses
        self.items: dict[str, list[dict]] = {"portrait": [], "costume": [], "pose": []}
        self.sfw_paths: set[str] = set()
        self.adult_paths: set[str] = set()
        self.reason = ""
        self.load()

    def _read(self, filename: str) -> list[dict]:
        path = self.dir / "data" / filename
        if not path.is_file():
            return []
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("读取 %s 失败: %s", filename, exc)
            return []
        return rows if isinstance(rows, list) else []

    @staticmethod
    def _blocked(text: str) -> bool:
        lower = text.lower()
        return any(part.lower() in lower for part in HARD_BLOCKED)

    def load(self) -> None:
        self.items = {"portrait": [], "costume": [], "pose": []}
        self.sfw_paths = set()
        self.adult_paths = set()
        if not self.poses.enabled:
            self.reason = "姿势参考库已禁用（POSE_REFERENCE_ENABLED=false）"
            return
        if not (self.dir / "data").is_dir():
            self.reason = "未挂载上游仓库：把 ai-pose-gallery-zh 挂到只读卷 /app/reference"
            return

        for row in self._read("portrait-zh.json"):
            if not isinstance(row, dict):
                continue
            title = str(row.get("标题") or row.get("发型名称") or "").strip()
            category = str(row.get("发长") or row.get("分区") or "人像").strip()
            if not title or self._blocked(title + category):
                continue
            thumb = _clean_path(row.get("缩略图") or "")
            full = _clean_path(row.get("原图") or "")
            item = {
                "id": str(row.get("id") or ""), "kind": "portrait",
                "title": title, "category": category,
                "sub": str(row.get("分区") or ""), "thumb": thumb, "full": full,
                "prompt": f"{category} hairstyle portrait reference", "adult": False,
            }
            self.items["portrait"].append(item)
            self.sfw_paths.update(p for p in (thumb, full) if p)

        for row in self._read("costume-zh.json"):
            if not isinstance(row, dict):
                continue
            title = str(row.get("服装名称") or "").strip()
            category = str(row.get("分类") or "服装").strip()
            if not title or self._blocked(title + category):
                continue
            adult = any(part in category for part in COSTUME_ADULT_PARTS)
            thumb = _clean_path(row.get("缩略图") or "")
            full = _clean_path(row.get("原图") or row.get("预览图") or "")
            item = {
                "id": str(row.get("id") or ""), "kind": "costume",
                "title": title, "category": category, "sub": "",
                "thumb": thumb, "full": full,
                "prompt": str(row.get("关键词") or "").strip(), "adult": adult,
            }
            self.items["costume"].append(item)
            (self.adult_paths if adult else self.sfw_paths).update(p for p in (thumb, full) if p)

        for item in self.poses.items:
            self.items["pose"].append({
                "id": item["id"], "kind": "pose", "title": item["name"],
                "category": item["category"], "sub": item["system"],
                "thumb": item["thumb"], "full": item["full"],
                "prompt": item["prompt"], "adult": bool(item["adult"]),
            })
            (self.adult_paths if item["adult"] else self.sfw_paths).update(
                p for p in (item["thumb"], item["full"]) if p
            )

        counts = {kind: len(rows) for kind, rows in self.items.items()}
        if not any(counts.values()):
            self.reason = "未找到可用的三库元数据（需要 data/pose-zh.json 等文件）"
        else:
            self.reason = ""
            LOG.info("三库索引已载入：人像 %s / 服装 %s / 姿势 %s",
                     counts["portrait"], counts["costume"], counts["pose"])

    def status(self) -> dict:
        visible = self.visible_counts()
        return {
            "available": any(visible.values()),
            "counts": visible,
            "adult_unlocked": self.poses.unlocked,
            "adult_mode": self.poses.adult_mode,
            "reference_dir": str(self.dir),
            "reason": self.reason,
            "note": "图片来自只读挂载卷，镜像不含任何上游数据；ComfyUI 请求由服务端代理。",
        }

    def visible_counts(self) -> dict:
        unlocked = self.poses.unlocked
        return {
            kind: sum(1 for item in rows if unlocked or not item["adult"])
            for kind, rows in self.items.items()
        }

    def pick(self, kind: str, same: str = "", exclude: str = "") -> dict | None:
        if kind not in self.items:
            return None
        pool = [item for item in self.items[kind] if self.poses.unlocked or not item["adult"]]
        if same:
            narrowed = [item for item in pool if item["category"] == same and item["id"] != exclude]
            if narrowed:
                pool = narrowed
        else:
            pool = [item for item in pool if item["id"] != exclude]
        if not pool:
            return None
        item = random.choice(pool)
        return {**item, "thumb_url": self.image_url(item["thumb"]), "full_url": self.image_url(item["full"])}

    @staticmethod
    def image_url(path: str) -> str:
        return "/api/studio/image?path=" + urllib.parse.quote(path or "", safe="") if path else ""

    def resolve_image(self, relative: str) -> Path | None:
        allowed = self.sfw_paths | (self.adult_paths if self.poses.unlocked else set())
        if relative not in allowed:
            return None
        root = self.dir.resolve()
        target = (self.dir / relative).resolve()
        if root not in target.parents or not target.is_file():
            return None
        return target

    @staticmethod
    def content_type(path: Path) -> str:
        return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


class ComfyClient:
    """本地 ComfyUI 的极简 HTTP 客户端（标准库实现，服务端代理）。"""

    def __init__(self, base_url: str, timeout: int = 20, headers_fn=None):
        self.base = (base_url or "http://127.0.0.1:8188").rstrip("/")
        self.timeout = timeout
        self.headers_fn = headers_fn
        self.client_id = "prompt-studio-" + uuid.uuid4().hex[:8]

    def _url(self, path: str, query: dict | None = None) -> str:
        suffix = ("?" + urllib.parse.urlencode(query)) if query else ""
        return self.base + path + suffix

    def request(self, path: str, *, method: str = "GET", body: bytes | None = None,
                ctype: str = "application/json", query: dict | None = None):
        headers = {"Content-Type": ctype, "Accept": "application/json, image/*"}
        if self.headers_fn:
            headers = self.headers_fn(headers)
        req = urllib.request.Request(
            self._url(path, query), data=body, method=method, headers=headers,
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()

    @staticmethod
    def diagnose(exc: Exception) -> str:
        text = str(exc)
        return (
            "无法连接 ComfyUI，可能原因：\n"
            "① COMFYUI_URL 配错（容器内 127.0.0.1 指向自己，应填 NAS 局域网 IP 或 host.docker.internal）；\n"
            "② ComfyUI 未启动，或端口不是 8188；\n"
            "③ Docker 网络隔离，容器访问不到宿主机（compose 已配 host-gateway）；\n"
            "④ 请求超时或代理拦截。\n"
            "注意：本工作台由服务端转发请求，浏览器无需 CORS 配置。\n"
            f"底层错误：{text[:220]}"
        )

    def status(self) -> dict:
        try:
            _, _, raw = self.request("/system_stats")
            data = json.loads(raw.decode("utf-8", "replace"))
            return {
                "ok": True, "url": self.base,
                "version": data.get("system", {}).get("comfyui_version", ""),
                "device": (data.get("devices") or [{}])[0].get("name", ""),
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "url": self.base, "error": self.diagnose(exc)}

    def upload_image(self, data: bytes, filename: str) -> dict:
        boundary = "----PromptStudio" + uuid.uuid4().hex
        safe = "".join(ch for ch in filename if ch.isalnum() or ch in "._-")[-80:] or "reference.png"
        ctype = mimetypes.guess_type(safe)[0] or "image/png"
        parts = [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="image"; filename="{safe}"\r\n'.encode(),
            f"Content-Type: {ctype}\r\n\r\n".encode(),
            data,
            f"\r\n--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n',
            f"--{boundary}--\r\n".encode(),
        ]
        body = b"".join(parts)
        _, _, raw = self.request(
            "/upload/image", method="POST", body=body,
            ctype=f"multipart/form-data; boundary={boundary}",
        )
        result = json.loads(raw.decode("utf-8", "replace"))
        return {
            "name": result.get("name") or safe,
            "subfolder": result.get("subfolder") or "",
            "type": result.get("type") or "input",
        }

    def inject(self, workflow: dict, positive: str, negative: str, uploaded: list[dict]) -> dict:
        wf = copy.deepcopy(workflow)
        nodes = wf.get("prompt", wf) if isinstance(wf, dict) else wf
        if not isinstance(nodes, dict):
            raise ValueError("工作流必须是 API 格式的 JSON 对象")
        pos_id = neg_id = None
        for node in nodes.values():
            if not isinstance(node, dict) or node.get("class_type") not in ("KSampler", "KSamplerAdvanced"):
                continue
            inputs = node.get("inputs", {})
            for key, target in (("positive", "pos"), ("negative", "neg")):
                link = inputs.get(key)
                if isinstance(link, list) and link:
                    if target == "pos":
                        pos_id = str(link[0])
                    else:
                        neg_id = str(link[0])
        clips = [nid for nid, node in nodes.items()
                 if isinstance(node, dict) and node.get("class_type") == "CLIPTextEncode"]
        if pos_id is None and clips:
            pos_id = clips[0]
        if neg_id is None and len(clips) > 1:
            neg_id = clips[1]
        if pos_id in nodes:
            nodes[pos_id].setdefault("inputs", {})["text"] = positive or ""
        if neg_id in nodes:
            nodes[neg_id].setdefault("inputs", {})["text"] = negative or ""
        if uploaded:
            for node in nodes.values():
                if isinstance(node, dict) and node.get("class_type") == "LoadImage":
                    node.setdefault("inputs", {})["image"] = uploaded[0]["name"]
        return nodes

    def submit(self, workflow: dict, positive: str, negative: str, uploaded: list[dict]) -> dict:
        nodes = self.inject(workflow, positive, negative, uploaded)
        body = json.dumps({"prompt": nodes, "client_id": self.client_id}).encode("utf-8")
        _, _, raw = self.request("/prompt", method="POST", body=body)
        answer = json.loads(raw.decode("utf-8", "replace"))
        if not answer.get("prompt_id"):
            raise ValueError(f"ComfyUI 未返回 prompt_id：{str(answer)[:200]}")
        return {"prompt_id": answer["prompt_id"], "node_count": len(nodes)}

    def result(self, prompt_id: str) -> dict:
        try:
            _, _, raw = self.request("/history/" + urllib.parse.quote(prompt_id, safe=""))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {"status": "running", "images": []}
            raise
        data = json.loads(raw.decode("utf-8", "replace"))
        entry = data.get(prompt_id)
        if not entry:
            return {"status": "running", "images": []}
        status = entry.get("status") or {}
        images = []
        for output in (entry.get("outputs") or {}).values():
            if not isinstance(output, dict):
                continue
            for image in output.get("images") or []:
                if not isinstance(image, dict) or not image.get("filename"):
                    continue
                query = {
                    "filename": image.get("filename", ""),
                    "subfolder": image.get("subfolder", ""),
                    "type": image.get("type", "output"),
                }
                images.append({**query, "url": "/api/comfy/view?" + urllib.parse.urlencode(query)})
        done = bool(status.get("completed")) or bool(images)
        if status.get("status_str") == "error" or not done and status.get("messages"):
            messages = status.get("messages") or []
            if any("error" in str(msg).lower() for msg in messages):
                return {"status": "error", "images": [], "message": str(messages[-1])[:300]}
        return {
            "status": "done" if done else "running",
            "status_str": status.get("status_str", ""),
            "images": images,
        }

    def view(self, filename: str, subfolder: str = "", image_type: str = "output"):
        if not filename or ".." in filename or "/" in filename or "\\" in filename:
            raise ValueError("非法的图片文件名")
        status, headers, raw = self.request(
            "/view", query={"filename": filename, "subfolder": subfolder, "type": image_type}
        )
        return raw, headers.get("Content-Type") or mimetypes.guess_type(filename)[0] or "image/png"
