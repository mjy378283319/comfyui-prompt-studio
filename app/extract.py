"""图生提示词：PNG/JPEG 元数据、WD14 ONNX 本地反推、外部视觉接口。

零依赖路径：PNG tEXt/iTXt/zTXt + JPEG EXIF/XMP 元数据解析（ComfyUI / A1111 / NovelAI 生成图）。
增强路径：WD14 ONNX（可选依赖）、OpenAI 兼容视觉模型（可选配置）。
"""
from __future__ import annotations

import base64
import binascii
import io
import json
import re
import struct
import urllib.error
import urllib.request
import zlib
from pathlib import Path

try:
    from .lexicon import Lexicon
except ImportError:  # 直接以脚本方式运行
    from lexicon import Lexicon

PNG_SIG = b"\x89PNG\r\n\x1a\n"
MAX_TEXT = 4 * 1024 * 1024

VISION_SYSTEM_PROMPT = """你是成年人像摄影的提示词工程师。只描述成年人物，禁止输出任何未成年人或露骨性内容。
观察图片，把画面拆成四层，并严格输出一个 JSON 对象（不要 markdown 代码块）：
{"identity":["年龄/发色/五官/身材的英文短语"],"attire":["遮蔽状态与布料材质的英文短语"],"pose_scene":["姿态/环境/时间的英文短语"],"photo":["光影方向/镜头焦段/皮肤纹理的英文短语"],"positive":"整合后的英文正向提示词","negative":"英文负向提示词"}
每项 2-5 个短语，全部使用英文。"""

JSON_BLOCK = re.compile(r"\{[\s\S]*\}")


class EngineUnavailable(Exception):
    """引擎未启用或环境不满足，附带可执行的处理建议。"""


# --------------------------------------------------------------------------- 基础工具

