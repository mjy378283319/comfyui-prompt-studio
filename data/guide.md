# 高质感成年人像：模块化提示词心法

> 秘诀不在于「每次凭感觉乱写」，而在于构建一套**可复用、可单变量迭代**的模块化模板。
> 一个清晰的提示词由四个层面自上而下递进组成：

| 层 | 名称 | 要解决什么 | 写错会怎样 |
| --- | --- | --- | --- |
| ① | 角色固定层 | 年龄、发色、五官与基础身材轮廓 | 每次出图都是另一个人，无法复现 |
| ② | 服饰与遮蔽层 | 遮挡状态，以及布料材质 | 光落不到身上，材质像塑料 |
| ③ | 动作与场景层 | 卧姿、跪姿、倚靠姿态；卧室、浴缸或落地窗前等环境 | 姿态与环境互相打架，肢体穿帮 |
| ④ | 摄影与画质层 | 光影方向、镜头焦段、皮肤纹理要求 | 平光、脸部变形、自动磨皮成蜡像 |

---

## 一、为什么必须分层

CLIP 类文本编码器（SD 1.5 / SDXL / Pony）对**顺序敏感**：靠前的 token 权重更高。
T5 类编码器（Flux / SD3）对**句子结构敏感**：自然语言完整句远好于 tag 堆砌。
分层同时满足两者：先在四层里把语义定死，再由合成器按模型切换输出形态。

**四条纪律**

1. **一层只干一件事。** 别在「角色固定层」里写光影，也别在「摄影层」里重写长相。
2. **上层稳定，下层可换。** 换风格只动 ② ③ ④，① 永远不动，这样 A/B 对比才有意义。
3. **每层 2–5 个词条足够。** 超过 8 个会互相稀释；`masterpiece, best quality` 只放最前。
4. **冲突词不共存。** `soft window light` 与 `harsh direct flash`、`matte silk` 与 `glossy latex` 不要同时出现。

---

## 二、逐层写法

### ① 角色固定层
- 年龄写**明确的成年数字**（28 / 35 / 42），比 `young` 稳定得多；也避免模型滑向未成年人。
- 五官写**结构**不写评价：`high cheekbones / soft jawline / almond-shaped eyes` 有效，`beautiful / perfect face` 基本无效。
- 身材只写**轮廓词**：`slender build / athletic build / tall and lean`，不写具体部位尺寸。

### ② 服饰与遮蔽层
先写**遮蔽关系**（`layered outfit` / `off-shoulder neckline` / `draped shawl`），再写**材质**。
材质决定光学表现，是「高质感」最大的杠杆：

| 材质 | 光的表现 | 适用光影 |
| --- | --- | --- |
| `crisp linen` 亚麻 | 吸光、边缘清晰、有明显织纹 | 侧光、窗光 |
| `matte silk` 哑光真丝 | 柔和流动的高光带 | 逆光、漫射光 |
| `fine-knit cashmere` 羊绒 | 几乎无反光、绒面散射 | 顶部漫射、柔光箱 |
| `sheer chiffon` 雪纺 | 透光、层次叠加 | 背光、轮廓光 |
| `ribbed wool` 罗纹羊毛 | 竖向明暗条纹 | 斜侧光 |

### ③ 动作与场景层
姿态与环境要**成对成立**：卧姿配 `linen bedding`；跪姿配窗台或地毯；倚靠配墙面、窗框。
环境同时要交代**光的来源**（`floor-to-ceiling window` → 下一层写 `soft window light from the left`），两层形成因果链。

### ④ 摄影与画质层（质感开关）
写实人像翻车九成出在这一层，三件套缺一不可：

- **光影方向**：`soft window light from the left` / `backlit rim light` / `Rembrandt lighting`。只写 `good lighting` 等于没写。
- **镜头焦段**：`85mm f/1.4` / `135mm f/2` / `medium format look`。不写焦段，模型容易吐出广角畸变脸。
- **皮肤纹理**：`natural skin texture, visible pores, subtle peach fuzz, no over-smoothing`。这是摆脱「塑料人」的关键，必须**正向写**，光靠负面词压不住。

