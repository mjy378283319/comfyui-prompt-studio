# ComfyUI 提示词工作台（Prompt Studio）

一个部署在 **Unraid / NAS** 上的 Docker 小工具：用**四层模块化模板**写高质感成年人像提示词，
从图片反推提示词，输出 ComfyUI 可直接粘贴的正/负向提示词、工作流 JSON，或直接推送到 ComfyUI 出图队列。

后端只用 **Python 标准库**，镜像约 60 MB、启动秒级、构建不需要 pip 联网；WD14 ONNX 反推是可选项。

---

## 一、它解决什么

写人像提示词最大的问题不是词不够，而是**每次凭感觉乱写、无法复现**。工作台把提示词固定成四层：

| 层 | 内容 | 作用 |
| --- | --- | --- |
| ① 角色固定层 | 年龄、发色、五官、基础身材轮廓 | 锁定「这是谁」，后续迭代永不动 |
| ② 服饰与遮蔽层 | 遮蔽关系、布料材质、层次与色彩 | 决定光怎么落在身上（亚麻吸光 / 真丝反光 / 羊绒散射） |
| ③ 动作与场景层 | 卧姿、跪姿、倚靠；卧室、浴缸、落地窗前 | 姿态与环境成对成立，并交代光源 |
| ④ 摄影与画质层 | 光影方向、镜头焦段、皮肤纹理 | 质感开关：不平光、不畸变、不磨皮成塑料人 |

完整心法见 `data/guide.md`，界面右上角「📖 编写心法」可直接阅读。

**功能清单**

- ✅ 四层中英双语词库（约 180 条），点选即生成；冲突词检测（同组多个光影/镜头会提示）
- ✅ Tag 串（CLIP / SDXL / Pony）与自然语言（T5 / Flux / SD3）双模式
- ✅ 权重语法切换：`(tag:1.2)` / `((tag))` / `{tag}` / `[tag]` / 不加权重；层权重 ①1.25 ②1.15 ③1.05 ④1.0
- ✅ 图生提示词：PNG 元数据（零依赖）→ WD14 ONNX 本地反推 → 外部视觉接口（按四层归位）
- ✅ 正/负向提示词、四层分段预览、冲突与合规警告
- ✅ 导出 `.txt`、ComfyUI 工作流 JSON、一键推送到 ComfyUI `/prompt`
- ✅ 预设保存/载入（持久化到挂载卷），内置 4 套成年人像模板
- ✅ 合规底线：词库与反推结果过滤未成年/露骨标签，默认负向词含排除项

---

## 二、Unraid 部署（推荐）

### 方式 A：docker compose（最省事）

把 `docker-compose.yml` 放到 `/mnt/user/appdata/comfyui-prompt-studio/`，然后：

```bash
cd /mnt/user/appdata/comfyui-prompt-studio
mkdir -p data models

# 1) 先把 COMFYUI_URL 改成 NAS 上 ComfyUI 的地址
#    http://host.docker.internal:8188  或  http://192.168.x.x:8188
#    ⚠️ 容器内 127.0.0.1 指向容器自己，绝对不能填 127.0.0.1

docker compose pull
docker compose up -d
docker compose logs -f
```

浏览器打开 `http://<NAS-IP>:8199`。

### 方式 B：Unraid WebUI「添加容器」

把 `unraid/comfyui-prompt-studio.xml` 复制到
`/boot/config/plugins/dockerMan/templates-user/`，在 Docker 页点「添加容器」→ 模板下拉选择
**ComfyUI Prompt Studio**，改一下 `COMFYUI_URL` 与宿主机目录即可应用。

### 方式 C：在 NAS 上从源码构建（不依赖 GHCR）

```bash
docker compose -f docker-compose.build.yml up -d --build
# 需要 WD14 本地反推时：
docker compose -f docker-compose.build.yml build --build-arg WITH_ONNX=1
```

### 端口与卷

| 项 | 容器 | 宿主机（Unraid） | 说明 |
| --- | --- | --- | --- |
| Web UI | 8199 | 8199 | 避开 ComfyUI 的 8188、拓竹耗材管家的 8971 |
| 数据 | `/app/data` | `/mnt/user/appdata/comfyui-prompt-studio/data` | 用户预设持久化；首次启动自动补齐内置词库/指南，不覆盖已有文件 |
| 模型 | `/app/models/wd14` | `.../models` | 可选，放 `model.onnx` + `selected_tags.csv` 后启用 WD14 |
| 属主 | PUID/PGID | 99 / 100 | Unraid 的 `nobody:users` |

