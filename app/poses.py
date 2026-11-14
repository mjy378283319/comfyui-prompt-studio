"""姿势参考库：可选挂载 anhao/ai-pose-gallery-zh 仓库的只读卷。

镜像与仓库内不含任何上游数据与图片，全部由用户自行 clone 后以只读卷挂载。

默认 SFW：只索引「全身单人・标准系 / 上半身单人」。
成人模式（POSE_ADULT_MODE=true + 登录 + 内网直连 + 显式确认）额外开放
「全身单人・性感系」的单人人体动态参考；「双人缠绵」及露骨性行为、
BDSM 器具、调教、未成年或疑似未成年内容在任何模式下都不开放。

解锁状态只存在进程内存，容器重启自动回到 SFW。
"""
from __future__ import annotations

import json
import logging
import mimetypes
from pathlib import Path

LOG = logging.getLogger("prompt-studio.poses")

SFW_SYSTEMS = {"全身单人・标准系", "上半身单人"}
ADULT_SYSTEMS = {"全身单人・性感系"}
NEVER_SYSTEMS = {"双人缠绵"}

# 任何模式都不过滤掉的硬边界：未成年 / 露骨性行为 / 器具 / 调教 / 性化暴力
HARD_BLOCKED = (
    "loli", "shota", "lolicon", "shotacon", "child", "kid", "baby", "toddler",
    "teen", "未成年", "幼女", "幼男", "ロリ", "ショタ",
    "nsfw", "nude", "naked", "porn", "rape", "gore", "explicit",
    "性交", "交配", "插入", "性器", "全裸", "裸露", "露骨",
    "后入", "後入", "挤胸", "强调臀", "口塞", "项圈", "束缚", "拘束",
    "捆绑", "调教", "服从", "器具", "鞭打", "SM", "痴", "淫",
    "双人", "缠绵", "诱奸", "凌辱",
)
# SFW 模式额外屏蔽的成人向氛围词（成人模式下这些词不再拦截，但 HARD_BLOCKED 仍生效）
SFW_BLOCKED = ("性感", "挑逗", "煽情", "诱惑", "誘惑", "内衣", "睡衣", "泳装", "臀部")

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
    def __init__(self, reference_dir: str | Path, enabled: bool = True, adult_mode: bool = False):
        self.dir = Path(reference_dir)
        self.enabled = enabled
        self.adult_mode = adult_mode
        self.adult_unlocked = False
        self.items: list[dict] = []
        self.categories: list[str] = []
        self.sfw_paths: set[str] = set()
        self.adult_paths: set[str] = set()
        self.available = False
        self.reason = ""
        self.load()

    @property
    def unlocked(self) -> bool:
        return self.adult_mode and self.adult_unlocked

    @property
    def active_systems(self) -> set[str]:
        return SFW_SYSTEMS | (ADULT_SYSTEMS if self.unlocked else set())

    def load(self) -> None:
        self.items = []
        self.categories = []
        self.sfw_paths = set()
        self.adult_paths = set()
        self.available = False
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

        systems = self.active_systems
        blocked = HARD_BLOCKED if self.unlocked else (HARD_BLOCKED + SFW_BLOCKED)
        grouped: dict[tuple[str, str], dict] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            system = str(row.get("体系") or "")
            if system in NEVER_SYSTEMS or system not in systems:
                continue
            name = str(row.get("姿势名称") or "").strip()
            if not name or any(part.lower() in name.lower() for part in blocked):
                continue
            category = str(row.get("分类") or "未分类").strip()
            angle = str(row.get("角度") or "").strip()
            is_adult = system in ADULT_SYSTEMS
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
                    "adult": is_adult,
                }
            item = grouped[key]
            if angle and angle not in item["angles"]:
                item["angles"].append(angle)
            if not item["thumb"] and thumb:
                item["thumb"] = thumb
            if not item["full"] and full:
                item["full"] = full

        self.items = sorted(grouped.values(), key=lambda x: (x["adult"], x["category"], x["name"]))
        self.categories = sorted({item["category"] for item in self.items})
        for item in self.items:
            bucket = self.adult_paths if item["adult"] else self.sfw_paths
            for path in (item["thumb"], item["full"]):
                if path and not path.startswith("/") and ".." not in Path(path).parts:
                    bucket.add(path)
        self.available = bool(self.items)
        self.reason = "" if self.items else "姿势库中没有可用记录"
        if self.items:
            LOG.info("姿势参考库已载入：%s 个姿势（成人 %s）/ %s 个分类 / 解锁=%s",
                     len(self.items), sum(1 for i in self.items if i["adult"]),
                     len(self.categories), self.unlocked)

    def unlock(self) -> None:
        if not self.enabled or not self.adult_mode:
            raise PermissionError("成人模式未启用：需在 compose 中设置 POSE_ADULT_MODE=true")
        self.adult_unlocked = True
        self.load()
        LOG.info("成人模式已解锁（仅限成年人 / 内网直连 / 本次容器运行）")

    def lock(self) -> None:
        self.adult_unlocked = False
        self.load()
        LOG.info("已回到 SFW 模式")

    def status(self) -> dict:
        return {
            "available": self.available,
            "enabled": self.enabled,
            "count": len(self.items),
            "adult_count": sum(1 for item in self.items if item["adult"]),
            "categories": self.categories,
            "systems": sorted(self.active_systems),
            "adult_mode": self.adult_mode,
            "adult_unlocked": self.unlocked,
            "reference_dir": str(self.dir),
            "reason": self.reason,
            "note": (
                "成人模式已解锁：仅限成年人、仅限内网直连，重启容器自动回到 SFW。"
                if self.unlocked else
                "默认 SFW；镜像不含上游数据与图片，版权归原作者所有。"
            ),
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
        """只放行索引内登记的路径：未解锁时仅 SFW，解锁后含成人体系；始终做目录穿越校验。"""
        allowed = self.sfw_paths | (self.adult_paths if self.unlocked else set())
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