def decode_data_url(value: str) -> tuple[bytes, str]:
    """接受 dataURL 或纯 base64，返回 (bytes, mime)。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("图片数据为空")
    raw = value.strip()
    mime = "image/png"
    if raw.startswith("data:"):
        head, _, body = raw.partition(",")
        mime = head[5:].split(";")[0] or mime
        raw = body
    try:
        return base64.b64decode(raw, validate=True), mime
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"图片 base64 解码失败: {exc}") from exc


def _utf8(raw: bytes) -> str:
    return raw.decode("utf-8", "replace").strip("\x00 ").strip()


def split_tags(text: str) -> list[str]:
    """把提示词串拆成词条，剥离权重语法但保留文本。"""
    if not text:
        return []
    out = []
    for chunk in re.split(r"[,\n]", text):
        chunk = chunk.strip()
        if not chunk or len(chunk) > 240:
            continue
        if chunk.lower().startswith(("negative prompt:", "steps:", "seed:", "sampler:")):
            continue
        cleaned = re.sub(r"^\(+|\)+$", "", chunk)
        cleaned = re.sub(r"^\{+|\}+$", "", cleaned)
        cleaned = re.sub(r"^\[+|\]+$", "", cleaned)
        cleaned = re.sub(r":\s*[0-9.]+$", "", cleaned).strip()
        if cleaned:
            out.append(cleaned)
    return out


# --------------------------------------------------------------------------- PNG

def _png_text_chunks(data: bytes) -> dict[str, str]:
    if not data.startswith(PNG_SIG):
        return {}
    pos, out = 8, {}
    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        ctype = data[pos + 4:pos + 8]
        if length > MAX_TEXT or pos + 12 + length > len(data):
            break
        payload = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        try:
            if ctype == b"tEXt":
                key, _, val = payload.partition(b"\x00")
                out[_utf8(key)] = _utf8(val)
            elif ctype == b"zTXt":
                key, _, rest = payload.partition(b"\x00")
                if len(rest) > 1:
                    out[_utf8(key)] = _utf8(zlib.decompress(rest[1:]))
            elif ctype == b"iTXt":
                key, _, rest = payload.partition(b"\x00")
                if len(rest) < 2:
                    continue
                compressed, rest = rest[0], rest[2:]
                _, _, rest = rest.partition(b"\x00")
                _, _, text = rest.partition(b"\x00")
                if compressed:
                    text = zlib.decompress(text)
                out[_utf8(key)] = _utf8(text)
        except (zlib.error, UnicodeError, ValueError):
            continue
        if ctype == b"IEND":
            break
    return out


def _from_parameters(text: str) -> tuple[str, str]:
    """A1111 / ComfyUI 常见 'parameters' 文本格式。"""
    positive, sep, rest = text.partition("\nNegative prompt:")
    if not sep:
        return text.strip(), ""
    negative, _, _settings = rest.partition("\n")
    return positive.strip(), negative.strip()


def _comfy_workflow_prompt(raw: str) -> tuple[str, str, int]:
    """从 ComfyUI 的 prompt(API JSON) 里抠出正负向 CLIPTextEncode。"""
    try:
        nodes = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return "", "", 0
    if not isinstance(nodes, dict):
        return "", "", 0
    texts = {
        nid: str(node.get("inputs", {}).get("text", ""))
        for nid, node in nodes.items()
        if isinstance(node, dict) and node.get("class_type") == "CLIPTextEncode" and node.get("inputs", {}).get("text")
    }
    pos_ids, neg_ids = [], []
    for node in nodes.values():
        if not isinstance(node, dict) or node.get("class_type") not in ("KSampler", "KSamplerAdvanced"):
            continue
        inputs = node.get("inputs", {})
        for key, bucket in (("positive", pos_ids), ("negative", neg_ids)):
            link = inputs.get(key)
            if isinstance(link, list) and link:
                nid = str(link[0])
                if nid in texts and nid not in bucket:
                    bucket.append(nid)
    positive = "\n".join(texts[n] for n in pos_ids)
    negative = "\n".join(texts[n] for n in neg_ids)
    if not positive and not negative:
        # 没有 KSampler（纯文本节点子图）时，按节点顺序启发式取前两个
        ordered = list(texts.values())
        positive = ordered[0] if ordered else ""
        negative = ordered[1] if len(ordered) > 1 else ""
    return positive.strip(), negative.strip(), len(texts)


def _png_metadata(data: bytes) -> dict:
    chunks = _png_text_chunks(data)
    positive, negative = "", ""
    source = ""
    node_count = 0
    for key in ("parameters", "Description"):
        if chunks.get(key):
            positive, negative = _from_parameters(chunks[key])
            if positive:
                source = f"png:{key}"
                break
    if not positive and chunks.get("Comment"):
        comment = chunks["Comment"]
        if comment.lstrip().startswith("{"):
            try:
                obj = json.loads(comment)
                positive = str(obj.get("prompt") or obj.get("positive") or "")
                negative = str(obj.get("uc") or obj.get("negative_prompt") or obj.get("negative") or "")
                source = "png:Comment(json)"
            except json.JSONDecodeError:
                positive = comment
                source = "png:Comment"
        else:
            positive = comment
            source = "png:Comment"
    if chunks.get("prompt"):
        p, n, node_count = _comfy_workflow_prompt(chunks["prompt"])
        if p or n:
            positive, negative, source = positive or p, negative or n, "png:prompt(ComfyUI workflow)"
    if not positive and chunks.get("workflow"):
        try:
            ui = json.loads(chunks["workflow"])
            for node in ui.get("nodes", []):
                if node.get("type") == "CLIPTextEncode":
                    widgets = node.get("widgets_values") or []
                    if widgets and isinstance(widgets[0], str) and len(widgets[0]) > 12:
                        positive = positive or widgets[0]
                        source = "png:workflow(UI)"
        except (json.JSONDecodeError, AttributeError):
            pass
    return {
        "positive": positive, "negative": negative, "source": source,
        "chunks": sorted(chunks.keys()),
        "workflow_nodes": node_count,
        "raw": {k: v[:2000] for k, v in chunks.items() if k in ("parameters", "Comment", "Description")},
    }


# --------------------------------------------------------------------------- JPEG

EXIF_TEXT_TAGS = {
    0x010E: "ImageDescription",
    0x0131: "Software",
    0x9286: "UserComment",
    0x9C9B: "XPTitle",
    0x9C9C: "XPComment",
    0x9C9D: "XPAuthor",
    0x9C9E: "XPKeywords",
    0x9C9F: "XPSubject",
}
TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}


def _exif_strings(tiff: bytes) -> dict[str, str]:
    if len(tiff) < 8 or tiff[:2] not in (b"II", b"MM"):
        return {}
    endian = "<" if tiff[:2] == b"II" else ">"
    try:
        ifd_offset = struct.unpack_from(endian + "I", tiff, 4)[0]
        if ifd_offset + 2 > len(tiff):
            return {}
        count = struct.unpack_from(endian + "H", tiff, ifd_offset)[0]
    except struct.error:
        return {}
    found: dict[str, str] = {}
    for i in range(min(count, 512)):
        entry = ifd_offset + 2 + i * 12
        if entry + 12 > len(tiff):
            break
        try:
            tag, typ, num = struct.unpack_from(endian + "HHI", tiff, entry)
            size = TYPE_SIZE.get(typ, 1) * num
            if size <= 0 or size > MAX_TEXT:
                continue
            raw = tiff[entry + 8:entry + 8 + size] if size <= 4 else None
            if raw is None:
                offset = struct.unpack_from(endian + "I", tiff, entry + 8)[0]
                if offset + size > len(tiff):
                    continue
                raw = tiff[offset:offset + size]
            if tag not in EXIF_TEXT_TAGS:
                continue
            if typ == 2:
                text = _utf8(raw)
                for prefix in (b"ASCII\x00\x00\x00", b"UNICODE\x00", b"JIS\x00\x00\x00\x00\x00"):
                    if raw.startswith(prefix):
                        text = _utf8(raw[len(prefix):])
                        break
            elif tag in (0x9C9B, 0x9C9C, 0x9C9D, 0x9C9E, 0x9C9F):
                text = raw.decode("utf-16-le", "replace").strip("\x00 ")
            else:
                text = _utf8(raw)
            if text:
                found[EXIF_TEXT_TAGS[tag]] = text
        except (struct.error, UnicodeError):
            continue
    return found


def _jpeg_metadata(data: bytes) -> dict:
    if not data.startswith(b"\xff\xd8"):
        return {}
    pos, fields, xmp = 2, {}, ""
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            break
        marker = data[pos + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        seglen = int.from_bytes(data[pos + 2:pos + 4], "big")
        if seglen < 2 or pos + 2 + seglen > len(data):
            break
        seg = data[pos + 4:pos + 2 + seglen]
        pos += 2 + seglen
        if marker == 0xE1 and seg.startswith(b"Exif\x00\x00"):
            fields.update(_exif_strings(seg[6:]))
        elif marker == 0xE1 and b"ns.adobe.com/xap" in seg[:64]:
            xmp = seg.decode("utf-8", "replace")
        elif marker == 0xDA:
            break

    positive, negative, source = "", "", ""
    for key in ("ImageDescription", "UserComment", "XPComment", "XPSubject", "XPTitle", "XPKeywords"):
        value = fields.get(key, "")
        if value and ("Negative prompt:" in value or "," in value or "masterpiece" in value.lower()):
            positive, negative = _from_parameters(value)
            source = f"exif:{key}"
            break
    if not positive and xmp:
        match = re.search(r"<dc:description[^>]*>([\s\S]*?)</dc:description>", xmp)
        if match:
            positive = re.sub(r"<[^>]+>", "", match.group(1)).strip()
            source = "xmp:dc:description"
    return {"positive": positive, "negative": negative, "source": source, "chunks": sorted(fields), "workflow_nodes": 0, "raw": {k: v[:2000] for k, v in fields.items()}}


def extract_metadata(data: bytes, mime: str = "") -> dict:
    if data.startswith(PNG_SIG):
        fmt, result = "png", _png_metadata(data)
    elif data.startswith(b"\xff\xd8"):
        fmt, result = "jpeg", _jpeg_metadata(data)
    else:
        fmt, result = "unknown", {"positive": "", "negative": "", "source": "", "chunks": [], "workflow_nodes": 0, "raw": {}}
    return {**result, "format": fmt, "mime": mime}


# --------------------------------------------------------------------------- WD14 ONNX

def _load_tag_names(csv_path: Path) -> list[tuple[str, int]]:
    import csv
    rows: list[tuple[str, int]] = []
    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, [])
        for row in reader:
            if len(row) >= 3:
                try:
                    category = int(row[2])
                except ValueError:
                    category = 0
                rows.append((row[1].replace("_", " "), category))
    if not rows:
        raise EngineUnavailable(f"标签表为空或格式不符（需要 tag_id,name,category 三列）: {csv_path}")
    return rows


def extract_wd14(data: bytes, model_dir: str | Path, threshold: float = 0.35) -> dict:
    try:
        import numpy as np
        import onnxruntime as ort
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise EngineUnavailable(
            "WD14 本地反推需要可选依赖 onnxruntime / numpy / Pillow。"
            "请用 `docker build --build-arg WITH_ONNX=1` 重建镜像，或改用「外部视觉接口」引擎。"
        ) from exc

    model_dir = Path(model_dir)
    model_path = model_dir / "model.onnx"
    csv_path = model_dir / "selected_tags.csv"
    missing = [str(p) for p in (model_path, csv_path) if not p.exists()]
    if missing:
        raise EngineUnavailable("缺少模型文件：" + "、".join(missing) + "。请把 WD14 ONNX 模型与 selected_tags.csv 放到 models/wd14/ 后重启容器。")

    tag_rows = _load_tag_names(csv_path)
    try:
        session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        input_name = session.get_inputs()[0].name
        output_name = session.get_outputs()[0].name
        size = 448
        if isinstance(session.get_inputs()[0].shape, list) and len(session.get_inputs()[0].shape) >= 3:
            dim = session.get_inputs()[0].shape[2]
            if isinstance(dim, int) and 64 <= dim <= 1024:
                size = dim
        image = Image.open(io.BytesIO(data)).convert("RGB")
        image = ImageOps.pad(image, (size, size), method=Image.Resampling.BICUBIC, color=(255, 255, 255))
        arr = np.asarray(image, dtype=np.float32)[:, :, ::-1][None, ...]  # RGB -> BGR, NHWC
        logits = session.run([output_name], {input_name: arr})[0][0]
        probs = 1.0 / (1.0 + np.exp(-logits.astype(np.float32)))
    except Exception as exc:  # noqa: BLE001 - 模型/图片异常统一转为可读提示
        raise EngineUnavailable(f"WD14 推理失败: {exc}") from exc

    tags = []
    for idx, (name, category) in enumerate(tag_rows):
        if idx >= len(probs):
            break
        score = float(probs[idx])
        if score < threshold or category == 2:  # 跳过 rating 分类
            continue
        tags.append({"tag": name, "score": round(score, 4), "category": "character" if category == 1 else "general", "source": "wd14"})
    tags.sort(key=lambda x: -x["score"])
    return {"positive": "", "negative": "", "tags": tags[:120], "meta": {"model": str(model_path), "size": size, "threshold": threshold, "total": len(tag_rows)}}


# --------------------------------------------------------------------------- 外部视觉接口

def extract_remote(data: bytes, cfg: dict, mime: str = "image/jpeg") -> dict:
    url = str(cfg.get("url") or "").strip()
    if not url:
        raise EngineUnavailable("未配置外部视觉接口。设置 REMOTE_VISION_URL（OpenAI 兼容 /v1/chat/completions）后重启容器。")
    mode = cfg.get("mode", "openai")
    timeout = max(3, min(120, int(cfg.get("timeout", 45))))
    b64 = base64.b64encode(data).decode("ascii")
    if mode == "raw":
        body = {"image": b64, "mime": mime}
    else:
        body = {
            "model": cfg.get("model") or "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": VISION_SYSTEM_PROMPT},
                {"role": "user", "content": [
                    {"type": "text", "text": "请按四层结构分析这张图片并输出 JSON。"},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ]},
            ],
            "temperature": 0.4,
            "max_tokens": 900,
        }
    headers = {"Content-Type": "application/json"}
    key = cfg.get("key")
    if key:
        headers["Authorization"] = f"Bearer {key}"

    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise EngineUnavailable(f"外部视觉接口调用失败: {exc}") from exc

    if mode == "raw":
        return {
            "positive": str(payload.get("positive") or ""),
            "negative": str(payload.get("negative") or ""),
            "tags": payload.get("tags") or [],
            "meta": {"engine": "remote:raw"},
        }

    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise EngineUnavailable(f"外部视觉接口返回结构异常: {str(payload)[:200]}") from exc
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    match = JSON_BLOCK.search(content)
    parsed = {}
    if match:
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            parsed = {}
    return {
        "positive": str(parsed.get("positive") or content).strip(),
        "negative": str(parsed.get("negative") or "").strip(),
        "layers": {k: parsed.get(k) or [] for k in ("identity", "attire", "pose_scene", "photo")},
        "tags": [],
        "meta": {"engine": "remote:openai", "model": cfg.get("model") or "gpt-4o-mini", "parsed_json": bool(parsed)},
    }


# --------------------------------------------------------------------------- 统一入口

def extract(data: bytes, mime: str, engine: str, cfg: dict, lex: Lexicon, filename: str = "") -> dict:
    if len(data) > 25 * 1024 * 1024:
        raise ValueError("图片超过 25 MB 上限")
    engine = (engine or "auto").lower()
    warnings: list[str] = []

    if engine in ("auto", "metadata"):
        meta = extract_metadata(data, mime)
        if meta.get("positive") or engine == "metadata":
            tags = [{"tag": t, "score": 1.0, "source": meta.get("source") or "metadata"} for t in split_tags(meta["positive"])]
            kept, removed = lex.sanitize_tags([t["tag"] for t in tags])
            tags = [t for t in tags if t["tag"] in set(kept)]
            if removed:
                warnings.append(f"已从反推结果中过滤 {len(set(removed))} 个不合规标签")
            return {
                "engine": "metadata", "filename": filename, "positive": meta["positive"],
                "negative": meta["negative"], "tags": tags[:150],
                "meta": {k: meta.get(k) for k in ("format", "mime", "source", "chunks", "workflow_nodes")},
                "raw": meta.get("raw", {}), "warnings": warnings,
                "hint": "" if meta.get("positive") else "该图片没有内嵌提示词元数据（可能是截图或经过二次压缩）。可改用 WD14 或外部视觉接口。",
            }
        if engine == "metadata":
            return {"engine": "metadata", "positive": "", "negative": "", "tags": [], "meta": {}, "warnings": warnings, "hint": "未找到元数据。"}

    if engine == "wd14":
        result = extract_wd14(data, cfg["model_dir"], float(cfg.get("threshold", 0.35)))
        kept, removed = lex.sanitize_tags([t["tag"] for t in result.get("tags", [])])
        allowed = set(kept)
        result["tags"] = [t for t in result["tags"] if t["tag"] in allowed]
        result["positive"] = ", ".join(t["tag"] for t in result["tags"])
        result.setdefault("meta", {})["filtered"] = len(set(removed))
        result.setdefault("warnings", []).extend(warnings)
        result["engine"] = "wd14"
        return result

    if engine in ("remote", "vision"):
        result = extract_remote(data, cfg.get("remote", {}), mime)
        result["engine"] = "remote"
        result.setdefault("warnings", []).extend(warnings)
        if result.get("positive"):
            kept, removed = lex.sanitize_tags(split_tags(result["positive"]))
            if removed:
                result["warnings"].append(f"已过滤 {len(set(removed))} 个不合规标签，请人工复核输出")
        return result

    if engine == "auto":
        # auto 依次尝试：元数据 -> WD14（若模型就绪）-> 外部接口
        if (Path(cfg["model_dir"]) / "model.onnx").exists():
            return extract(data, mime, "wd14", cfg, lex, filename)
        if cfg.get("remote", {}).get("url"):
            return extract(data, mime, "remote", cfg, lex, filename)
        return extract(data, mime, "metadata", cfg, lex, filename)

    raise ValueError(f"未知引擎: {engine}（可选 auto / metadata / wd14 / remote）")
