# ComfyUI 提示词工作台（Prompt Studio）

一个部署在 **Unraid / NAS** 上的 Docker 小工具：用**四层模块化模板**写高质感成年人像提示词，
从图片反推提示词，输出 ComfyUI 可直接粘贴的正/负向提示词、工作流 JSON，或直接推送到 ComfyUI 出图队列。
支持 **Z-Image-Turbo 一键出图**，并会把 ComfyUI 生成的图片**自动拉回并归档**到容器持久化卷，
之后即使出图那台机器关机，也能在画廊里查看、搜索、复用当初的提示词。

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

- ✅ 四层中英双语词库（226 条），点选即生成；冲突词检测（同组多个光影/镜头/材质会提示）
- ✅ 私房 / 氛围感摄影词条：薄纱叠搭、斜裁真丝、布料遮蔽关系、纱帘逆光、低调私房光（仅摄影术语）
- ✅ 柔美甜欲提案板词条：樱粉 / 玫瑰红 / 香槟金 / 奶白 / 浅杏配色，法式蕾丝、丝带蝴蝶结、珍珠链、娇羞神态、明亮柔光
- ✅ 姿势参考库（可选只读挂载）：中文检索 SFW 姿势、缩略图预览、一键加入 ③ 层、复制参考图路径
- ✅ Tag 串（CLIP / SDXL / Pony）与自然语言（T5 / Flux / SD3）双模式
- ✅ 权重语法切换：`(tag:1.2)` / `((tag))` / `{tag}` / `[tag]` / 不加权重；层权重 ①1.25 ②1.15 ③1.05 ④1.0
- ✅ 图生提示词：PNG 元数据（零依赖）→ WD14 ONNX 本地反推 → 外部视觉接口（按四层归位）
- ✅ 正/负向提示词、四层分段预览、冲突与合规警告
- ✅ 导出 `.txt`、ComfyUI 工作流 JSON、一键推送到 ComfyUI `/prompt`
- ✅ **Z-Image-Turbo 一键出图**：`zimage-int8`（8189 cu130，约 9.7 s/张）/ `zimage-gguf`（8188 秋叶包）双变体，
  官方参数 8 步 / CFG 1 / res_multistep / simple / AuraFlow shift 3；`sd` 仍保留原通用模板
- ✅ **出图自动归档**：后台轮询 `/history` → 经 `/view` 把成品图拉回 `DATA_DIR/gallery/YYYYMM/`，
  反解出提示词、seed、步数、模型、尺寸存入索引；画廊支持看大图、搜索、下载、复制词、删除
- ✅ 预设保存/载入（持久化到挂载卷），内置 7 套成年人像模板（含 2 套私房摄影 + 1 套柔美甜欲提案板）
- ✅ 登录界面与访问控制：首次内网创建管理员、会话 Cookie、失败锁定、反代 HTTPS、CORS/CSRF 防护
- ✅ 合规底线：词库与反推结果过滤未成年/露骨标签，默认负向词含排除项

---

## 二、Unraid 部署（推荐）

### 方式 A：docker compose（最省事）

把 `docker-compose.yml` 放到 `/mnt/user/appdata/comfyui-prompt-studio/`，然后：

```bash
cd /mnt/user/appdata/comfyui-prompt-studio
mkdir -p data models

# 1) COMFYUI_URL 默认已指向 A 房 frp 隧道端口 18189（→ B 房 cu130 / Z-Image INT8）
#    若 ComfyUI 就在同一台 NAS 上，改成 http://host.docker.internal:8188 或 http://<NAS-IP>:8188
#    ⚠️ 容器内 127.0.0.1 指向容器自己，绝对不能填 127.0.0.1
#    确认引擎：WORKFLOW_ENGINE=zimage-int8（用 8188 秋叶包则填 zimage-gguf）

docker compose pull
docker compose up -d
docker compose logs -f
```