### 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PORT` | `8199` | 容器内监听端口 |
| `DATA_DIR` | `/app/data` | 词库、指南、用户预设 |
| `MODEL_DIR` | `/app/models/wd14` | WD14 模型目录 |
| `COMFYUI_URL` | `http://127.0.0.1:8188` | 推送工作流的目标 ComfyUI；**容器部署必须改** |
| `WD14_THRESHOLD` | `0.35` | WD14 置信度阈值 |
| `REMOTE_VISION_URL` | 空 | OpenAI 兼容视觉接口 `/v1/chat/completions` |
| `REMOTE_VISION_KEY` | 空 | Bearer Token（可选） |
| `REMOTE_VISION_MODEL` | `gpt-4o-mini` | 视觉模型名 |
| `REMOTE_VISION_MODE` | `openai` | `openai`（四层 JSON）或 `raw`（自定义反推服务） |
| `REMOTE_VISION_TIMEOUT` | `45` | 超时秒数 |
| `PUID` / `PGID` | `99` / `100` | Unraid 卷属主 |

> 访问密钥一律走环境变量，不写进代码，也不写进镜像。

---

## 三、图生提示词：三条路径

1. **图片元数据（零依赖，最可靠）**：ComfyUI / A1111 / NovelAI 生成的 PNG 会把提示词写进
   `tEXt` / `iTXt` / `zTXt` 块。工作台能解析 `parameters` 的 `Negative prompt:`、`Comment` 的
   NovelAI JSON，以及 ComfyUI `prompt` 工作流里 `CLIPTextEncode` 节点的正/负向文本；JPEG 走 EXIF / XMP。
2. **WD14 ONNX 本地反推（可选）**：见 `models/README.md`。输出带置信度条，勾选后合并到四层。
3. **外部视觉接口（可选）**：配 `REMOTE_VISION_URL`，模型按四层输出 JSON，点「按四层归位」自动回填。

反推结果只当草稿：删冲突词、补 ④ 层的皮肤纹理与镜头，再出图。

---

## 四、和 ComfyUI 对接

- **复制粘贴**：右侧「正向提示词 / 负向提示词」，直接贴进两个 `CLIPTextEncode`。
- **导出工作流**：点「导出工作流 JSON」，导入 ComfyUI 后把 ① 号 `CheckpointLoaderSimple`
  的 `ckpt_name` 换成你的模型即可跑。可设尺寸、步数、CFG。
- **直接推送**：点「推送到 ComfyUI」，服务端 POST 到 `COMFYUI_URL/prompt`，返回 `prompt_id`。
  （Flux / SD3 若工作流节点不同，建议用前两种方式。）

---

## 五、镜像发布链路（复用拓竹耗材管家的 git 密钥）

`.github/workflows/docker.yml` 与 `bambu-spool` 完全同一套：仓库内置的
`secrets.GITHUB_TOKEN`（`permissions: packages: write`）在 push 到 `main` 或打 `v*` 标签时
自动跑自测 → 构建 → 推到 `ghcr.io/${{ github.repository }}`。
**NAS 上不需要任何 git 密钥**，`docker compose pull` 即可取到镜像。

> 若仓库是 private，GHCR 包默认也是 private，Unraid 首次拉取需先
> `docker login ghcr.io -u <用户名> -p <PAT>`；把仓库改为 public，镜像即可免登录拉取。

---

## 六、本地运行与验证（无需 Docker）

```bash
python -m app.server              # http://127.0.0.1:8199
python tests/smoke_test.py        # 零依赖端到端自测
node --check app/static/app.js    # 前端语法检查
```

自测覆盖：词库加载、四层合成（tag / 自然语言 / 权重 / 合规过滤）、工作流生成、
PNG 元数据反推（含 ComfyUI workflow 正/负向节点）、全部 HTTP 接口与错误分支。

---

## 七、已知限制与定位

- 工具定位于**成年人像摄影**提示词工程；词库与反推结果均过滤未成年与露骨标签，负向词默认含合规排除项。
- WD14 推理依赖需 `--build-arg WITH_ONNX=1` 额外安装，默认镜像不含模型与推理库。
- 导出的工作流是 SD 系通用 `KSampler` 模板；Flux 等需换成对应节点，提示词本身可直接复用。
- ComfyUI 推送只负责提交 `/prompt`，不查询执行结果。
- 无用户体系与鉴权，建议只在内网 / 反代后面使用。

## 参考来源

`github.com/WolfChen1996/NovelAI-tag-generator` · `wolfchen.top/tag/doc.html` ·
`prompt.qpipi.com` · `tags.novelai.dev`（Danbooru 标签超市）· `magic-tag.netlify.app`（魔导绪论）