---

## 三、权重与顺序（来自实测规则）

| 语法 | 每层权重 | 适用 |
| --- | --- | --- |
| `{tag}` | ×1.05 | NovelAI / Naifu |
| `((tag))` | ×1.10 | SD WebUI |
| `(tag:1.2)` | 精确数值 | **ComfyUI 推荐** |
| `[tag]` | 降权 | 次要氛围词 |

- 顺序即优先级：本工具按 ① → ④ 输出，角色永远排最前。
- 层权重建议 `① 1.25 / ② 1.15 / ③ 1.05 / ④ 1.0`，不要给所有词都加权，全加权等于没加权。
- 负面提示词分三段：通用瑕疵（`lowres, bad anatomy…`）、人像专项（`plastic skin, waxy face…`）、合规底线（`child, teen, nsfw…`）。Flux / SD3 的负面留空。

---

## 四、自然语言模式怎么写（Flux / SD3）

自然语言不是「把 tag 用逗号连起来加个句号」，而是**把四层改写成四个陈述句**：

```
A high-quality adult portrait of a 32-year-old woman with dark brown hair,
high cheekbones and a slender build.
Attire and layering: a crisp linen shirt under a matte silk robe, ivory tones.
Pose and setting: kneeling beside a tall window in a quiet bedroom during golden hour.
Photography: soft window light falling from the left, shot on an 85mm f/1.4 lens,
natural unretouched skin texture with finely visible pores, restrained editorial grading.
```

要点：主语只出现一次；一句一个层面；用具体名词与方向词；不要堆 `masterpiece` 这类空洞词（T5 会当噪声）。

---

## 五、从图片反推提示词

三种来源，按可靠性排序：

1. **图片元数据（零模型，最可靠）**：ComfyUI / A1111 / NovelAI 生成的 PNG 会把正向、负向提示词和工作流写进 tEXt chunk。本工具直接解析，连工作流里的 `CLIPTextEncode` 节点都能抠出来。
2. **WD14 Tagger 本地反推（可选增强）**：把 `model.onnx` + `selected_tags.csv` 放进 `models/wd14/`，容器启动时自动启用，输出带置信度的 Danbooru tag，可勾选后再合成。
3. **外部视觉模型（可选）**：配置一个 OpenAI 兼容的视觉接口，让模型按四层结构直接写出自然语言提示词。

反推结果**只当草稿**：勾选后按四层重新归位，删掉互相冲突的词，再补 ④ 层的皮肤纹理与镜头。

---

## 六、迭代方法

1. 固定 seed、CFG、步数，只改一个层的一个词条。
2. 先调 ① 直到「人」稳定，再调 ② ③，最后才动 ④。
3. 把跑通的模板存成预设；下次只换 ③ 的场景词，就能批量出同一人物的系列片。
4. 出图后把它拖回「图生提示词」，对照自己写的原稿，看模型实际读进了哪些词。

---

## 参考来源

- `github.com/WolfChen1996/NovelAI-tag-generator`（纯前端 tag 生成器，分类结构与中英对照词条）
- `wolfchen.top/tag/doc.html`（`{}` ×1.05 / `()` ×1.1 / `[]` 降权、正负面输入框差异）
- `prompt.qpipi.com`（分类交互、正/负向默认组合、加权符号切换）
- `tags.novelai.dev`（Danbooru 标签超市：顺序即权重、线性/指数步进、混合组、语法解析器）
- `magic-tag.netlify.app`（魔导绪论：中文搜索、加权/减权/删除模式、tag 分类）

> 本工具定位于**成年人像摄影**提示词工程。词库与反推结果均已过滤未成年相关与露骨标签，负向提示词默认包含合规排除项。