浏览器打开 `http://<NAS-IP>:8199`，首次访问会跳到 `/login` 让你创建管理员账号（详见下方「登录与安全」）。

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
| 数据 | `/app/data` | `/mnt/user/appdata/comfyui-prompt-studio/data` | 用户预设 + `auth.json` + **出图归档**（`gallery/YYYYMM/` 与 `gallery.json`）都持久化在这里；首次启动自动补齐内置词库/指南，不覆盖已有文件。**归档不需要新增卷** |
| 模型 | `/app/models/wd14` | `.../models` | 可选，放 `model.onnx` + `selected_tags.csv` 后启用 WD14 |
| 姿势库 | `/app/reference` | `.../ai-pose-gallery-zh` | 可选**只读**挂载，镜像内不含任何上游数据与图片 |
| 属主 | PUID/PGID | 99 / 100 | Unraid 的 `nobody:users` |

### 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PORT` | `8199` | 容器内监听端口 |
| `DATA_DIR` | `/app/data` | 词库、指南、用户预设 |
| `MODEL_DIR` | `/app/models/wd14` | WD14 模型目录 |
| `COMFYUI_URL` | `http://127.0.0.1:8188` | 目标 ComfyUI；**容器部署必须改**。支持内嵌凭据 `http://user:pass@host:port`（ComfyUI `--auth` 时必需，凭据不会出现在日志/响应里）。跨房经 frp 隧道时填 `http://host.docker.internal:18189` |
| `WORKFLOW_ENGINE` | `sd` | 出图引擎：`zimage-int8` / `zimage-gguf` / `sd`。默认 `sd` 保持旧行为；用 Z-Image 必须显式设置 |
| `GALLERY_ENABLED` | `true` | 是否开启出图自动归档 |
| `GALLERY_POLL_INTERVAL` | `8` | 轮询 `/history` 的秒数；ComfyUI 不可达时自动指数退避（最多 60 s） |
| `GALLERY_INIT_MODE` | `from_now` | `from_now`＝容器启动后只归档新出的图（跳过已有历史，避免启动瞬间拉回上百张旧图）；`all`＝连历史一起回填 |
| `GALLERY_HISTORY_LIMIT` | `64` | 每轮只取最近 64 条 history，避免反复搬运完整历史打爆隧道；「扫描全部历史」按钮不受此限 |
| `GALLERY_TIMEOUT` | `15` | 单次 ComfyUI 请求超时（秒） |
| `GALLERY_MAX_IMAGE_MB` | `20` | 单张图大小上限，超过则跳过并记录失败 |
| `GALLERY_MAX_ITEMS` | `0` | `0`＝不自动淘汰；填数字则只保留最近 N 条，超出部分连图片文件一起删除 |
| `WD14_THRESHOLD` | `0.35` | WD14 置信度阈值 |
| `REMOTE_VISION_URL` | 空 | OpenAI 兼容视觉接口 `/v1/chat/completions` |
| `REMOTE_VISION_KEY` | 空 | Bearer Token（可选） |
| `REMOTE_VISION_MODEL` | `gpt-4o-mini` | 视觉模型名 |
| `REMOTE_VISION_MODE` | `openai` | `openai`（四层 JSON）或 `raw`（自定义反推服务） |
| `REMOTE_VISION_TIMEOUT` | `45` | 超时秒数 |
| `APP_PASSWORD` | 空 | 预设管理员口令，设置后跳过首次设置页；至少 8 位、不要纯数字/纯字母 |
| `APP_USERNAME` | `admin` | 管理员账号名 |
| `SESSION_TTL_DAYS` | `30` | 会话有效期（天），访问时滑动续期 |
| `TRUST_PROXY` | `true` | 反代后读取 `X-Forwarded-*`（用于判定 HTTPS 与来源 IP） |
| `REQUIRE_HTTPS` | `false` | 为 `true` 时把 http 请求 308 跳到 https（`/api/health` 例外） |
| `ALLOW_PUBLIC_SETUP` | `false` | 是否允许公网来源创建首个管理员 |
| `LOGIN_MAX_ATTEMPTS` / `LOGIN_LOCKOUT_MINUTES` | `5` / `5` | 失败锁定阈值与初始锁定时长 |
| `ALLOWED_ORIGINS` | 空 | 跨站白名单（逗号分隔）；留空 = 严格同源 |
| `REFERENCE_DIR` | `/app/reference` | 姿势参考库的只读挂载点 |
| `POSE_REFERENCE_ENABLED` | `true` | 设为 `false` 可完全关闭姿势参考库 |
| `POSE_ADULT_MODE` | `false` | 成人模式总开关；开启后仍需登录 + 内网直连 + 界面二次确认才解锁 |
| `DEFAULT_VIEW` | `studio` | 默认首页：`studio`=AI 角色合成台，`workbench`=四层工作台（浏览器会记住上次选择） |
| `PUID` / `PGID` | `99` / `100` | Unraid 卷属主 |

