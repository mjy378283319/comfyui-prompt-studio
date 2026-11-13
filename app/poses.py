"""姿势参考库：可选挂载 anhao/ai-pose-gallery-zh 仓库的只读卷。

镜像内不含任何上游数据与图片。只有用户自行 clone 并把仓库挂到 REFERENCE_DIR 时才启用，
且只索引 SFW 体系（全身单人・标准系 / 上半身单人），18+ 体系与敏感姿势名一律排除；
图片接口只放行索引内登记的路径，避免绕过过滤读取其它图片。
"""
from __future__ import annotations

import json
import logging
import mimetypes
from pathlib import Path

LOG = logging.getLogger("prompt-studio.poses")

ALLOWED_SYSTEMS = {"全身单人・标准系", "上半身单人"}
BLOCKED_NAME_PARTS = (
    "性感", "缠绵", "后入", "後入", "挤胸", "强调臀", "裸露", "内衣",
    "挑逗", "煽情", "诱惑", "誘惑", "痴", "淫", "SM", "拘束",
)
CATEGORY_PROMPTS = [
    ("侧坐", "side-seated pose"),
    ("前倾", "leaning forward"),
    ("后仰", "leaning back with an arched spine"),
    ("扭身", "twisted torso pose"),
    ("站姿", "standing pose"),
    ("坐姿", "seated pose"),
    ("跪", "kneeling pose"),
    ("蹲", "crouching pose"),
    ("趴", "reclining pose"),
    ("卧", "reclining pose"),
    ("上半身", "upper-body portrait pose"),
    ("托腮", "resting chin on hand pose"),
    ("扶脸", "resting chin on hand pose"),
]


def prompt_for(category: str, name: str) -> str:
    text = f"{category} {name}"
    for keyword, phrase in CATEGORY_PROMPTS:
        if keyword in text:
            return phrase
    return "natural relaxed pose"


class PoseIndex:
    def __init__(self, reference_dir: str | Path, enabled: bool = True):
        self.dir = Path(reference_dir)
        self.enabled = enabled
        self.items: list[dict] = []
        self.categories: list[str] = []
        self.allowed_paths: set[str] = set()
        self.available = False
        self.reason = ""
        self.load()

    def load(self) -> None:
        if not self.enabled:
            self.reason = "POSE_REFERENCE_ENABLED=false，姿势参考库已禁用"
            return
        source = self.dir / "data" / "pose-zh.json"
        if not source.is_file():
            self.reason = f"未挂载姿势库：把 ai-pose-gallery-zh 挂到 {self.dir}（只读），使其包含 data/pose-zh.json"
            return
        try:
            rows = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.reason = f"姿势库数据读取失败: {exc}"
            return
        if not isinstance(rows, list):
            self.reason = "pose-zh.json 结构不是数组"
            return

        grouped: dict[tuple[str, str], dict] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            system = str(row.get("体系") or "")
            if system not in ALLOWED_SYSTEMS:
                continue
            name = str(row.get("姿势名称") or "").strip()
            if not name or any(part in name for part in BLOCKED_NAME_PARTS):
                continue
            category = str(row.get("分类") or "未分类").strip()
            angle = str(row.get("角度") or "").strip()
            key = (system, str(row.get("姿势编号") or name))
            thumb = str(row.get("缩略图") or "")
            full = str(row.get("原图") or "")
            if key not in grouped:
                grouped[key] = {
                    "id": str(row.get("id") or ""),
                    "name": name,
                    "system": system,
                    "category": category,
                    "angles": [],
                    "thumb": thumb,
                    "full": full,
                    "prompt": prompt_for(category, name),
                }
            item = grouped[key]
            if angle and angle not in item["angles"]:
                item["angles"].append(angle)
            if not item["thumb"] and thumb:
                item["thumb"] = thumb
            if not item["full"] and full:
                item["full"] = full

        self.items = sorted(grouped.values(), key=lambda x: (x["category"], x["name"]))
        self.categories = sorted({item["category"] for item in self.items})
        for item in self.items:
            for path in (item["thumb"], item["full"]):
                if path and not path.startswith("/") and ".." not in Path(path).parts:
                    self.allowed_paths.add(path)
        self.available = bool(self.items)
        if not self.items:
            self.reason = "姿势库中没有可用的 SFW 记录"
        else:
            LOG.info("姿势参考库已载入：%s 个 SFW 姿势 / %s 个分类 / %s 个图片路径",
                     len(self.items), len(self.categories), len(self.allowed_paths))

    def status(self) -> dict:
        return {
            "available": self.available,
            "enabled": self.enabled,
            "count": len(self.items),
            "categories": self.categories,
            "systems": sorted(ALLOWED_SYSTEMS),
            "reference_dir": str(self.dir),
            "reason": self.reason,
            "note": "仅索引 SFW 体系；镜像不含上游数据与图片，版权归原作者所有。",
        }

    def search(self, query: str = "", category: str = "", limit: int = 60) -> list[dict]:
        q = (query or "").strip().lower()
        result = []
        for item in self.items:
            if category and item["category"] != category:
                continue
            if q and q not in f"{item['name']} {item['category']} {item['id']}".lower():
                continue
            result.append(item)
            if len(result) >= max(1, min(limit, 200)):
                break
        return result

    def resolve_image(self, relative: str) -> Path | None:
        """只放行索引内登记的 SFW 图片路径，并做目录穿越防护。"""
        if relative not in self.allowed_paths:
            return None
        root = self.dir.resolve()
        target = (self.dir / relative).resolve()
        if root not in target.parents or not target.is_file():
            return None
        return target

    @staticmethod
    def content_type(path: Path) -> str:
        return mimetypes.guess_type(path.name)[0] or "application/octet-stream"
