# WD14 模型目录（可选）

默认镜像**不含模型**，因为核心的「从图片提取提示词」零依赖路径已经可用：
ComfyUI / A1111 / NovelAI 生成的 PNG 会把正向、负向提示词直接写在图片元数据里，工作台能原样读出来。

如果你想对**任意图片**（截图、照片、二次压缩图）也做 tag 反推，放两个文件到这里并启用推理依赖：

```
models/
└── wd14/
    ├── model.onnx            # WD14 ONNX 模型
    └── selected_tags.csv     # 标签表，列：tag_id,name,category,count
```

## 1. 模型来源

任选一个 WD14 / Danbooru tagger 的 ONNX 版本（例如 `SmilingWolf/wd-v1-4-vit-tagger-v2`、
`wd-swinv2-tagger-v3`、`wd-eva02-large-tagger-v3`），需要 `model.onnx` 与 `selected_tags.csv` 两个文件。
国内网络可用 `https://hf-mirror.com` 镜像下载，下载后放到上面的目录即可。

> 注意：不同版本的输入尺寸不同（448 / 512 / 640）。工作台会从 ONNX 输入 shape 自动读取，
> 并用白底等比填充后转 BGR 送进模型，无需手工改代码。

## 2. 启用推理依赖

基础镜像刻意保持零 pip 依赖。用可选参数重建：

```bash
docker compose -f docker-compose.build.yml build --build-arg WITH_ONNX=1
docker compose -f docker-compose.build.yml up -d
```

或单独构建：

```bash
docker build --build-arg WITH_ONNX=1 -t comfyui-prompt-studio:onnx .
```

`WITH_ONNX=1` 会安装 `onnxruntime` / `numpy` / `Pillow`（走 `PIP_INDEX_URL`，默认清华镜像）。

## 3. 验证

```bash
curl http://<NAS-IP>:8199/api/health | python -m json.tool
```

返回里 `engines.wd14.available` 应为 `true`。前端选「WD14 ONNX 本地反推」上传图片，
调整阈值（默认 0.35）后勾选词条，再合并回四层模板。

## 4. 不装模型也能反推的替代方案

在 compose 里配置 `REMOTE_VISION_URL`（OpenAI 兼容的 `/v1/chat/completions` 视觉接口），
引擎选「外部视觉接口」，模型会按四层结构返回自然语言提示词，可一键「按四层归位」。
