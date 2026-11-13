"""四层模块化提示词合成器：tag 模式 / 自然语言模式。"""
from __future__ import annotations

import math
from typing import Any

try:
    from .lexicon import Lexicon
except ImportError:  # 直接以脚本方式运行
    from lexicon import Lexicon

CONFLICT_GROUPS: list[tuple[set[str], str]] = [
    ({
        "soft window light from the left", "backlit rim light", "diffused overhead light",
        "warm side lighting", "Rembrandt lighting", "gentle fill light",
        "low-key chiaroscuro", "golden hour backlight",
        "soft backlight through sheer curtain", "low-key boudoir lighting",
    }, "光影方向"),
    ({
        "crisp linen", "washed cotton", "heavy satin", "sheer chiffon",
        "fine-knit cashmere", "ribbed wool", "matte silk", "delicate lace",
        "sheer layered fabric", "bias-cut silk", "unstructured linen layers",
    }, "布料材质"),
    ({
        "85mm f/1.4 portrait lens", "50mm f/1.8", "135mm f/2",
        "35mm environmental view", "medium format look",
    }, "镜头焦段"),
    ({"close-up portrait", "medium shot", "full-body shot"}, "构图"),
    ({"golden hour", "overcast morning", "blue hour", "soft rainy daylight", "midday shade"}, "时间"),
]


def _clamp(value: Any, low: float = 0.3, high: float = 2.0, default: float = 1.0) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(num):
        return default
    return max(low, min(high, num))


def _escape(tag: str) -> str:
    """避免用户输入里的括号破坏权重语法。"""
    return tag.replace("(", "\\(").replace(")", "\\)")


def apply_syntax(tag: str, weight: float, syntax: str) -> str:
    clean = _escape(tag.strip())
    if not clean:
        return ""
    if syntax == "plain" or abs(weight - 1.0) < 1e-6:
        return clean
    if syntax == "paren_weight":
        return f"({clean}:{weight:.2f})"
    if syntax == "paren":
        levels = max(1, min(4, round(math.log(weight) / math.log(1.10))))
        return "(" * levels + clean + ")" * levels
    if syntax == "brace":
        levels = max(1, min(6, round(math.log(weight) / math.log(1.05))))
        return "{" * levels + clean + "}" * levels
    if syntax == "bracket":
        return f"[{clean}]"
    return clean


def _split_extra(text: str) -> list[str]:
    return [part.strip() for part in (text or "").split(",") if part.strip()]


def _conflicts(selections: dict[str, list[str]]) -> list[str]:
    chosen = {tag for tags in selections.values() for tag in tags}
    warnings = []
    for group, label in CONFLICT_GROUPS:
        hits = sorted(chosen & group)
        if len(hits) > 1:
            warnings.append(f"{label}同时选了 {len(hits)} 项（{'、'.join(hits)}），互相竞争，建议只留 1 项")
    return warnings


def compose(payload: dict, lex: Lexicon) -> dict:
    """输入四层选择，输出 ComfyUI 可直接粘贴的正/负向提示词。"""
    if not isinstance(payload, dict):
        payload = {}

    raw_selections = payload.get("selections") or {}
    selections: dict[str, list[str]] = {}
    filtered: list[str] = []
    for layer in lex.layers:
        lid = layer["id"]
        values = raw_selections.get(lid) or []
        if isinstance(values, str):
            values = [v.strip() for v in values.split(",") if v.strip()]
        kept, removed = lex.sanitize_tags([str(v) for v in values])
        selections[lid] = kept
        filtered.extend(removed)

    mode = payload.get("mode") if payload.get("mode") in ("tag", "natural") else "tag"
    model = str(payload.get("model") or "generic")
    syntax = str(payload.get("syntax") or "paren_weight")
    weights = lex.default_weights()
    if isinstance(payload.get("layer_weights"), dict):
        for key, value in payload["layer_weights"].items():
            if key in weights:
                weights[key] = _clamp(value)

    use_quality = bool(payload.get("quality_prefix", mode == "tag"))
    use_weight = mode == "tag" and syntax != "plain"
    segments: list[dict] = []
    parts: list[str] = []

    if mode == "tag" and use_quality and model != "flux":
        prefix = ", ".join(lex.quality_prefix())
        parts.append(prefix)
        segments.append({"layer": "quality", "title": "画质前缀", "text": prefix, "count": len(lex.quality_prefix())})

    templates = lex.data.get("natural_templates", {})
    for layer in lex.layers:
        lid = layer["id"]
        chosen = selections.get(lid) or []
        if not chosen:
            continue
        weight = weights.get(lid, 1.0) if use_weight else 1.0
        if mode == "natural":
            phrases = []
            for en in chosen:
                item = lex.lookup(en)
                phrases.append(item["nl"] if item else en)
            body = ", ".join(phrases)
            template = templates.get(lid, "{body}.")
            text = template.replace("{body}", body)
        else:
            text = ", ".join(apply_syntax(en, weight, syntax) for en in chosen)
        parts.append(text)
        segments.append({
            "layer": lid, "title": layer["title"], "text": text,
            "count": len(chosen), "weight": round(weight, 2),
        })

    extra_positive = str(payload.get("extra_positive") or "").strip()
    if extra_positive:
        kept_extra, removed_extra = lex.sanitize_tags(
            _split_extra(extra_positive) if mode == "tag" else [extra_positive]
        )
        filtered.extend(removed_extra)
        if kept_extra:
            extra_text = ", ".join(kept_extra) if mode == "tag" else kept_extra[0].rstrip(".") + "."
            parts.append(extra_text)
            segments.append({"layer": "extra", "title": "补充词", "text": extra_text, "count": len(kept_extra)})

    positive = "\n".join(parts) if mode == "natural" else ", ".join(parts)

    negative_enabled = payload.get("negative_enabled")
    if negative_enabled is None:
        negative_enabled = model not in ("flux",)
    negative_parts: list[str] = []
    if negative_enabled:
        groups = payload.get("negative_groups") or {"common": True, "portrait": True, "safety": True}
        for name in ("common", "portrait", "safety"):
            if groups.get(name, name != "safety"):
                negative_parts.extend(lex.negative_group(name))
        extra_negative = str(payload.get("extra_negative") or "").strip()
        if extra_negative:
            negative_parts.extend(_split_extra(extra_negative))
    # 去重、保序
    seen: set[str] = set()
    negative_parts = [x for x in negative_parts if not (x.lower() in seen or seen.add(x.lower()))]
    negative = ", ".join(negative_parts)

    tag_count = sum(len(v) for v in selections.values())
    warnings = _conflicts(selections)
    if filtered:
        warnings.append(f"已按合规规则过滤 {len(set(filtered))} 个词条：{'、'.join(sorted(set(filtered))[:6])}")
    if mode == "tag" and tag_count > 28:
        warnings.append(f"当前 {tag_count} 个词条偏多，CLIP 会互相稀释；建议每层控制在 2–5 项")
    if mode == "natural" and syntax != "plain":
        warnings.append("自然语言模式下权重语法不生效，已自动忽略")

    return {
        "positive": positive,
        "negative": negative,
        "segments": segments,
        "mode": mode,
        "model": model,
        "syntax": syntax,
        "stats": {
            "tag_count": tag_count,
            "segment_count": len(segments),
            "weighted": use_weight,
            "negative_count": len(negative_parts),
            "filtered": sorted(set(filtered)),
        },
        "warnings": warnings,
    }
