"""生成 ComfyUI 可直接导入 / 通过 /prompt 提交的 API 工作流 JSON。"""
from __future__ import annotations

import json
import random

try:  # 以 app.workflow 包内模块导入
    from .zimage_workflow import MODELS as ZIMAGE_MODELS, build_zimage_workflow
except ImportError:  # server.py 把 app/ 加进 sys.path 后以顶层模块导入
    from zimage_workflow import MODELS as ZIMAGE_MODELS, build_zimage_workflow

WORKFLOW_ENGINES = ("sd", "zimage-gguf", "zimage-int8")

ENGINE_DEFAULTS = {
    "sd": {"width": 896, "height": 1152, "steps": 28, "cfg": 6.5},
    "zimage-gguf": {"width": 1024, "height": 1024, "steps": 8, "cfg": 1.0},
    "zimage-int8": {"width": 1024, "height": 1024, "steps": 8, "cfg": 1.0},
}

ZIMAGE_NODE_MARKERS = {"UNETLoader", "UnetLoaderGGUF", "ModelSamplingAuraFlow"}


def engine_defaults(engine: str) -> dict:
    return dict(ENGINE_DEFAULTS.get((engine or "sd").strip().lower(), ENGINE_DEFAULTS["sd"]))


def is_zimage_workflow(workflow: dict) -> bool:
    """判断一份 API 工作流是否已是 Z-Image（避免推送时二次转换）。"""
    if not isinstance(workflow, dict):
        return False
    return any(
        isinstance(node, dict) and node.get("class_type") in ZIMAGE_NODE_MARKERS
        for node in workflow.values()
    )


def build_for_engine(
    engine: str, positive: str, negative: str, options: dict | None = None
) -> dict:
    """按 WORKFLOW_ENGINE 生成工作流；未知引擎回落到原 SD 模板（保持向后兼容）。"""
    name = (engine or "sd").strip().lower()
    if name not in WORKFLOW_ENGINES:
        name = "sd"
    if name.startswith("zimage"):
        variant = "int8" if name == "zimage-int8" else "gguf"
        return build_zimage_workflow(positive, negative, options, variant=variant)
    return build_workflow(positive, negative, options)


def build_workflow(positive: str, negative: str, options: dict | None = None) -> dict:
    opts = options or {}
    width = max(64, min(2048, int(opts.get("width", 896))))
    height = max(64, min(2048, int(opts.get("height", 1152))))
    steps = max(1, min(150, int(opts.get("steps", 28))))
    cfg = max(0.0, min(30.0, float(opts.get("cfg", 6.5))))
    seed = opts.get("seed")
    if seed in (None, "", -1):
        seed = random.randint(0, 2**31 - 1)
    seed = int(seed) % (2**31)
    positive = (positive or "").strip()
    negative = (negative or "").strip()

    # 标准 API prompt 格式：键为节点 ID，值为 class_type + inputs
    return {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": str(opts.get("ckpt_name") or "REPLACE_WITH_YOUR_CHECKPOINT.safetensors")},
            "_meta": {"title": "① 选择模型"},
        },
        "2": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": positive, "clip": ["1", 1]},
            "_meta": {"title": "正向提示词 · Prompt Studio"},
        },
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative, "clip": ["1", 1]},
            "_meta": {"title": "负向提示词 · Prompt Studio"},
        },
        "4": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
            "_meta": {"title": f"画布 {width}×{height}"},
        },
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed, "steps": steps, "cfg": cfg,
                "sampler_name": str(opts.get("sampler_name") or "euler"),
                "scheduler": str(opts.get("scheduler") or "normal"),
                "denoise": 1.0,
                "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0],
                "latent_image": ["4", 0],
            },
            "_meta": {"title": "采样器"},
        },
        "6": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["5", 0], "vae": ["1", 2]},
            "_meta": {"title": "VAE 解码"},
        },
        "7": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "PromptStudio", "images": ["6", 0]},
            "_meta": {"title": "保存图片"},
        },
    }


def workflow_to_text(workflow: dict) -> str:
    return json.dumps(workflow, ensure_ascii=False, indent=2)
