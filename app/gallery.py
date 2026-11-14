"""ComfyUI 出图自动归档（纯 Python 标准库）。

后台守护线程定期轮询 ComfyUI 的 /history，把「新出现且已出图」的记录里的
图片经 /view 拉回，写到 DATA_DIR/gallery/YYYYMM/ 并把元数据写进
DATA_DIR/gallery.json。DATA_DIR 在 Unraid 上已经是持久化卷，因此图片
在容器重建、B 房 ComfyUI 关机后依然保留，可随时在工作台里查看 / 搜索 /
复用提示词。

覆盖两种出图来源：
  · 工作台「推送到 ComfyUI」提交的任务（推送成功后会立即唤醒一次扫描）
  · 用户在 ComfyUI 网页里手动 Queue 的任务（由轮询兜底扫到）

不做的事：不改动 ComfyUI、不需要 B 房额外端口、不共享 SMB 目录。
"""
from __future__ import annotations

import json
import logging
import mimetypes
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

LOG = logging.getLogger("prompt-studio.gallery")

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_TEXT_NODES = {"CLIPTextEncode", "CLIPTextEncodeSDXL", "CLIPTextEncodeFlux"}
_LATENT_NODES = {"EmptySD3LatentImage", "EmptyLatentImage"}
_MODEL_KEYS = ("unet_name", "ckpt_name", "checkpoint_name", "model_name")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _node_sort_key(key) -> tuple:
    text = str(key)
    return (0, int(text)) if text.isdigit() else (1, text)


def _describe(prompt_id: str, entry: dict) -> dict:
    """从 /history 的条目里反解出提示词与采样参数，供画廊检索与复用。"""
    raw_prompt = (entry or {}).get("prompt")
    if isinstance(raw_prompt, list) and len(raw_prompt) > 2 and isinstance(raw_prompt[2], dict):
        workflow = raw_prompt[2]
    elif isinstance(raw_prompt, dict):
        workflow = raw_prompt
    else:
        workflow = {}

    texts: list[str] = []
    params: dict = {}
    model = ""
    engine = "unknown"
    for key in sorted(workflow, key=_node_sort_key):
        node = workflow[key]
        if not isinstance(node, dict):
            continue
        ct = str(node.get("class_type") or "")
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        if ct in _TEXT_NODES:
            texts.append(str(inputs.get("text") or ""))
        elif ct == "KSampler":
            for field in ("seed", "steps", "cfg", "sampler_name", "scheduler"):
                if field in inputs:
                    params[field] = inputs[field]
        elif ct in _LATENT_NODES:
            params["width"] = inputs.get("width")
            params["height"] = inputs.get("height")
        elif ct in ("UNETLoader", "UnetLoaderGGUF", "CheckpointLoaderSimple"):
            for name in _MODEL_KEYS:
                if inputs.get(name):
                    model = str(inputs[name])
                    break
            if ct == "UNETLoader":
                engine = "zimage-int8"
            elif ct == "UnetLoaderGGUF":
                engine = "zimage-gguf"
            else:
                engine = "sd"
        elif ct in ("CLIPLoader", "CLIPLoaderGGUF") and not model:
            model = str(inputs.get("clip_name") or "")

    # 前两个 CLIPTextEncode 依次是正向 / 负向（Z-Image 的负向常为空）
    positive = texts[0] if texts else ""
    negative = texts[1] if len(texts) > 1 else ""
    return {
        "id": prompt_id,
        "ts": _utc_now(),
        "engine": engine,
        "model": model,
        "positive": positive,
        "negative": negative,
        "seed": params.get("seed"),
        "steps": params.get("steps"),
        "cfg": params.get("cfg"),
        "sampler": params.get("sampler_name"),
        "scheduler": params.get("scheduler"),
        "width": params.get("width"),
        "height": params.get("height"),
    }


