"""Z-Image-Turbo 的 ComfyUI API 工作流生成器。

两套 B 房环境的节点名不同，用 variant 区分：
  int8 -> 8189 / cu130  / ComfyUI 0.45.0（主用，9.7 s/张）
         节点与参数按官方 blueprint「Text to Image (Z-Image-Turbo).json」核对
  gguf -> 8188 / 秋叶包 / ComfyUI 0.44.0
         模板来自已实测的 zimage_api_call_example.json

两套共用官方参数：8 步 / CFG 1.0 / res_multistep / simple /
ModelSamplingAuraFlow shift 3 / denoise 1.0 / 1024x1024（16 的倍数）。
CFG=1 时负向提示词不生效（蒸馏模型），这里保留空 CLIPTextEncode 占位，
与官方模板的 ConditioningZeroOut 行为等价。
"""
from __future__ import annotations

import json
import random

MODELS = {
    "gguf": {
        "unet_loader": "UnetLoaderGGUF",
        "unet_inputs": {"unet_name": "z_image_turbo-Q4_K_M.gguf"},
        "clip_loader": "CLIPLoaderGGUF",
        "clip_inputs": {"clip_name": "Qwen3-4B-Q4_K_M.gguf", "type": "lumina2"},
        "vae_name": "ae.safetensors",
    },
    "int8": {
        "unet_loader": "UNETLoader",
        "unet_inputs": {
            "unet_name": "z_image_turbo_int8_convrot.safetensors",
            "weight_dtype": "default",
        },
        "clip_loader": "CLIPLoader",
        "clip_inputs": {
            "clip_name": "qwen_3_4b_fp8_mixed.safetensors",
            "type": "lumina2",
            "device": "default",
        },
        "vae_name": "ae.safetensors",
    },
}


def build_zimage_workflow(
    positive: str,
    negative: str = "",
    options: dict | None = None,
    variant: str = "int8",
) -> dict:
    opts = options or {}
    if variant not in MODELS:
        raise ValueError(f"variant 必须是 {sorted(MODELS)} 之一")
    m = MODELS[variant]

    width = max(64, min(2048, int(opts.get("width", 1024))))
    height = max(64, min(2048, int(opts.get("height", 1024))))
    # Z-Image 要求 16 的倍数，否则出现重复纹理伪影
    width -= width % 16
    height -= height % 16
    steps = max(1, min(20, int(opts.get("steps", 8))))
    seed = opts.get("seed")
    if seed in (None, "", -1):
        seed = random.randint(0, 2**31 - 1)
    prefix = str(opts.get("prefix") or "PromptStudio")

    # 允许通过 options 覆盖模型文件名（换量化版本时不用改代码）
    unet_inputs = dict(m["unet_inputs"])
    clip_inputs = dict(m["clip_inputs"])
    if opts.get("unet_name"):
        unet_inputs["unet_name"] = str(opts["unet_name"])
    if opts.get("clip_name"):
        clip_inputs["clip_name"] = str(opts["clip_name"])
    if opts.get("vae_name"):
        m = dict(m, vae_name=str(opts["vae_name"]))

    return {
        "1": {"class_type": m["unet_loader"], "inputs": unet_inputs},
        "2": {"class_type": m["clip_loader"], "inputs": clip_inputs},
        "3": {"class_type": "CLIPTextEncode",
              "inputs": {"text": (positive or "").strip(), "clip": ["2", 0]}},
        "4": {"class_type": "CLIPTextEncode",
              "inputs": {"text": (negative or "").strip(), "clip": ["2", 0]}},
        "5": {"class_type": "EmptySD3LatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
        "10": {"class_type": "ModelSamplingAuraFlow",
               "inputs": {"model": ["1", 0], "shift": 3.0}},
        "6": {"class_type": "KSampler", "inputs": {
            "model": ["10", 0], "seed": int(seed) % (2**31),
            "steps": steps, "cfg": 1.0,
            "sampler_name": "res_multistep", "scheduler": "simple",
            "denoise": 1.0,
            "positive": ["3", 0], "negative": ["4", 0],
            "latent_image": ["5", 0],
        }},
        "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["8", 0]}},
        "8": {"class_type": "VAELoader", "inputs": {"vae_name": m["vae_name"]}},
        "9": {"class_type": "SaveImage",
              "inputs": {"images": ["7", 0], "filename_prefix": prefix}},
    }


if __name__ == "__main__":
    for v in MODELS:
        wf = build_zimage_workflow("a cinematic portrait, soft window light, 85mm", variant=v)
        print(f"# variant={v}")
        print(json.dumps({"prompt": wf, "client_id": "prompt-studio"}, ensure_ascii=False, indent=2))