> 访问密钥一律走环境变量，不写进代码，也不写进镜像。

### 登录与安全（与拓竹耗材管家同一套口径）

首次启动后打开 `http://<NAS-IP>:8199` 会跳转到 `/login`：

- **首次使用**：显示「初始化管理员」，填账号（默认 `admin`）与口令 → 创建后自动登录。
  **仅允许内网 / 回环 TCP 直连创建**（看对端地址，不看 `X-Forwarded-For`），避免挂上公网被抢先注册；
  公网部署请先在 compose 里设 `APP_PASSWORD`，或临时把 `ALLOW_PUBLIC_SETUP` 设为 `true`。
- **之后**：账号 + 口令登录，可勾「保持登录状态」；主界面右上角「退出」可注销。
- 口令用 **PBKDF2-HMAC-SHA256 加盐哈希**存 `data/auth.json`，比对走 `hmac.compare_digest`；
  磁盘只存会话令牌的 SHA-256 摘要，改口令后所有旧会话立即失效。
- Cookie 为 `HttpOnly` + `SameSite=Lax`，反代 HTTPS 下自动加 `Secure`；
  连续 5 次失败按 IP 锁定，锁定时间递增（上限 1 小时）。
- 所有 `/api/*` 与页面均需会话（`/api/health` 例外，供容器健康检查）；
  CORS 默认严格同源，写操作额外校验 `Origin`，避免第三方网页跨站调用。
- 挂 Lucky / Nginx 反代时保持 `TRUST_PROXY=true`；公网建议 `REQUIRE_HTTPS=true`。

> 忘记口令：删除挂载卷里的 `data/auth.json` 并重启容器，即可重新走首次设置。
> 口令哈希与会话文件已在 `.gitignore` / `.dockerignore` 中排除，不会进仓库或镜像。

---

## 三、图生提示词：三条路径

1. **图片元数据（零依赖，最可靠）**：ComfyUI / A1111 / NovelAI 生成的 PNG 会把提示词写进
   `tEXt` / `iTXt` / `zTXt` 块。工作台能解析 `parameters` 的 `Negative prompt:`、`Comment` 的
   NovelAI JSON，以及 ComfyUI `prompt` 工作流里 `CLIPTextEncode` 节点的正/负向文本；JPEG 走 EXIF / XMP。
2. **WD14 ONNX 本地反推（可选）**：见 `models/README.md`。输出带置信度条，勾选后合并到四层。
3. **外部视觉接口（可选）**：配 `REMOTE_VISION_URL`，模型按四层输出 JSON，点「按四层归位」自动回填。

反推结果只当草稿：删冲突词、补 ④ 层的皮肤纹理与镜头，再出图。

---

## 三之二、姿势参考库（可选只读挂载）

