"""词库加载、索引与合规过滤。仅使用标准库。"""
from __future__ import annotations

import json
import re
from pathlib import Path

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


class LexiconError(Exception):
    pass


class Lexicon:
    def __init__(self, data_dir: str | Path | None = None):
        self.data_dir = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
        self.data: dict = {}
        self._index: dict[str, dict] = {}
        self._blocked_re: re.Pattern | None = None
        self.load()

    def load(self) -> None:
        path = self.data_dir / "lexicon.json"
        if not path.exists():
            raise LexiconError(f"词库文件不存在: {path}")
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LexiconError(f"词库解析失败: {exc}") from exc

        self._index = {}
        for layer in self.data.get("layers", []):
            for group in layer.get("groups", []):
                for item in group.get("items", []):
                    if not isinstance(item, list) or len(item) < 3:
                        raise LexiconError(f"词条格式错误: {layer['id']}/{group['id']}/{item!r}")
                    en, zh, nl = item[0], item[1], item[2]
                    self._index[en] = {
                        "en": en, "zh": zh, "nl": nl,
                        "layer": layer["id"], "group": group["id"],
                        "layer_title": layer["title"],
                    }
        words = [re.escape(w) for w in self.data.get("blacklist", [])]
        self._blocked_re = re.compile(r"(?<!\w)(?:" + "|".join(words) + r")(?!\w)", re.I) if words else None

    @property
    def layers(self) -> list[dict]:
        return self.data.get("layers", [])

    @property
    def models(self) -> list[dict]:
        return self.data.get("models", [])

    def lookup(self, en: str) -> dict | None:
        return self._index.get(en)

    def blocked_terms(self, text: str) -> list[str]:
        if not text or not self._blocked_re:
            return []
        return sorted({m.group(0).lower() for m in self._blocked_re.finditer(text)})

    def is_blocked(self, text: str) -> bool:
        return bool(self.blocked_terms(text))

    def sanitize_tags(self, tags: list[str]) -> tuple[list[str], list[str]]:
        kept, removed = [], []
        for tag in tags:
            tag = (tag or "").strip()
            if not tag:
                continue
            if self.is_blocked(tag):
                removed.append(tag)
            else:
                kept.append(tag)
        return kept, removed

    def guide(self) -> str:
        path = self.data_dir / "guide.md"
        if not path.exists():
            return "# 提示词指南\n\n指南文件缺失。"
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            return f"# 提示词指南\n\n读取失败: {exc}"

    def default_weights(self) -> dict:
        return dict(self.data.get("layer_default_weights", {}))

    def negative_group(self, name: str) -> list[str]:
        return list(self.data.get("negative", {}).get(name, []))

    def quality_prefix(self) -> list[str]:
        return [row[0] for row in self.data.get("quality_prefix", [])]
