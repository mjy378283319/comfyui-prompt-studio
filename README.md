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

- ✅ 四层中英双语词库（226 条），点选即生成；冲突词检测（同组多个光影/镜头/材质会提示）
- ✅ 私房 / 氛围感摄影词条：薄纱叠搭、斜裁真丝、布料遮蔽关系、纱帘逆光、低调私房光（仅摄影术语）
- ✅ 柔美甜欲提案板词条：樱粉 / 玫瑰红 / 香槟金 / 奶白 / 浅杏配色，法式蕾丝、丝带蝴蝶结、珍珠链、娇羞神态、明亮柔光
- ✅ 姿势参考库（可选只读挂载）：中文检索 SFW 姿势、缩略图预览、一键加入 ③ 层、复制参考图路径
- ✅ Tag 串（CLIP / SDXL / Pony）与自然语言（T5 / Flux / SD3）双模式
- ✅ 权重语法切换：`(tag:1.2)` / `((tag))` / `{tag}` / `[tag]` / 不加权重；层权重 ①1.25 ②1.15 ③1.05 ④1.0
- ✅ 图生提示词：PNG 元数据（零依赖）→ WD14 ONNX 本地反推 → 外部视觉接口（按四层归位）
- ✅ 正/负向提示词、四层分段预览、冲突与合规警告
- ✅ 导出 `.txt`、ComfyUI 工作流 JSON、一键推送到 ComfyUI `/prompt`
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

# 1) 先把 COMFYUI_URL 改成 NAS 上 ComfyUI 的地址
#    http://host.docker.internal:8188  或  http://192.168.x.x:8188
#    ⚠️ 容器内 127.0.0.1 指向容器自己，绝对不能填 127.0.0.1

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
| 数据 | `/app/data` | `/mnt/user/appdata/comfyui-prompt-studio/data` | 用户预设 + `auth.json`（口令哈希与会话）持久化；首次启动自动补齐内置词库/指南，不覆盖已有文件 |
| 模型 | `/app/models/wd14` | `.../models` | 可选，放 `model.onnx` + `selected_tags.csv` 后启用 WD14 |
| 姿势库 | `/app/reference` | `.../ai-pose-gallery-zh` | 可选**只读**挂载，镜像内不含任何上游数据与图片 |
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
| 体系白名单 | 只索引「全身单人・标准系」「上半身单人」；性感系 / 双人缠绵（18+）一律排除 |
| 名称过滤 | 含性感 / 缠绵 / 后入 / 挤胸 / 裸露 / 内衣 / 挑逗等词的姿势名排除 |
| 图片接口 | 只放行索引内登记的 SFW 图片路径，且做目录穿越校验；无法借接口读取其它图片 |
| 会话 | 与其它 `/api/*` 一样需要登录 |

**界面能做什么**

- 中文关键词 / 分类检索，按「姿势编号」聚合多视角（正面 / 斜侧 / 侧面 / 俯视 / 仰视）。
- 显示 SFW 缩略图（直接从只读卷读，不经过外网）。
- 「加入③层」把该姿势对应的英文基础姿势词（如 `standing pose` / `kneeling pose` / `leaning forward`）加进 ③ 动作与场景层，立刻参与合成。
- 「路径」复制参考图相对路径，可把 `img/` 下的白模或 `img/depth/` 下的深度图丢进 ComfyUI 的 ControlNet（OpenPose / Depth）作为姿势骨架参考；提示词里注明「仅参考姿势，不参考服装 / 发型 / 画风」。

> 若要在 ComfyUI 画布内使用完整 6,162 条可视化选图器，按上游 README 的「模式 A」把仓库 clone 到
> `ComfyUI/custom_nodes/ai-pose-gallery-zh` 即可；本工作台只负责提示词侧的 SFW 姿势索引对接。
> 上游图片与数据版权归原作者，仅供个人学习、人体动态研究与合法创作，不得商用或再分发。

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

> 仓库当前为 public，GHCR 镜像已验证可匿名拉取，Unraid 无需 `docker login`。
> 若把仓库改成 private，包默认也会变 private，需先 `docker login ghcr.io -u <用户名> -p <PAT>`。

---

## 六、本地运行与验证（无需 Docker）

```bash
python -m app.server              # http://127.0.0.1:8199
python tests/smoke_test.py        # 零依赖端到端自测
node --check app/static/app.js    # 前端语法检查
```

自测覆盖：词库加载、四层合成（tag / 自然语言 / 权重 / 合规过滤）、工作流生成、
PNG 元数据反推（含 ComfyUI workflow 正/负向节点）、登录/初始化/会话/越权/登出/改口令/Origin 校验、
全部 HTTP 接口与错误分支。

---

## 七、已知限制与定位

- 工具定位于**成年人像摄影**提示词工程；词库与反推结果均过滤未成年与露骨标签，负向词默认含合规排除项。
- WD14 推理依赖需 `--build-arg WITH_ONNX=1` 额外安装，默认镜像不含模型与推理库。
- 导出的工作流是 SD 系通用 `KSampler` 模板；Flux 等需换成对应节点，提示词本身可直接复用。
- ComfyUI 推送只负责提交 `/prompt`，不查询执行结果。
- 有登录与会话保护，但仍是**单账号**、无多用户/权限分级；公网暴露建议再叠加反代 Basic Auth 或统一认证。

## 参考来源

`github.com/WolfChen1996/NovelAI-tag-generator` · `wolfchen.top/tag/doc.html` ·
`prompt.qpipi.com` · `tags.novelai.dev`（Danbooru 标签超市）· `magic-tag.netlify.app`（魔导绪论） ·
`github.com/anhao/ai-pose-gallery-zh`（可选只读挂载，仅取 SFW 姿势索引）