工作台可以对接 [`anhao/ai-pose-gallery-zh`](https://github.com/anhao/ai-pose-gallery-zh) 的中文姿势索引。
**镜像里不含该仓库的任何数据或图片**，由你自己在 NAS 上 clone 并以只读卷挂进来。

> 为什么不打包：上游仓库约 **1.1 GB / 12,022 张图**；其 `LICENSE` 明确 MIT 只覆盖源代码，
> `img/`、`thumbs/`、`data/` 著作权归原作者（ギア @giaaibusi）且不得商用、禁止打包分发；
> 仓库还含 18+ 分类。把它们打进我们发布的 GHCR 镜像既不合规，也会毁掉 60 MB 零依赖的设计。

**启用（Unraid）**

```bash
cd /mnt/user/appdata
git clone --depth 1 https://github.com/anhao/ai-pose-gallery-zh.git
# 然后取消 docker-compose.yml 里这一行的注释：
#   - /mnt/user/appdata/ai-pose-gallery-zh:/app/reference:ro
docker compose up -d
```

打开左侧「姿势参考库」面板即可用；未挂载时面板会给出提示，其余功能不受影响。

**过滤规则（硬约束）**

| 项 | 规则 |
| --- | --- |
| 默认 SFW | 只索引「全身单人・标准系」「上半身单人」 |
| 成人模式（可选） | 需 `POSE_ADULT_MODE=true` + 登录 + 内网直连 + 界面二次确认；额外开放「全身单人・性感系」单人人体动态参考 |
| 始终排除 | 「双人缠绵」体系、露骨性行为 / 性器官描述、BDSM 器具与调教词、未成年或疑似未成年内容——任何模式都不开放 |
| 名称过滤 | SFW 模式额外屏蔽成人向氛围词；成人模式仍执行硬边界词过滤 |
| 图片接口 | 只放行当前索引内登记的路径（SFW 与已解锁成人路径分开管理）+ 目录穿越校验 |
| 会话与解锁 | 所有接口需登录；解锁状态仅在进程内存，重启容器自动回到 SFW |

**界面能做什么**

- 中文关键词 / 分类检索，按「姿势编号」聚合多视角（正面 / 斜侧 / 侧面 / 俯视 / 仰视）。
- 显示 SFW 缩略图（直接从只读卷读，不经过外网）。
- 「加入③层」把该姿势对应的英文基础姿势词（如 `standing pose` / `kneeling pose` / `leaning forward`）加进 ③ 动作与场景层，立刻参与合成。
- 「路径」复制参考图相对路径，可把 `img/` 下的白模或 `img/depth/` 下的深度图丢进 ComfyUI 的 ControlNet（OpenPose / Depth）作为姿势骨架参考；提示词里注明「仅参考姿势，不参考服装 / 发型 / 画风」。

### 成人模式（18+，默认关闭）

在 compose 里设 `POSE_ADULT_MODE: "true"` 并 `docker compose up -d`，面板会出现「解锁成人内容（18+）」按钮。
点击后弹出确认框，服务端再校验三件事：**已登录**、请求来自**内网 / 回环 TCP 直连**、请求带显式确认标记；缺一即 403。
解锁只存在于容器进程内存，重启容器或点「重新锁定」立即回到 SFW。

- 只额外开放「全身单人・性感系」的**单人**人体动态参考（3D 素体白模 / 深度图），供 ControlNet 姿势骨架练习。
- 「双人缠绵」、露骨性行为与性器官描述、BDSM 器具 / 调教 / 服从类词、任何未成年或疑似未成年内容**不开放**，也不能通过改配置绕过。
- 图片仍全部来自你自行挂载的只读卷：**不上传、不写进镜像、不进仓库**；上游版权与「不得商用 / 禁止再分发」条款不变。
- 仅限成年人使用。工具可能挂在公网反代后面，所以解锁额外限制在内网直连，避免公网来源打开。

> 若要在 ComfyUI 画布内使用完整 6,162 条可视化选图器，按上游 README 的「模式 A」把仓库 clone 到
> `ComfyUI/custom_nodes/ai-pose-gallery-zh` 即可；本工作台只负责提示词侧的 SFW 姿势索引对接。
> 上游图片与数据版权归原作者，仅供个人学习、人体动态研究与合法创作，不得商用或再分发。

---

## 四、和 ComfyUI 对接（Z-Image-Turbo 一键出图 + 自动归档）

### 4.1 选引擎

`WORKFLOW_ENGINE` 决定「导出工作流 JSON」和「推送到 ComfyUI」生成什么节点：

| 引擎 | 对应环境 | 关键节点 | 默认参数 |
| --- | --- | --- | --- |
| `zimage-int8`（推荐） | B 房 cu130 / ComfyUI 0.45.0 / 8189 | `UNETLoader` + `CLIPLoader` | 1024×1024 · 8 步 · CFG 1 |
| `zimage-gguf` | B 房秋叶包 / ComfyUI 0.44.0 / 8188 | `UnetLoaderGGUF` + `CLIPLoaderGGUF` | 同上 |
| `sd` | 原通用模板（默认，保持旧行为） | `CheckpointLoaderSimple` | 896×1152 · 28 步 · CFG 6.5 |

模型文件名写在 `app/zimage_workflow.py` 的 `MODELS` 里，也可在界面「模型文件」框覆盖
`unet_name`。Z-Image 是 **CFG=1 的蒸馏模型，负向提示词不生效**（官方模板用
`ConditioningZeroOut`），工作台生成的负向词可以忽略，不是配置错误。尺寸必须是 16 的倍数，
服务端会自动向下对齐。

`COMFYUI_URL` 指向真正的出图机。跨房 frp 隧道场景里，ComfyUI 在 B 房、工作台在 A 房 Unraid：

```
[手机 / 外网] ──HTTPS──▶ A房 Lucky 反代 ──▶ prompt-studio 容器(8199)
                                              │
                          COMFYUI_URL = http://host.docker.internal:18189
                                              │  (A房 frps 隧道端口 → B房 frpc)
                                              ▼
                                    B房 ComfyUI 8189 / RTX4060 8G
                                              │ 出图落在 B房 output/
                                              ▼
        /history → /view 把成品图拉回 A房 ──▶ ./data/gallery/YYYYMM/*.png
```

若 B 房 ComfyUI 开了 `--auth`，把凭据写进 URL：`http://用户名:密码@host.docker.internal:18189`。
服务端会剥离凭据后再拼接地址、打日志、返回响应，所有 `/prompt`、`/history`、`/view`
请求统一带上 `Authorization` 头。

### 4.2 出图自动归档（"生成后自动存到 docker 里"）

后台守护线程每 `GALLERY_POLL_INTERVAL` 秒拉一次 ComfyUI `/history`，
发现**新出图**就走下面这条链路，全程不需要改 ComfyUI、不需要额外端口、不需要 SMB 共享：

1. `GET /history?max_items=64` → 找出还没归档过的 `prompt_id`；
2. `GET /view?filename=...` → 把成品图 bytes 拉回 A 房；
3. 写入 `DATA_DIR/gallery/YYYYMM/<id>_<序号>_<原文件名>`（按月份分目录）；
4. 从 history 里的工作流反解出**正向词、负向词、seed、步数、CFG、采样器、尺寸、模型名、引擎**，
   连同图片相对路径写进 `DATA_DIR/gallery.json`；
5. 工作台右栏「出图归档」自动出现新图（推送成功后会立即唤醒一次扫描，不必等下一个轮询周期）。

`DATA_DIR` 就是已经挂到 Unraid 的 `./data` 卷，所以**图片是真正持久化在宿主机上的**：
容器重建、B 房关机、隧道断开都不影响查看。宿主机路径
`/mnt/user/appdata/comfyui-prompt-studio/data/gallery/`，可以直接在 NAS 里备份。

覆盖两种出图来源：

- 工作台「推送到 ComfyUI」提交的任务 —— 推送成功后立即唤醒扫描；
- 在 ComfyUI 网页里**手动 Queue** 的任务 —— 由轮询兜底扫到。

> 首次启动默认是 `GALLERY_INIT_MODE=from_now`：先把 ComfyUI 现有历史登记为"已见"，
> **不会**把上百张旧图一次性拉回来打爆隧道。想回填历史，把该变量设为 `all`，
> 或在画廊里点「扫描全部历史」（会拉全量 history，补回所有未归档的图）。
> ComfyUI 不可达时线程自动退避到最多 60 s、日志降噪，不影响工作台其他功能。

### 4.3 画廊能做什么

| 操作 | 位置 | 说明 |
| --- | --- | --- |
| 看大图 / 参数 | 点缩略图或「查看」 | 显示原图、模型、seed、步数、CFG、尺寸、时间、完整提示词 |
| 搜索 | 顶部搜索框 | 按提示词 / 模型 / 引擎 / prompt_id 模糊匹配 |
| 复用提示词 | 卡片「复用词」 | 把这条正向词直接填回右侧输出框，接着改四层继续出图 |
| 下载 / 复制 | 详情弹窗 | 下载原图，或复制正向提示词 |
| 删除 | 详情弹窗 | 删记录 + 删对应图片文件；已删 id 会记进索引，重启后不会复活 |
| 手动扫描 | 「扫描全部历史」 | 补回没被自动扫到的历史出图 |

所有画廊接口都需要登录，图片接口只放行 `gallery.json` 里登记过的文件并做目录穿越校验。

### 4.4 其它对接方式（保留）

- **复制粘贴**：右侧「正向提示词 / 负向提示词」，直接贴进两个 `CLIPTextEncode`。
- **导出工作流 JSON**：按当前引擎生成，导入 ComfyUI 即可用；`sd` 引擎仍需把 ① 号节点的
  `ckpt_name` 换成你的模型。
- **扫描全部历史**：见上。

---

## 四之二、AI 角色合成台（Studio，默认首页）

按 `ai-pose-gallery-zh` 的「AI 角色合成台」版式做了同源界面：左侧竖导航、顶部连接状态、左主区
「1. 参考图组合 / 2. 模型与参数」、右侧「生成结果」。默认打开 Docker 就是这一页；
左侧导航可切回四层工作台，选择记在浏览器 `localStorage`；`DEFAULT_VIEW=workbench` 可改回默认工作台。

**与上游 Studio 的关键差别**：上游是浏览器直连 ComfyUI，截图里的报错正是 CORS 与 `127.0.0.1`
指向浏览器本机。本工作台的所有 ComfyUI 请求都由**服务端代理**：

| 能力 | 服务端端点 | 转发到 ComfyUI |
| --- | --- | --- |
| 检测连接 | `/api/comfy/status` | `/system_stats` |
| 上传参考图 | `/api/comfy/upload` | `/upload/image`（multipart） |
| 提交工作流 | `/api/comfy/submit` | `/prompt`（自动填 `CLIPTextEncode` 与 `LoadImage`） |
| 轮询结果 | `/api/comfy/result` | `/history/{prompt_id}` |
| 取回成品图 | `/api/comfy/view` | `/view` |

所以**不需要** `--enable-cors-header`，浏览器也不必能访问 `COMFYUI_URL`，只要容器能连到 ComfyUI 即可；
`COMFYUI_URL` 内嵌的 `--auth` 凭据由服务端透传、不回显。提交成功后会唤醒出图归档线程，
成品图同时出现在「出图归档」里。

**界面能做**：三槽位（人像 / 服装 / 姿势）换一个 / 同类 / 移除、随机捏人、参考拼图（Canvas 合成可下载 PNG）、
附加参考图（上传 / Ctrl+V / URL）、工作流 JSON 导入 / 编辑 / 导出、画风预设、尺寸 / 步数 / CFG / Seed、
生成结果轮询与失败诊断。正向 / 负向提示词会自动带入四层工作台的合成结果。

> 镜像与仓库仍不含任何上游代码、数据或图片，三库元数据全部来自你自行挂载的只读卷。
> 参考图能否真正参与生成，取决于工作流里是否有 `LoadImage` / IP-Adapter / ControlNet 节点：
> 默认模板是基础文生图，导入含这些节点的 API 工作流后会自动把第一张上传图注入 `LoadImage`。

---

## 五、镜像发布链路（复用拓竹耗材管家的 git 密钥）

`.github/workflows/docker.yml` 与 `bambu-spool` 完全同一套：仓库内置的
`secrets.GITHUB_TOKEN`（`permissions: packages: write`）在 push 到 `main` 或打 `v*` 标签时
自动跑自测 → 构建 → 推到 `ghcr.io/${{ github.repository }}`。
**NAS 上不需要任何 git 密钥**，`docker compose pull` 即可取到镜像。

> 仓库当前为 public，GHCR 镜像已验证可匿名拉取，Unraid 无需 `docker login`。
> 若把仓库改成 private，包默认也会变 private，需先 `docker login ghcr.io -u <用户名> -p <PAT>`。

---

## 六、本地运行与验证（无需 Docker）

```bash
python -m app.server              # http://127.0.0.1:8199
python tests/smoke_test.py        # 原有 20 项零依赖冒烟测试
python tests/test_gallery.py      # 出图归档 / Z-Image 端到端测试（自带假 ComfyUI）
node --check app/static/app.js    # 前端语法检查
```

`smoke_test.py` 覆盖：词库加载、四层合成（tag / 自然语言 / 权重 / 合规过滤）、工作流生成、
PNG 元数据反推（含 ComfyUI workflow 正/负向节点）、登录/初始化/会话/越权/登出/改口令/Origin 校验、
全部 HTTP 接口与错误分支。

`test_gallery.py` 用本地假 ComfyUI 顶替出图机，覆盖 14 项检查：`--auth` 凭据透传与不回显、
`/history → /view → 落盘 → 元数据反解`、Z-Image 工作流生成与 `/prompt` 推送、画廊列表/搜索/
原图读取、路径穿越与未登录防护、手动全量扫描、删除后文件清理与重启不复活。
两个测试都会跑在 GitHub Actions 里，不通过就不推镜像。

---

## 七、已知限制与定位

- 工具定位于**成年人像摄影**提示词工程；词库与反推结果均过滤未成年与露骨标签，负向词默认含合规排除项。
- WD14 推理依赖需 `--build-arg WITH_ONNX=1` 额外安装，默认镜像不含模型与推理库。
- `sd` 引擎导出的仍是 SD 系通用 `KSampler` 模板；用 Z-Image-Turbo 请把 `WORKFLOW_ENGINE`
  设为 `zimage-int8` / `zimage-gguf`。Z-Image 的模型文件名按本机环境写死在
  `app/zimage_workflow.py`，换量化版本时改 `MODELS` 或在界面覆盖。
- 自动归档依赖 ComfyUI 的 `/history` 保留记录；若 B 房清空了 history 或重启了 ComfyUI，
  历史出图可能扫不到（新出的图不受影响）。`GALLERY_INIT_MODE=from_now` 不会回填旧图，
  需要时点「扫描全部历史」。
- 自动归档只收 `outputs.images`（SaveImage / PreviewImage 一类），不收视频、GIF 与自定义节点输出。
- 图片按原图 bytes 存储，不做压缩；长时间大量出图请关注 `./data` 卷容量，
  或用 `GALLERY_MAX_ITEMS` 设上限。
- 有登录与会话保护，但仍是**单账号**、无多用户/权限分级；公网暴露建议再叠加反代 Basic Auth 或统一认证。

## 参考来源

`github.com/WolfChen1996/NovelAI-tag-generator` · `wolfchen.top/tag/doc.html` ·
`prompt.qpipi.com` · `tags.novelai.dev`（Danbooru 标签超市）· `magic-tag.netlify.app`（魔导绪论） ·
`github.com/anhao/ai-pose-gallery-zh`（可选只读挂载，仅取 SFW 姿势索引）
