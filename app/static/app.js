/* ComfyUI Prompt Studio — 无框架单页逻辑 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    lex: null,
    selections: { identity: [], attire: [], pose_scene: [], photo: [] },
    index: {},
    presets: [],
    image: null,
    imageName: "",
    extract: null,
    checked: new Set(),
    composeTimer: null,
    poses: null,
    gallery: { items: [], total: 0, offset: 0, query: "", current: null, status: null, online: null },
    galleryTimer: null,
  };

  async function api(path, options = {}) {
    const resp = await fetch(path, {
      method: options.method || "GET",
      headers: { "Content-Type": "application/json" },
      body: options.body ? JSON.stringify(options.body) : undefined,
    });
    let data;
    try { data = await resp.json(); } catch { throw new Error(`服务返回异常 (HTTP ${resp.status})`); }
    if (resp.status === 401) {
      window.location.href = "/login";
      throw new Error("登录已过期，正在跳转登录页");
    }
    if (!resp.ok || data.ok === false) {
      const err = new Error(data.error || `请求失败 (HTTP ${resp.status})`);
      err.hint = data.hint || "";
      throw err;
    }
    return data;
  }

  function toast(message, isError = false) {
    const node = $("toast");
    node.textContent = message;
    node.style.borderColor = isError ? "var(--danger)" : "var(--accent)";
    node.style.color = isError ? "var(--danger)" : "var(--accent)";
    node.classList.remove("hidden");
    clearTimeout(node._timer);
    node._timer = setTimeout(() => node.classList.add("hidden"), 2600);
  }

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  }

  function inlineMd(text) {
    return escapeHtml(text)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  }

  function renderMarkdown(md) {
    const lines = md.split("\n");
    const out = [];
    let inCode = false, inTable = false, inList = false;
    const closeList = () => { if (inList) { out.push("</ul>"); inList = false; } };
    const closeTable = () => { if (inTable) { out.push("</tbody></table>"); inTable = false; } };
    for (let i = 0; i < lines.length; i++) {
      const raw = lines[i];
      const line = raw.trimEnd();
      if (line.startsWith("```")) {
        if (inCode) { out.push("</pre>"); inCode = false; } else { closeList(); closeTable(); out.push("<pre>"); inCode = true; }
        continue;
      }
      if (inCode) { out.push(escapeHtml(raw)); continue; }
      if (/^\|/.test(line) && /\|/.test(line.slice(1))) {
        const cells = line.split("|").slice(1, -1).map((c) => c.trim());
        if (!inTable) {
          closeList();
          out.push("<table><thead><tr>" + cells.map((c) => `<th>${inlineMd(c)}</th>`).join("") + "</tr></thead><tbody>");
          inTable = true;
        } else if (!/^[-:\s|]+$/.test(line)) {
          out.push("<tr>" + cells.map((c) => `<td>${inlineMd(c)}</td>`).join("") + "</tr>");
        }
        continue;
      }
      closeTable();
      if (!line.trim()) { closeList(); continue; }
      if (/^#{1,3}\s/.test(line)) {
        closeList();
        const level = line.match(/^#+/)[0].length;
        out.push(`<h${level}>${inlineMd(line.replace(/^#+\s/, ""))}</h${level}>`);
      } else if (line.startsWith(">")) {
        closeList();
        out.push(`<blockquote>${inlineMd(line.replace(/^>\s?/, ""))}</blockquote>`);
      } else if (/^[-*]\s/.test(line)) {
        if (!inList) { out.push("<ul>"); inList = true; }
        out.push(`<li>${inlineMd(line.replace(/^[-*]\s/, ""))}</li>`);
      } else {
        closeList();
        out.push(`<p>${inlineMd(line)}</p>`);
      }
    }
    closeList(); closeTable();
    if (inCode) out.push("</pre>");
    return out.join("\n");
  }

  // ------------------------------------------------------------------ 初始化
  function buildIndex(lex) {
    state.index = {};
    for (const layer of lex.layers) {
      for (const group of layer.groups) {
        for (const item of group.items) state.index[item[0]] = { layer: layer.id, item };
      }
    }
  }

  function renderLayers() {
    const box = $("layerPanels");
    box.textContent = "";
    state.lex.layers.forEach((layer, idx) => {
      const card = el("details", "layer-card");
      card.open = idx < 2;
      const summary = el("summary");
      const left = el("span");
      left.append(el("span", "l-title", layer.title), el("span", "l-sub", layer.subtitle));
      const count = el("span", "l-count", "");
      count.dataset.layer = layer.id;
      summary.append(left, count);
      const body = el("div", "layer-body");
      body.append(el("p", "principle", layer.principle));
      for (const group of layer.groups) {
        const wrap = el("div", "group");
        wrap.append(el("div", "group-title", group.title));
        const chips = el("div", "chips");
        for (const item of group.items) {
          const chip = el("button", "chip", item[1]);
          chip.type = "button";
          chip.title = `${item[0]} — ${item[2]}`;
          chip.dataset.en = item[0];
          chip.dataset.layer = layer.id;
          chip.classList.toggle("active", state.selections[layer.id].includes(item[0]));
          chip.addEventListener("click", () => toggleChip(layer.id, item[0], chip));
          chips.append(chip);
        }
        wrap.append(chips);
        body.append(wrap);
      }
      card.append(summary, body);
      box.append(card);
    });
    updateCounts();
  }

  function updateCounts() {
    document.querySelectorAll(".l-count").forEach((node) => {
      const n = (state.selections[node.dataset.layer] || []).length;
      node.textContent = n ? `${n} 项` : "";
    });
  }

  function toggleChip(layerId, en, chip) {
    const list = state.selections[layerId];
    const at = list.indexOf(en);
    if (at >= 0) list.splice(at, 1); else list.push(en);
    chip.classList.toggle("active", at < 0);
    updateCounts();
    scheduleCompose();
  }

  function refreshChips() {
    document.querySelectorAll(".chip").forEach((chip) => {
      chip.classList.toggle("active", state.selections[chip.dataset.layer].includes(chip.dataset.en));
    });
    updateCounts();
  }

  function fillControls() {
    const lex = state.lex;
    const model = $("modelSelect");
    model.textContent = "";
    lex.models.forEach((m) => { const o = el("option", "", m.name); o.value = m.id; model.append(o); });
    const syntax = $("syntaxSelect");
    syntax.textContent = "";
    lex.syntax.forEach((s) => { const o = el("option", "", s.name); o.value = s.id; o.title = s.hint; syntax.append(o); });
    model.value = "sdxl";
    syntax.value = "paren_weight";
    $("modeSelect").value = "tag";
  }

  function renderBadges(engines) {
    const box = $("engineBadges");
    box.textContent = "";
    Object.entries(engines || {}).forEach(([key, info]) => {
      const badge = el("span", `badge ${info.available ? "on" : "off"}`, `${info.label} ${info.available ? "✓" : "○"}`);
      badge.title = info.hint || "";
      box.append(badge);
    });
  }

  function loadPresets(presets) {
    state.presets = presets;
    const select = $("presetSelect");
    select.textContent = "";
    const placeholder = el("option", "", "— 载入预设 —");
    placeholder.value = "";
    select.append(placeholder);
    presets.forEach((p) => {
      const o = el("option", "", (p.builtin ? "★ " : "☆ ") + p.name);
      o.value = p.id;
      select.append(o);
    });
  }

  function applyPreset(preset) {
    if (!preset) return;
    for (const layer of state.lex.layers) {
      state.selections[layer.id] = Array.isArray(preset.selections[layer.id]) ? [...preset.selections[layer.id]] : [];
    }
    $("modeSelect").value = preset.mode || "tag";
    $("modelSelect").value = preset.model || "sdxl";
    $("syntaxSelect").value = preset.syntax || "paren_weight";
    refreshChips();
    scheduleCompose();
    toast(`已载入预设：${preset.name}`);
  }

  // ------------------------------------------------------------------ 合成
  function composePayload() {
    return {
      selections: state.selections,
      mode: $("modeSelect").value,
      model: $("modelSelect").value,
      syntax: $("syntaxSelect").value,
      quality_prefix: $("qualityChk").checked,
      negative_enabled: $("negativeChk").checked,
      negative_groups: {
        common: $("negCommon").checked,
        portrait: $("negPortrait").checked,
        safety: $("negSafety").checked,
      },
      extra_positive: $("extraPositive").value,
      extra_negative: $("extraNegative").value,
    };
  }

  function scheduleCompose() {
    clearTimeout(state.composeTimer);
    state.composeTimer = setTimeout(composeNow, 220);
  }

  function renderWarnings(box, warnings) {
    box.textContent = "";
    (warnings || []).forEach((w) => box.append(el("div", "", w)));
  }

  async function composeNow() {
    try {
      const data = await api("/api/compose", { method: "POST", body: composePayload() });
      $("positiveOut").value = data.positive;
      $("negativeOut").value = data.negative;
      $("statsLabel").textContent = `${data.stats.tag_count} 词条 · ${data.stats.segment_count} 段 · 负面 ${data.stats.negative_count} 项`;
      const seg = $("segmentsBox");
      seg.textContent = "";
      data.segments.forEach((s) => {
        const wrap = el("div", "seg");
        wrap.append(el("b", "", `${s.title}（${s.count} 项${s.weight ? ` · ×${s.weight}` : ""}）`));
        wrap.append(el("p", "", s.text));
        seg.append(wrap);
      });
      renderWarnings($("composeWarnings"), data.warnings);
    } catch (err) {
      $("statsLabel").textContent = "合成失败";
      renderWarnings($("composeWarnings"), [err.message]);
    }
  }

  // ------------------------------------------------------------------ 图生提示词
  function normalizeTags(tags) {
    return (tags || []).map((t) => {
      if (typeof t === "string") return { tag: t, score: 1, source: "" };
      return { tag: String(t.tag || ""), score: Number(t.score || 0), source: String(t.source || "") };
    }).filter((t) => t.tag);
  }

  function renderExtractResult(data) {
    state.extract = data;
    $("resultEngine").textContent = {
      metadata: "图片元数据", wd14: "WD14 ONNX 反推", remote: "外部视觉接口",
    }[data.engine] || data.engine;
    const tags = normalizeTags(data.tags);
    $("resultCount").textContent = `${tags.length} 个词条`;
    state.checked = new Set(tags.map((t) => t.tag));
    const warnings = [...(data.warnings || [])];
    if (data.hint) warnings.push(data.hint);
    renderWarnings($("resultWarnings"), warnings);
    $("resultRaw").textContent = [
      data.positive ? `正向:\n${data.positive}` : "",
      data.negative ? `\n负向:\n${data.negative}` : "",
      data.meta && data.meta.source ? `\n来源: ${data.meta.source}` : "",
    ].filter(Boolean).join("\n") || "（无）";
    const list = $("tagList");
    list.textContent = "";
    tags.forEach((t) => {
      const row = el("label", "tag-item");
      const cb = el("input");
      cb.type = "checkbox";
      cb.checked = state.checked.has(t.tag);
      cb.addEventListener("change", () => { if (cb.checked) state.checked.add(t.tag); else state.checked.delete(t.tag); });
      const name = el("span", "t-name", t.tag);
      const score = el("span", "t-score", t.score < 1 ? t.score.toFixed(2) : "");
      const bar = el("span", "t-bar");
      const fill = el("i");
      fill.style.width = `${Math.min(100, Math.round(t.score * 100))}%`;
      bar.append(fill);
      row.append(cb, name, score, bar);
      list.append(row);
    });
    $("extractResult").classList.remove("hidden");
  }

  function setImageData(dataUrl, name) {
    state.image = dataUrl;
    state.imageName = name;
    const img = $("previewImg");
    img.src = dataUrl;
    img.classList.remove("hidden");
  }

  function readFile(file) {
    if (!file) return;
    if (!/^image\/(png|jpeg|jpg|webp)$/.test(file.type)) { toast("仅支持 PNG / JPEG / WebP", true); return; }
    if (file.size > 25 * 1024 * 1024) { toast("图片超过 25 MB", true); return; }
    const reader = new FileReader();
    reader.onload = () => { setImageData(reader.result, file.name); $("extractStatus").className = "status ok"; $("extractStatus").textContent = `已载入 ${file.name}，点击「提取」开始反推`; };
    reader.onerror = () => toast("读取图片失败", true);
    reader.readAsDataURL(file);
  }

  async function extractNow() {
    if (!state.image) { toast("请先选择一张图片", true); return; }
    const btn = $("extractBtn");
    btn.disabled = true;
    $("extractStatus").className = "status";
    $("extractStatus").textContent = "正在反推…";
    try {
      const data = await api("/api/extract", {
        method: "POST",
        body: {
          image: state.image, filename: state.imageName,
          engine: $("engineSelect").value,
          threshold: parseFloat($("thresholdInput").value) || 0.35,
        },
      });
      renderExtractResult(data);
      $("extractStatus").className = "status ok";
      $("extractStatus").textContent = `完成 · ${data.engine} · ${(data.tags || []).length} 个词条`;
    } catch (err) {
      $("extractStatus").className = "status err";
      $("extractStatus").textContent = err.message + (err.hint ? ` — ${err.hint}` : "");
    } finally {
      btn.disabled = false;
    }
  }

  function mergeCheckedTags() {
    const tags = [...state.checked];
    if (!tags.length) { toast("没有勾选的词条", true); return; }
    const box = $("extraPositive");
    const existing = box.value.split(",").map((s) => s.trim()).filter(Boolean);
    const merged = [...new Set([...existing, ...tags])];
    box.value = merged.join(", ");
    scheduleCompose();
    toast(`已合并 ${tags.length} 个词条到补充正向词`);
  }

  function applyExtractLayers() {
    const layers = state.extract && state.extract.layers;
    if (!layers || !Object.values(layers).some((v) => v && v.length)) {
      toast("当前引擎没有返回四层结构（建议用「外部视觉接口」）", true);
      return;
    }
    let matched = 0, extra = 0;
    const extras = $("extraPositive").value.split(",").map((s) => s.trim()).filter(Boolean);
    for (const layer of state.lex.layers) {
      for (const phrase of layers[layer.id] || []) {
        const known = state.index[String(phrase).trim()];
        if (known && known.layer === layer.id) {
          if (!state.selections[layer.id].includes(phrase)) state.selections[layer.id].push(phrase);
          matched++;
        } else {
          extras.push(String(phrase).trim());
          extra++;
        }
      }
    }
    $("extraPositive").value = [...new Set(extras)].join(", ");
    refreshChips();
    scheduleCompose();
    toast(`四层归位完成：匹配词库 ${matched} 项，自定义短语 ${extra} 项进入补充词`);
  }

  // ------------------------------------------------------------------ 导出
  async function copyText(text, label) {
    if (!text) { toast(`${label}为空`, true); return; }
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = el("textarea");
      ta.value = text;
      document.body.append(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    }
    toast(`已复制${label}`);
  }

  function download(name, content, type) {
    const blob = new Blob([content], { type });
    const url = URL.createObjectURL(blob);
    const a = el("a");
    a.href = url;
    a.download = name;
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  }

  function workflowOptions() {
    const [width, height] = $("wfSize").value.split("x").map(Number);
    return {
      width, height,
      steps: Number($("wfSteps").value) || 28,
      cfg: Number($("wfCfg").value) || 6.5,
      ckpt_name: $("wfCkpt").value.trim(),
    };
  }

  async function getWorkflow() {
    return api("/api/workflow", {
      method: "POST",
      body: {
        positive: $("positiveOut").value,
        negative: $("negativeOut").value,
        options: workflowOptions(),
      },
    });
  }

  // ------------------------------------------------------------------ 姿势参考库
  async function loadPoseStatus() {
    try {
      const data = await api("/api/poses/status");
      state.poses = data;
      const status = $("poseStatus");
      status.className = "pose-status" + (data.available ? "" : " off");
      status.textContent = data.available
        ? `已载入 ${data.count} 个 SFW 姿势 · ${data.categories.length} 个分类（仅标准系 / 上半身）`
        : data.reason;
      $("poseCount").textContent = data.available ? `${data.count} 项` : "未挂载";
      const adultState = $("poseAdultState");
      const adultBtn = $("poseAdultBtn");
      if (!data.adult_mode) {
        adultState.className = "pose-adult-state";
        adultState.textContent = "成人模式未启用：需在 compose 设置 POSE_ADULT_MODE=true（默认关闭）";
        adultBtn.textContent = "未启用";
        adultBtn.disabled = true;
      } else if (data.adult_unlocked) {
        adultState.className = "pose-adult-state on";
        adultState.textContent = "成人模式已解锁 · 仅限成年人 / 内网直连 · 重启容器自动回到 SFW";
        adultBtn.textContent = "重新锁定（回到 SFW）";
        adultBtn.disabled = false;
      } else {
        adultState.className = "pose-adult-state";
        adultState.textContent = "当前 SFW。解锁后额外显示「全身单人・性感系」单人人体动态参考；双人缠绵与露骨内容始终不开放。";
        adultBtn.textContent = "解锁成人内容（18+）";
        adultBtn.disabled = false;
      }
      const select = $("poseCategory");
      select.textContent = "";
      const all = el("option", "", "全部分类");
      all.value = "";
      select.append(all);
      (data.categories || []).forEach((name) => {
        const option = el("option", "", name);
        option.value = name;
        select.append(option);
      });
      if (data.available) await loadPoses();
      else $("poseList").textContent = "";
    } catch (err) {
      $("poseStatus").textContent = err.message;
    }
  }

  async function loadPoses() {
    if (!state.poses || !state.poses.available) return;
    const query = $("poseSearch").value.trim();
    const category = $("poseCategory").value;
    try {
      const data = await api(`/api/poses?q=${encodeURIComponent(query)}&category=${encodeURIComponent(category)}&limit=48`);
      const list = $("poseList");
      list.textContent = "";
      (data.items || []).forEach((item) => list.append(renderPoseCard(item)));
      if (!data.items.length) list.append(el("div", "pose-status", "没有匹配的姿势"));
    } catch (err) {
      $("poseList").textContent = err.message;
    }
  }

  function renderPoseCard(item) {
    const card = el("div", "pose-card");
    if (item.adult) {
      card.classList.add("adult");
      card.title = "18+ 成人向单人人体动态参考";
    }
    if (item.thumb) {
      const img = document.createElement("img");
      img.src = `/api/pose/image?path=${encodeURIComponent(item.thumb)}`;
      img.alt = item.name;
      img.loading = "lazy";
      card.append(img);
    }
    card.append(el("div", "pn", item.name));
    card.append(el("div", "pm", `${item.category} · ${(item.angles || []).join("/") || "单视角"}`));
    const buttons = el("div", "pb");
    const add = el("button", "btn tiny primary", "加入③层");
    add.title = `加入：${item.prompt}`;
    add.addEventListener("click", () => {
      if (!state.selections.pose_scene.includes(item.prompt)) state.selections.pose_scene.push(item.prompt);
      refreshChips();
      scheduleCompose();
      toast(`已加入 ③ 层：${item.prompt}`);
    });
    const path = el("button", "btn tiny ghost", "路径");
    path.title = "复制参考图相对路径（供 ComfyUI ControlNet 使用）";
    path.addEventListener("click", () => copyText(item.full || item.thumb, "参考图路径"));
    buttons.append(add, path);
    card.append(buttons);
    return card;
  }

  // ------------------------------------------------------------------ 出图引擎默认值
  function applyWorkflowDefaults(health) {
    const engine = health.workflow_engine || "sd";
    const d = health.workflow_defaults || {};
    const labels = {
      sd: "SD 通用模板",
      "zimage-gguf": "Z-Image-Turbo GGUF（8188 秋叶包）",
      "zimage-int8": "Z-Image-Turbo INT8（8189 cu130）",
    };
    $("engineNote").textContent = `出图引擎：${labels[engine] || engine}`;
    if (d.width && d.height) {
      const value = `${d.width}x${d.height}`;
      const select = $("wfSize");
      let opt = [...select.options].find((o) => o.value === value);
      if (!opt) {
        opt = el("option", "", `${d.width}×${d.height}`);
        opt.value = value;
        select.append(opt);
      }
      select.value = value;
    }
    if (d.steps != null) $("wfSteps").value = d.steps;
    if (d.cfg != null) $("wfCfg").value = d.cfg;
    if (engine.startsWith("zimage")) {
      $("wfCkpt").placeholder = "可选：覆盖 unet 模型文件名";
      $("negativeOut").placeholder = "CFG=1 蒸馏模型，负向词不生效";
    }
  }

  // ------------------------------------------------------------------ 出图归档
  const GALLERY_PAGE = 60;

  function fmtTime(ts) {
    if (!ts) return "—";
    const d = new Date(ts);
    return isNaN(d.getTime()) ? String(ts) : d.toLocaleString("zh-CN", {
      year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    });
  }

  function engineLabel(engine) {
    return ({
      "zimage-int8": "Z-Image INT8", "zimage-gguf": "Z-Image GGUF",
      sd: "SD", unknown: "未知",
    })[engine] || engine || "—";
  }

  function updateGalleryStatus(status) {
    if (!status) return;
    state.gallery.status = status;
    state.gallery.online = status.online;
    const box = $("galleryStatus");
    box.className = "pose-status" + (status.enabled && !status.online ? " off" : "");
    const base = status.enabled
      ? `自动归档已开启 · ComfyUI ${status.online ? "在线" : "不可达（离线时暂停轮询）"} · 共 ${status.items} 条 / ${status.images} 张图`
      : "自动归档未开启：在 compose 里设 GALLERY_ENABLED=true";
    box.textContent = base + (status.last_error ? ` · ${status.last_error}` : "");
  }

  async function loadGallery(reset = true) {
    if (reset) state.gallery.offset = 0;
    try {
      const data = await api(`/api/gallery?limit=${GALLERY_PAGE}&offset=${state.gallery.offset}&q=${encodeURIComponent(state.gallery.query)}`);
      state.gallery.items = reset ? data.items : state.gallery.items.concat(data.items);
      state.gallery.total = data.total;
      state.gallery.offset = state.gallery.items.length;
      updateGalleryStatus(data.status);
      renderGallery();
    } catch (err) {
      $("galleryList").textContent = err.message;
    }
  }

  function renderGallery() {
    const list = $("galleryList");
    list.textContent = "";
    const items = state.gallery.items;
    if (!items.length) {
      list.append(el("div", "pose-status", state.gallery.query
        ? "没有匹配的出图记录" : "还没有归档图片：在 ComfyUI 里出一张图，或点「扫描全部历史」"));
    }
    items.forEach((item) => list.append(renderGalleryCard(item)));
    $("galleryMore").classList.toggle("hidden", items.length >= state.gallery.total);
    $("galleryCount").textContent = `${state.gallery.total} 条`;
  }

  function renderGalleryCard(item) {
    const card = el("div", "gallery-card");
    const first = (item.images || [])[0];
    if (first) {
      const img = document.createElement("img");
      img.src = first.url;
      img.alt = item.positive || item.id;
      img.loading = "lazy";
      img.addEventListener("click", () => openImageModal(item, 0));
      card.append(img);
    }
    card.append(el("div", "gc-title", (item.positive || "（无提示词）").slice(0, 60)));
    card.append(el("div", "gc-meta", `${engineLabel(item.engine)} · ${fmtTime(item.ts)}`));
    const buttons = el("div", "gc-buttons");
    const reuse = el("button", "btn tiny primary", "复用词");
    reuse.title = "把这条正向提示词填回右侧输出框";
    reuse.addEventListener("click", () => reusePrompt(item));
    const open = el("button", "btn tiny ghost", "查看");
    open.addEventListener("click", () => openImageModal(item, 0));
    buttons.append(reuse, open);
    card.append(buttons);
    return card;
  }

  function reusePrompt(item) {
    if (!item.positive) { toast("这条记录没有提示词", true); return; }
    $("positiveOut").value = item.positive;
    $("negativeOut").value = item.negative || "";
    $("positiveOut").scrollIntoView({ behavior: "smooth", block: "center" });
    toast("已把这条提示词填回右侧输出框，可直接复制或继续编辑");
  }

  function openImageModal(item, idx = 0) {
    const images = item.images || [];
    if (!images.length) { toast("这条记录没有图片", true); return; }
    state.gallery.current = item;
    const image = images[Math.min(idx, images.length - 1)];
    $("imageModalImg").src = image.url;
    const meta = $("imageModalMeta");
    meta.textContent = "";
    const rows = [
      ["引擎", engineLabel(item.engine)],
      ["模型", item.model || "—"],
      ["时间", fmtTime(item.ts)],
      ["参数", `${item.width || "?"}×${item.height || "?"} · ${item.steps ?? "?"} 步 · CFG ${item.cfg ?? "?"} · seed ${item.seed ?? "?"}`],
      ["采样", [item.sampler, item.scheduler].filter(Boolean).join(" / ") || "—"],
      ["图片", `${images.length} 张 · ${image.name || ""}`],
    ];
    rows.forEach(([key, value]) => {
      const row = el("div", "im-row");
      row.append(el("span", "im-key", key), el("span", "im-val", String(value)));
      meta.append(row);
    });
    meta.append(el("pre", "im-prompt", item.positive || "（无提示词）"));
    if (item.negative) meta.append(el("pre", "im-prompt", `Negative: ${item.negative}`));
    $("imageModal").classList.remove("hidden");
  }

  function closeImageModal() {
    $("imageModal").classList.add("hidden");
    $("imageModalImg").removeAttribute("src");
    state.gallery.current = null;
  }

  async function deleteCurrentRecord() {
    const item = state.gallery.current;
    if (!item) return;
    if (!window.confirm("删除这条归档记录和它对应的图片文件？此操作不可恢复。")) return;
    try {
      await api(`/api/gallery?id=${encodeURIComponent(item.id)}`, { method: "DELETE" });
      closeImageModal();
      await loadGallery(true);
      toast("已删除该出图记录");
    } catch (err) { toast(err.message, true); }
  }

  async function scanAllHistory() {
    const btn = $("galleryScanAll");
    btn.disabled = true;
    try {
      const data = await api("/api/gallery/scan", { method: "POST", body: {} });
      toast(`扫描完成：新增 ${data.added} 条，跳过 ${data.skipped} 条未出图任务`);
      await loadGallery(true);
    } catch (err) { toast(err.message + (err.hint ? ` — ${err.hint}` : ""), true); }
    finally { btn.disabled = false; }
  }

  async function refreshGallerySilently() {
    if (document.hidden || !$("galleryPanel").open || state.gallery.query) return;
    try {
      const data = await api(`/api/gallery?limit=${GALLERY_PAGE}&offset=0`);
      if (data.total !== state.gallery.total || (data.status && data.status.online !== state.gallery.online)) {
        state.gallery.items = data.items;
        state.gallery.total = data.total;
        state.gallery.offset = data.items.length;
        renderGallery();
      }
      updateGalleryStatus(data.status);
    } catch (_) { /* 后台轮询静默失败，不打扰用户 */ }
  }

  // ------------------------------------------------------------------ 事件
  function bind() {
    ["modeSelect", "modelSelect", "syntaxSelect", "qualityChk", "negativeChk",
      "negCommon", "negPortrait", "negSafety", "extraPositive", "extraNegative"].forEach((id) => {
      $(id).addEventListener("input", scheduleCompose);
      $(id).addEventListener("change", scheduleCompose);
    });

    $("modelSelect").addEventListener("change", () => {
      const model = state.lex.models.find((m) => m.id === $("modelSelect").value);
      if (!model) return;
      $("modeSelect").value = model.default_mode;
      $("negativeChk").checked = model.id !== "flux";
      scheduleCompose();
    });

    $("presetSelect").addEventListener("change", (e) => {
      const preset = state.presets.find((p) => p.id === e.target.value);
      if (preset) applyPreset(preset);
    });
    $("savePresetBtn").addEventListener("click", async () => {
      const name = window.prompt("预设名称", "我的成年人像模板");
      if (!name) return;
      try {
        const payload = composePayload();
        await api("/api/presets", { method: "POST", body: { name, ...payload } });
        const data = await api("/api/presets");
        loadPresets(data.presets);
        $("presetSelect").value = data.presets.find((p) => p.name === name)?.id || "";
        toast(`已保存预设：${name}`);
      } catch (err) { toast(err.message, true); }
    });
    $("deletePresetBtn").addEventListener("click", async () => {
      const id = $("presetSelect").value;
      const preset = state.presets.find((p) => p.id === id);
      if (!preset || preset.builtin) { toast("内置预设不可删除", true); return; }
      try {
        await api(`/api/presets?id=${encodeURIComponent(id)}`, { method: "DELETE" });
        const data = await api("/api/presets");
        loadPresets(data.presets);
        toast("已删除预设");
      } catch (err) { toast(err.message, true); }
    });

    $("pickBtn").addEventListener("click", () => $("fileInput").click());
    $("fileInput").addEventListener("change", (e) => readFile(e.target.files[0]));
    const dz = $("dropZone");
    ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
    ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
    dz.addEventListener("drop", (e) => readFile(e.dataTransfer.files[0]));
    $("extractBtn").addEventListener("click", extractNow);
    $("mergeTagsBtn").addEventListener("click", mergeCheckedTags);
    $("applyLayersBtn").addEventListener("click", applyExtractLayers);
    $("selectAllTags").addEventListener("click", () => {
      state.checked = new Set(normalizeTags(state.extract?.tags).map((t) => t.tag));
      $("tagList").querySelectorAll("input").forEach((cb) => { cb.checked = true; });
    });
    $("unselectAllTags").addEventListener("click", () => {
      state.checked.clear();
      $("tagList").querySelectorAll("input").forEach((cb) => { cb.checked = false; });
    });

    document.querySelectorAll("[data-copy]").forEach((btn) => {
      btn.addEventListener("click", () => copyText($(btn.dataset.copy === "positive" ? "positiveOut" : "negativeOut").value, btn.dataset.copy === "positive" ? "正向提示词" : "负向提示词"));
    });
    $("copyBothBtn").addEventListener("click", () => copyText(
      `Positive:\n${$("positiveOut").value}\n\nNegative:\n${$("negativeOut").value}`, "正 / 负向提示词"));
    $("downloadTxtBtn").addEventListener("click", () => download(
      "comfyui-prompt.txt",
      `# ComfyUI Prompt Studio\n# Positive\n${$("positiveOut").value}\n\n# Negative\n${$("negativeOut").value}\n`,
      "text/plain;charset=utf-8"));
    $("workflowBtn").addEventListener("click", async () => {
      try {
        const data = await getWorkflow();
        download(`comfyui-workflow-${data.engine || "sd"}.json`,
          JSON.stringify(data.workflow, null, 2), "application/json");
        toast(`工作流 JSON 已下载（引擎：${data.engine || "sd"}）`);
      } catch (err) { toast(err.message, true); }
    });
    $("pushBtn").addEventListener("click", async () => {
      const btn = $("pushBtn");
      btn.disabled = true;
      try {
        const data = await getWorkflow();
        const pushed = await api("/api/push", { method: "POST", body: {
          workflow: data.workflow, engine: data.engine, options: data.options,
        } });
        toast(`已推送 ${pushed.engine || ""} · prompt_id=${pushed.response?.prompt_id || "?"}，出图后自动归档`);
        // Z-Image 约 10s/张，出图完成后自动刷新画廊
        setTimeout(() => loadGallery(true), 4000);
        setTimeout(() => loadGallery(true), 14000);
      } catch (err) { toast(err.message + (err.hint ? ` — ${err.hint}` : ""), true); }
      finally { btn.disabled = false; }
    });

    $("galleryJumpBtn").addEventListener("click", () => {
      $("galleryPanel").open = true;
      $("galleryPanel").scrollIntoView({ behavior: "smooth", block: "start" });
      loadGallery(true);
    });
    $("galleryRefresh").addEventListener("click", () => loadGallery(true));
    $("galleryScanAll").addEventListener("click", scanAllHistory);
    $("galleryMore").addEventListener("click", () => loadGallery(false));
    $("gallerySearch").addEventListener("input", () => {
      clearTimeout(state.galleryTimer);
      state.galleryTimer = setTimeout(() => {
        state.gallery.query = $("gallerySearch").value.trim();
        loadGallery(true);
      }, 260);
    });
    $("galleryPanel").addEventListener("toggle", () => {
      if ($("galleryPanel").open) loadGallery(true);
    });
    $("closeImageBtn").addEventListener("click", closeImageModal);
    $("imageModal").addEventListener("click", (e) => {
      if (e.target.id === "imageModal") closeImageModal();
    });
    $("imageModalCopy").addEventListener("click", () =>
      copyText(state.gallery.current?.positive || "", "正向提示词"));
    $("imageModalDownload").addEventListener("click", () => {
      const image = (state.gallery.current?.images || [])[0];
      if (!image) return;
      const a = el("a");
      a.href = image.url;
      a.download = image.name || "comfyui-image.png";
      document.body.append(a);
      a.click();
      a.remove();
    });
    $("imageModalDelete").addEventListener("click", deleteCurrentRecord);

    let poseTimer = null;
    $("poseSearch").addEventListener("input", () => {
      clearTimeout(poseTimer);
      poseTimer = setTimeout(loadPoses, 250);
    });
    $("poseCategory").addEventListener("change", loadPoses);
    $("poseRefresh").addEventListener("click", async () => {
      await loadPoseStatus();
      toast(state.poses && state.poses.available ? "姿势库已刷新" : "姿势库未挂载");
    });
    $("posePanel").addEventListener("toggle", () => {
      if ($("posePanel").open && !state.poses) loadPoseStatus();
    });
    $("poseAdultBtn").addEventListener("click", async () => {
      if (!state.poses || !state.poses.adult_mode) return;
      if (state.poses.adult_unlocked) {
        try {
          const data = await api("/api/poses/adult", {
            method: "POST", body: { confirm: true, ack: "adult", unlock: false },
          });
          state.poses = data.status;
          await loadPoseStatus();
          toast("已回到 SFW 模式");
        } catch (err) { toast(err.message, true); }
        return;
      }
      const agreed = window.confirm(
        "成人模式（18+）\n\n" +
        "· 仅限成年人使用，未成年人请勿解锁\n" +
        "· 只额外显示「全身单人・性感系」人体动态参考；双人缠绵、露骨性行为、器具与未成年相关内容始终不开放\n" +
        "· 图片来自你自行挂载的本地仓库，不上传、不打包进镜像\n" +
        "· 仅允许内网直连解锁，重启容器后自动回到 SFW\n\n确认解锁？"
      );
      if (!agreed) return;
      try {
        const data = await api("/api/poses/adult", {
          method: "POST", body: { confirm: true, ack: "adult", unlock: true },
        });
        state.poses = data.status;
        await loadPoseStatus();
        toast(data.status.adult_unlocked ? "成人模式已解锁" : "未解锁");
      } catch (err) { toast(err.message, true); }
    });

    $("logoutBtn").addEventListener("click", async () => {
      try { await api("/api/auth/logout", { method: "POST", body: {} }); } catch (_) { /* 忽略，直接跳登录页 */ }
      window.location.href = "/login";
    });
    $("guideBtn").addEventListener("click", async () => {
      try {
        const data = await api("/api/guide");
        $("guideContent").innerHTML = renderMarkdown(data.markdown);
        $("guideModal").classList.remove("hidden");
      } catch (err) { toast(err.message, true); }
    });
    $("closeGuideBtn").addEventListener("click", () => $("guideModal").classList.add("hidden"));
    $("guideModal").addEventListener("click", (e) => { if (e.target.id === "guideModal") $("guideModal").classList.add("hidden"); });
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      $("guideModal").classList.add("hidden");
      closeImageModal();
    });
  }

  async function boot() {
    try {
      const [lexData, presetData, health] = await Promise.all([
        api("/api/lexicon"), api("/api/presets"), api("/api/health"),
      ]);
      state.lex = lexData.lexicon;
      buildIndex(state.lex);
      fillControls();
      renderLayers();
      renderBadges(health.engines);
      applyWorkflowDefaults(health);
      loadPresets(presetData.presets);
      bind();
      applyPreset(presetData.presets.find((p) => p.builtin));
      composeNow();
      loadPoseStatus();
      loadGallery(true);
      setInterval(refreshGallerySilently, 15000);
    } catch (err) {
      $("layerPanels").textContent = `初始化失败：${err.message}`;
      toast(err.message, true);
    }
  }

  boot();
})();