class Gallery:
    """出图归档存储 + 轮询守护线程。"""

    def __init__(
        self,
        data_dir: Path,
        comfyui_url: str,
        headers_fn,
        *,
        enabled: bool = True,
        poll_interval: float = 8.0,
        history_limit: int = 64,
        timeout: float = 15.0,
        max_image_bytes: int = 20 * 1024 * 1024,
        max_items: int = 0,
        init_mode: str = "from_now",
        pending_ttl: float = 900.0,
    ) -> None:
        self.root = Path(data_dir) / "gallery"
        self.index_file = Path(data_dir) / "gallery.json"
        self.comfyui_url = comfyui_url.rstrip("/")
        self.headers_fn = headers_fn
        self.enabled = enabled
        self.poll_interval = max(2.0, float(poll_interval))
        self.history_limit = max(1, int(history_limit))
        self.timeout = float(timeout)
        self.max_image_bytes = int(max_image_bytes)
        self.max_items = max(0, int(max_items))
        self.init_mode = "all" if init_mode == "all" else "from_now"
        self.pending_ttl = float(pending_ttl)

        self._lock = threading.RLock()
        self._records: list[dict] = []
        self._deleted: set[str] = set()
        self._seen: set[str] = set()
        self._pending: dict[str, float] = {}
        self._fails: dict[str, int] = {}
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._primed = False
        self._online: bool | None = None
        self._last_error = ""
        self._last_scan = 0.0
        self._fail_streak = 0
        self._load()

    # ------------------------------------------------------------------ 存储
    def _load(self) -> None:
        try:
            raw = json.loads(self.index_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(raw, list):  # 兼容旧格式：纯记录数组
            raw = {"items": raw, "deleted_ids": []}
        items = raw.get("items") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return
        self._records = [r for r in items if isinstance(r, dict) and r.get("id")]
        self._deleted = {str(x) for x in (raw.get("deleted_ids") or []) if isinstance(x, str)}
        self._seen = {str(r["id"]) for r in self._records} | self._deleted

    def _save_locked(self) -> None:
        try:
            self.index_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "items": self._records,
                "deleted_ids": sorted(self._deleted),
            }
            tmp = self.index_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.index_file)
        except OSError as exc:
            LOG.error("画廊索引写入失败: %s", exc)

    def _trim_locked(self) -> None:
        if not self.max_items or len(self._records) <= self.max_items:
            return
        overflow = self._records[:-self.max_items]
        self._records = self._records[-self.max_items:]
        for record in overflow:
            self._deleted.add(str(record["id"]))
            for image in record.get("images") or []:
                self._unlink(image.get("file"))

    def _unlink(self, relative: str | None) -> None:
        if not relative:
            return
        try:
            target = (self.root / relative).resolve()
            if str(target).startswith(str(self.root.resolve())) and target.is_file():
                target.unlink()
        except OSError:
            pass

    # ------------------------------------------------------------------ ComfyUI
    def _comfy_get(self, path: str) -> bytes:
        req = urllib.request.Request(
            self.comfyui_url + path, headers=self.headers_fn(), method="GET"
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.read()

    def _history(self, limit: int | None = None) -> dict:
        path = "/history"
        if limit:
            path += "?max_items=" + str(limit)
        data = json.loads(self._comfy_get(path).decode("utf-8", "replace"))
        if not isinstance(data, dict):
            raise ValueError("/history 返回的不是对象")
        return data

    def _view_url(self, image: dict) -> str:
        return "/view?" + urllib.parse.urlencode({
            "filename": str(image.get("filename") or ""),
            "subfolder": str(image.get("subfolder") or ""),
            "type": str(image.get("type") or "output"),
        })

    # ------------------------------------------------------------------ 归档
    def _prime(self) -> None:
        """from_now 模式：把 ComfyUI 现有历史登记为「已见」，避免容器启动瞬间
        把上百张旧图一次性拉回来打爆隧道 / 磁盘。"""
        for pid in self._history():
            self._seen.add(str(pid))
        self._primed = True
        LOG.info("出图归档已就绪：跳过 %d 条历史任务，只归档之后新生成的图片", len(self._seen))

    def _ingest(self, pid: str, entry: dict) -> dict | None:
        outputs = (entry or {}).get("outputs")
        images: list[dict] = []
        if isinstance(outputs, dict):
            for node_out in outputs.values():
                if isinstance(node_out, dict):
                    for image in node_out.get("images") or []:
                        if isinstance(image, dict) and image.get("filename"):
                            images.append(image)
        if not images:
            # 任务还在排队 / 尚未出图，先记为 pending，下轮再看
            self._pending.setdefault(pid, time.time())
            if time.time() - self._pending[pid] > self.pending_ttl:
                self._seen.add(pid)
                self._pending.pop(pid, None)
            return None

        meta = _describe(pid, entry)
        month = datetime.now(timezone.utc).strftime("%Y%m")
        folder = self.root / month
        saved: list[dict] = []
        for idx, image in enumerate(images):
            data = self._comfy_get(self._view_url(image))
            if len(data) > self.max_image_bytes:
                raise ValueError(f"图片超过 {self.max_image_bytes // 1024 // 1024} MB 上限，已跳过")
            safe = _SAFE.sub("_", str(image["filename"]))[-80:].lstrip(".") or "image.png"
            if not Path(safe).suffix:
                safe += ".png"
            name = f"{pid[:12]}_{idx}_{safe}"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(data)
            relative = f"{month}/{name}"
            saved.append({
                "file": relative,
                "name": str(image["filename"]),
                "bytes": len(data),
                "url": "/api/gallery/image?file=" + urllib.parse.quote(relative, safe=""),
            })
        meta["images"] = saved
        self._pending.pop(pid, None)
        self._fails.pop(pid, None)
        return meta

    def scan_once(self, limit: int | None = None) -> dict:
        """扫描一轮 /history，返回 {scanned, added, failed, skipped}。"""
        history = self._history(self.history_limit if limit is None else limit)
        added: list[dict] = []
        failed = 0
        skipped = 0
        for raw_pid, entry in history.items():
            pid = str(raw_pid)
            with self._lock:
                if pid in self._seen or pid in self._deleted:
                    continue
            try:
                record = self._ingest(pid, entry if isinstance(entry, dict) else {})
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as exc:
                failed += 1
                with self._lock:
                    count = self._fails.get(pid, 0) + 1
                    self._fails[pid] = count
                    if count >= 3:
                        self._seen.add(pid)
                        self._fails.pop(pid, None)
                if count < 3:
                    LOG.warning("归档 %s 失败（第 %d 次）: %s", pid[:12], count, exc)
                continue
            if record is None:
                skipped += 1
                continue
            added.append(record)
            with self._lock:
                self._seen.add(pid)

        with self._lock:
            if added:
                self._records.extend(added)
                self._trim_locked()
                self._save_locked()
            self._last_scan = time.time()
        return {
            "scanned": len(history),
            "added": len(added),
            "failed": failed,
            "skipped": skipped,
        }

    # ------------------------------------------------------------------ 守护线程
    def _loop(self) -> None:
        while not self._stop.is_set():
            interval = self.poll_interval
            try:
                if self.init_mode == "from_now" and not self._primed:
                    self._prime()
                result = self.scan_once()
                self._fail_streak = 0
                self._set_online(True)
                if result["added"]:
                    LOG.info("已自动归档 %d 张新出图（累计 %d 条记录）",
                             result["added"], len(self._records))
            except Exception as exc:  # noqa: BLE001 — 守护线程不能因为一次网络抖动退出
                self._fail_streak += 1
                self._last_error = str(exc)
                self._set_online(False)
                # 离线时指数退避，最多 60s，避免 ComfyUI 关机期间刷屏 / 空耗隧道
                interval = min(60.0, self.poll_interval * (2 ** min(self._fail_streak, 4)))
                if self._fail_streak == 1 or self._fail_streak % 10 == 0:
                    LOG.warning("出图归档暂不可用（第 %d 次，%.0fs 后重试）: %s",
                                self._fail_streak, interval, exc)
            self._wake.wait(interval)
            self._wake.clear()

    def _set_online(self, value: bool) -> None:
        if self._online is not value:
            LOG.info("ComfyUI %s", "已连接，出图归档恢复" if value else "不可达，自动归档暂停轮询")
            self._online = value
            if value:
                self._last_error = ""

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        if self.init_mode == "all":
            self._primed = True
        self._thread = threading.Thread(target=self._loop, name="gallery-archiver", daemon=True)
        self._thread.start()
        LOG.info("出图归档已启动: dir=%s | poll=%.0fs | init=%s",
                 self.root, self.poll_interval, self.init_mode)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def trigger(self) -> None:
        """推送成功后立即唤醒一次扫描，不必等下一个轮询周期。"""
        self._wake.set()

    # ------------------------------------------------------------------ 对外接口
    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": self.enabled,
                "online": bool(self._online),
                "items": len(self._records),
                "images": sum(len(r.get("images") or []) for r in self._records),
                "last_scan": self._last_scan,
                "last_error": self._last_error,
                "poll_interval": self.poll_interval,
                "init_mode": self.init_mode,
                "max_items": self.max_items,
            }

    def list_records(self, query: str = "", limit: int = 60, offset: int = 0) -> dict:
        needle = (query or "").strip().lower()
        with self._lock:
            items = list(reversed(self._records))
        if needle:
            items = [
                r for r in items
                if needle in " ".join(str(r.get(k) or "") for k in
                                     ("positive", "negative", "model", "engine", "id")).lower()
            ]
        total = len(items)
        page = items[offset:offset + max(1, min(200, limit))]
        return {"ok": True, "total": total, "items": page, "status": self.status()}

    def delete(self, prompt_id: str) -> bool:
        with self._lock:
            kept = [r for r in self._records if str(r.get("id")) != prompt_id]
            if len(kept) == len(self._records):
                return False
            removed = [r for r in self._records if str(r.get("id")) == prompt_id]
            self._records = kept
            self._deleted.add(prompt_id)
            self._seen.add(prompt_id)
            self._save_locked()
        for record in removed:
            for image in record.get("images") or []:
                self._unlink(image.get("file"))
        return True

    def resolve_image(self, relative: str) -> Path | None:
        """只允许读取 gallery.json 里登记过的文件，且必须落在归档目录内。"""
        if not relative:
            return None
        with self._lock:
            allowed = {img.get("file") for r in self._records for img in (r.get("images") or [])}
        if relative not in allowed:
            return None
        try:
            target = (self.root / relative).resolve()
            root = self.root.resolve()
            if root not in target.parents or not target.is_file():
                return None
        except OSError:
            return None
        return target

    @staticmethod
    def content_type(path: Path) -> str:
        return mimetypes.guess_type(path.name)[0] or "application/octet-stream"
