/* AI 角色合成台：独立视图，不改动四层工作台的 app.js。
   三库素材来自只读挂载卷；ComfyUI 的所有请求由服务端代理，浏览器无需 CORS。 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const KINDS = [
    { id: "portrait", label: "人像" },
    { id: "costume", label: "服装" },
    { id: "pose", label: "姿势" },
  ];
  const state = {
    library: null, slots: { portrait: null, costume: null, pose: null },
    extras: [], workflow: null, customWorkflow: null,
    promptId: null, pollTimer: null, defaultView: "studio",
  };

  async function api(path, options = {}) {
    const resp = await fetch(path, {
      method: options.method || "GET",
      headers: { "Content-Type": "application/json" },
      body: options.body ? JSON.stringify(options.body) : undefined,
    });
    let data;
    try { data = await resp.json(); } catch { throw new Error(`服务返回异常 (HTTP ${resp.status})`); }
    if (resp.status === 401) { window.location.href = "/login"; throw new Error("登录已过期"); }
    if (!resp.ok || data.ok === false) {
      const err = new Error(data.error || `请求失败 (HTTP ${resp.status})`);
      err.hint = data.hint || "";
      throw err;
    }
    return data;
  }

  function toast(message) {
    const node = $("stToast");
    node.textContent = message;
    node.classList.remove("hidden");
    clearTimeout(node._timer);
    node._timer = setTimeout(() => node.classList.add("hidden"), 2600);
  }

  function showView(name) {
    const studio = name === "studio";
    $("workbenchView").classList.toggle("hidden", studio);
    $("studioView").classList.toggle("hidden", !studio);
    document.querySelectorAll("[data-st-nav]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.stNav === (studio ? "studio" : "workbench"));
    });
    try { localStorage.setItem("ps_view", name); } catch (_) { /* 忽略隐私模式 */ }
    if (!studio) return;
    if (!state.library) loadLibrary();
    if (state.promptId && state.pollTimer) return;
    if ($("stPositive").value.trim() === "") syncWorkbenchPrompt();
  }

  function syncWorkbenchPrompt() {
    const positive = $("positiveOut");
    const negative = $("negativeOut");
    if (positive && positive.value.trim()) $("stPositive").value = positive.value.trim();
    if (negative && negative.value.trim()) $("stNegative").value = negative.value.trim();
  }

  // ---------------------------------------------------------------- 三库与槽位
  async function loadLibrary() {
    try {
      const data = await api("/api/studio/library");
      state.library = data;
      const counts = data.counts || {};
      $("stCountPose").textContent = counts.pose ?? "—";
      $("stCountCostume").textContent = counts.costume ?? "—";
      $("stCountPortrait").textContent = counts.portrait ?? "—";
      $("stAdultBadge").textContent = data.adult_unlocked ? "18+ 完整模式" : "SFW 安全模式";
      $("stAdultBadge").classList.toggle("sfw", !data.adult_unlocked);
      if (!data.available) {
        $("stResultBox").innerHTML = '<div class="st-placeholder"></div>';
        $("stResultBox").firstChild.textContent = data.reason || "未挂载参考图库";
        renderSlots();
        return;
      }
      if (!state.slots.portrait) await randomAll();
      else renderSlots();
    } catch (err) {
      renderSlots();
      toast(err.message);
    }
  }

  async function pick(kind, same = "", exclude = "") {
    try {
      const data = await api(`/api/studio/pick?kind=${encodeURIComponent(kind)}&same=${encodeURIComponent(same)}&exclude=${encodeURIComponent(exclude)}`);
      state.slots[kind] = data.item;
      renderSlots();
    } catch (err) { toast(err.message); }
  }

  async function randomAll() {
    await Promise.all(KINDS.map((k) => pick(k.id)));
  }

  function renderSlots() {
    const box = $("stSlots");
    box.textContent = "";
    KINDS.forEach((kind) => {
      const item = state.slots[kind.id];
      const card = document.createElement("div");
      card.className = "st-slot";
      const label = document.createElement("div");
      label.className = "st-slot-label";
      label.textContent = kind.label;
      card.append(label);
      const remove = document.createElement("button");
      remove.className = "st-slot-remove";
      remove.textContent = "×";
      remove.title = "移除";
      remove.addEventListener("click", () => { state.slots[kind.id] = null; renderSlots(); });
      card.append(remove);
      if (item) {
        const img = document.createElement("img");
        img.src = item.thumb_url || "";
        img.alt = item.title;
        img.loading = "lazy";
        card.append(img);
        const title = document.createElement("div");
        title.className = "st-slot-title";
        title.textContent = item.title;
        const cat = document.createElement("div");
        cat.className = "st-slot-cat";
        cat.textContent = item.category + (item.adult ? " · 18+" : "");
        card.append(title, cat);
      } else {
        const empty = document.createElement("div");
        empty.className = "st-placeholder";
        empty.textContent = dataUnavailable() ? "未挂载图库" : "点「换一个」抽取";
        card.append(empty);
      }
      const actions = document.createElement("div");
      actions.className = "st-slot-actions";
      [["same", "同类"], ["reroll", "换一个"]].forEach(([act, text]) => {
        const btn = document.createElement("button");
        btn.textContent = text;
        btn.addEventListener("click", () => {
          const current = state.slots[kind.id];
          pick(kind.id, act === "same" && current ? current.category : "",
            act === "reroll" && current ? current.id : "");
        });
        actions.append(btn);
      });
      card.append(actions);
      box.append(card);
    });
    const filled = KINDS.filter((k) => state.slots[k.id]).length;
    $("stRecipeCount").textContent = `角色配方 ${filled}/3`;
  }

  function dataUnavailable() {
    return state.library && state.library.available === false;
  }

  // ---------------------------------------------------------------- 拼图 / 工作流
  async function loadImage(url) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error("参考图加载失败"));
      img.src = url;
    });
  }

  function openModal(title, contentNode) {
    $("stModalTitle").textContent = title;
    const body = $("stModalBody");
    body.textContent = "";
    body.append(contentNode);
    $("stModal").classList.remove("hidden");
  }

  async function showCollage() {
    const items = KINDS.map((k) => state.slots[k.id]).filter((x) => x && x.full_url);
    if (!items.length) { toast("先抽取参考图"); return; }
    try {
      const images = await Promise.all(items.map((x) => loadImage(x.full_url)));
      const canvas = document.createElement("canvas");
      canvas.width = 1200;
      canvas.height = 420;
      const ctx = canvas.getContext("2d");
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      images.forEach((img, i) => {
        const w = 380, h = 380, x = 15 + i * 400, y = 20;
        const scale = Math.min(w / img.width, h / img.height);
        const dw = img.width * scale, dh = img.height * scale;
        ctx.drawImage(img, x + (w - dw) / 2, y + (h - dh) / 2, dw, dh);
        ctx.fillStyle = "#8b8f98";
        ctx.font = "12px sans-serif";
        ctx.fillText(items[i].title.slice(0, 22), x, 412);
      });
      const wrap = document.createElement("div");
      const pic = document.createElement("img");
      pic.src = canvas.toDataURL("image/png");
      wrap.append(pic);
      const row = document.createElement("div");
      row.className = "st-btn-row";
      row.style.marginTop = "10px";
      const dl = document.createElement("button");
      dl.className = "st-btn primary small";
      dl.textContent = "下载拼图 PNG";
      dl.addEventListener("click", () => downloadBlob(canvas.toDataURL("image/png"), "recipe-collage.png"));
      row.append(dl);
      wrap.append(row);
      openModal("参考拼图", wrap);
    } catch (err) { toast(err.message); }
  }

  function downloadBlob(href, name) {
    const a = document.createElement("a");
    a.href = href;
    a.download = name;
    document.body.append(a);
    a.click();
    a.remove();
  }

  async function ensureWorkflow() {
    if ($("stWorkflowSelect").value === "custom" && state.customWorkflow) return state.customWorkflow;
    if (!state.workflow) {
      const data = await api("/api/studio/workflow");
      state.workflow = data.workflow;
    }
    return state.workflow;
  }

  async function showWorkflowEditor() {
    try {
      const wf = await ensureWorkflow();
      const area = document.createElement("textarea");
      area.value = JSON.stringify(wf, null, 2);
      const wrap = document.createElement("div");
      wrap.append(area);
      const row = document.createElement("div");
      row.className = "st-btn-row";
      row.style.marginTop = "10px";
      const save = document.createElement("button");
      save.className = "st-btn primary small";
      save.textContent = "保存为自定义工作流";
      save.addEventListener("click", () => {
        try {
          state.customWorkflow = JSON.parse(area.value);
          $("stWorkflowSelect").value = "custom";
          $("stModal").classList.add("hidden");
          toast("已保存自定义工作流");
        } catch (err) { toast("JSON 解析失败：" + err.message); }
      });
      row.append(save);
      wrap.append(row);
      openModal("编辑工作流 JSON（API 格式）", wrap);
    } catch (err) { toast(err.message); }
  }

  function tuneWorkflow(source) {
    const wf = JSON.parse(JSON.stringify(source));
    const nodes = wf.prompt || wf;
    const [w, h] = $("stSize").value.split("x").map(Number);
    const steps = Math.max(1, Math.min(150, Number($("stSteps").value) || 28));
    const cfg = Math.max(0, Math.min(30, Number($("stCfg").value) || 6.5));
    let seed = Number($("stSeed").value);
    if (!Number.isFinite(seed) || seed < 0) seed = Math.floor(Math.random() * 2 ** 31);
    Object.values(nodes).forEach((node) => {
      if (!node || typeof node !== "object" || !node.inputs) return;
      if (node.class_type === "EmptyLatentImage" || node.class_type === "EmptySD3LatentImage") {
        node.inputs.width = w; node.inputs.height = h;
      }
      if (node.class_type === "KSampler") {
        node.inputs.steps = steps; node.inputs.cfg = cfg; node.inputs.seed = seed;
      }
    });
    return nodes;
  }

  // ---------------------------------------------------------------- 出图闭环
  function setResult(html) { $("stResultBox").innerHTML = html; }

  async function uploadReference(item, index) {
    const resp = await fetch(item.full_url);
    if (!resp.ok) throw new Error("参考图读取失败");
    const blob = await resp.blob();
    const dataUrl = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error("参考图编码失败"));
      reader.readAsDataURL(blob);
    });
    const result = await api("/api/comfy/upload", {
      method: "POST",
      body: { image: dataUrl, name: `studio_${index}_${Date.now()}.png` },
    });
    return result.uploaded;
  }

  async function generate() {
    const btn = $("stGenerateBtn");
    btn.disabled = true;
    clearTimeout(state.pollTimer);
    const style = $("stStyle").value;
    const slotPrompts = KINDS.map((k) => state.slots[k.id] && state.slots[k.id].prompt)
      .filter(Boolean).join(", ");
    let positive = $("stPositive").value.trim();
    if (!positive) syncWorkbenchPrompt(), positive = $("stPositive").value.trim();
    if (slotPrompts) positive = [positive, slotPrompts].filter(Boolean).join(", ");
    if (style) positive = `${style}风格, ${positive}`;
    $("stPositive").value = positive;
    const negative = $("stNegative").value.trim();
    setResult('<div class="st-placeholder">正在准备参考图与工作流…</div>');
    try {
      const source = await ensureWorkflow();
      const workflow = tuneWorkflow(source);
      const uploads = [];
      const items = KINDS.map((k) => state.slots[k.id]).filter((x) => x && x.full_url);
      for (let i = 0; i < Math.min(items.length, 3); i++) {
        setResult(`<div class="st-placeholder">上传参考图 ${i + 1}/${Math.min(items.length, 3)}…</div>`);
        try { uploads.push(await uploadReference(items[i], i)); }
        catch (err) { toast("参考图上传跳过：" + err.message); }
      }
      setResult('<div class="st-placeholder">已提交 ComfyUI，等待出图…</div>');
      const submitted = await api("/api/comfy/submit", {
        method: "POST",
        body: { workflow, positive, negative, images: uploads },
      });
      state.promptId = submitted.prompt_id;
      pollResult(0);
    } catch (err) {
      setResult('<pre></pre>');
      $("stResultBox").querySelector("pre").textContent =
        `生成失败\n\n${err.message}${err.hint ? "\n\n" + err.hint : ""}`;
      btn.disabled = false;
    }
  }

  async function pollResult(attempt) {
    if (!state.promptId) return;
    if (attempt > 120) {
      setResult('<div class="st-placeholder">等待超时，可到「出图归档」查看结果</div>');
      $("stGenerateBtn").disabled = false;
      return;
    }
    try {
      const data = await api("/api/comfy/result?id=" + encodeURIComponent(state.promptId));
      if (data.status === "done") {
        const box = $("stResultBox");
        box.textContent = "";
        (data.images || []).forEach((image) => {
          const img = document.createElement("img");
          img.src = image.url;
          img.alt = "生成结果";
          box.append(img);
          const dl = document.createElement("button");
          dl.className = "st-btn ghost small";
          dl.textContent = "下载原图";
          dl.style.marginTop = "8px";
          dl.addEventListener("click", () => downloadBlob(image.url, image.filename));
          box.append(dl);
        });
        if (!data.images || !data.images.length) {
          setResult('<div class="st-placeholder">任务完成，但没有图片输出</div>');
        }
        $("stGenerateBtn").disabled = false;
        toast("出图完成");
        return;
      }
      if (data.status === "error") {
        setResult('<pre></pre>');
        $("stResultBox").querySelector("pre").textContent = "ComfyUI 执行失败\n\n" + (data.message || "");
        $("stGenerateBtn").disabled = false;
        return;
      }
      setResult(`<div class="st-placeholder">排队 / 执行中…（${attempt + 1}）</div>`);
    } catch (err) {
      setResult('<pre></pre>');
      $("stResultBox").querySelector("pre").textContent = `查询结果失败\n\n${err.message}${err.hint ? "\n\n" + err.hint : ""}`;
      $("stGenerateBtn").disabled = false;
      return;
    }
    state.pollTimer = setTimeout(() => pollResult(attempt + 1), 2000);
  }

  async function checkComfy() {
    const badge = $("stComfyStatus");
    badge.textContent = "检测中…";
    badge.className = "st-conn";
    try {
      const data = await api("/api/comfy/status");
      const info = data.comfy || {};
      if (info.ok) {
        badge.textContent = `ComfyUI 已连接 ${info.version || ""}`.trim();
        badge.className = "st-conn ok";
      } else {
        badge.textContent = "ComfyUI 连接失败";
        badge.className = "st-conn bad";
        setResult('<pre></pre>');
        $("stResultBox").querySelector("pre").textContent = info.error || "连接失败";
      }
    } catch (err) {
      badge.textContent = "ComfyUI 连接失败";
      badge.className = "st-conn bad";
      toast(err.message);
    }
  }

  // ---------------------------------------------------------------- 附加参考图
  function addExtra(dataUrl, label) {
    state.extras.push({ dataUrl, label });
    const list = $("stExtraList");
    const img = document.createElement("img");
    img.src = dataUrl;
    img.title = label;
    list.append(img);
  }

  function readExtraFile(file) {
    if (!file || !file.type.startsWith("image/")) return;
    const reader = new FileReader();
    reader.onload = () => addExtra(reader.result, file.name);
    reader.readAsDataURL(file);
  }

  // ---------------------------------------------------------------- 导航说明
  function showInfo(title, lines) {
    const wrap = document.createElement("div");
    lines.forEach((text) => {
      const p = document.createElement("p");
      p.textContent = text;
      wrap.append(p);
    });
    openModal(title, wrap);
  }

  function handleNav(name) {
    if (name === "studio" || name === "workbench") { showView(name); return; }
    if (name === "poses") {
      showView("workbench");
      const panel = $("posePanel");
      if (panel) { panel.open = true; panel.scrollIntoView({ behavior: "smooth", block: "center" }); }
      return;
    }
    if (name === "costumes" || name === "portraits") {
      showInfo(name === "costumes" ? "服装参考库" : "人像参考库", [
        "该库来自你自行挂载的 ai-pose-gallery-zh 只读卷，镜像与仓库内不含任何上游数据或图片。",
        "在「1. 参考图组合」里点对应槽位的「换一个 / 同类」即可抽取，当前可见条数见左侧导航。",
        "成人向分类需要 POSE_ADULT_MODE=true 并在工作台解锁后才显示。",
      ]);
      return;
    }
    if (name === "api") {
      showInfo("API 接口配置", [
        "接口协议：本地 ComfyUI（服务端代理）。浏览器不直连 ComfyUI，无需 --enable-cors-header。",
        `服务端目标：${(state.comfyUrl || "见 /api/health")}`,
        "相关接口：/api/comfy/status、/api/comfy/upload、/api/comfy/submit、/api/comfy/result、/api/comfy/view。",
        "COMFYUI_URL 支持内嵌 http://user:pass@host:port 供开启 --auth 的 ComfyUI 使用。",
      ]);
      return;
    }
    if (name === "about") {
      showInfo("使用说明与免责声明", [
        "本工作台仅供个人学习、人体动态研究与合法艺术创作使用。",
        "三库图片与原始数据著作权归原作者所有，MIT 协议只覆盖上游源码；不得商用或再分发。",
        "成人模式仅限成年人、仅限内网直连解锁，重启容器自动回到 SFW。",
        "双人缠绵、露骨性行为、BDSM 器具与未成年相关内容在任何模式下都不开放。",
      ]);
    }
  }

  // ---------------------------------------------------------------- 绑定
  function bind() {
    $("studioJumpBtn").addEventListener("click", () => showView("studio"));
    document.querySelectorAll("[data-st-nav]").forEach((btn) => {
      btn.addEventListener("click", () => handleNav(btn.dataset.stNav));
    });
    $("stRandomBtn").addEventListener("click", randomAll);
    $("stRandomRecipe").addEventListener("click", randomAll);
    $("stCollageBtn").addEventListener("click", showCollage);
    $("stComfyCheckBtn").addEventListener("click", checkComfy);
    $("stGenerateBtn").addEventListener("click", generate);
    $("stAdvancedBtn").addEventListener("click", () => {
      const row = $("stAdvancedRow");
      row.classList.toggle("hidden");
    });
    $("stImportBtn").addEventListener("click", () => $("stImportFile").click());
    $("stImportFile").addEventListener("change", (e) => {
      const file = e.target.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => {
        try {
          state.customWorkflow = JSON.parse(reader.result);
          $("stWorkflowSelect").value = "custom";
          toast("工作流已导入");
        } catch (err) { toast("JSON 解析失败：" + err.message); }
      };
      reader.readAsText(file);
    });
    $("stEditBtn").addEventListener("click", showWorkflowEditor);
    $("stExportBtn").addEventListener("click", async () => {
      try {
        const wf = await ensureWorkflow();
        const blob = new Blob([JSON.stringify(wf, null, 2)], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        downloadBlob(url, "comfyui-workflow.json");
        setTimeout(() => URL.revokeObjectURL(url), 2000);
      } catch (err) { toast(err.message); }
    });
    $("stExtraBtn").addEventListener("click", () => $("stExtraFile").click());
    $("stExtraFile").addEventListener("change", (e) => readExtraFile(e.target.files[0]));
    $("stUrlBtn").addEventListener("click", () => {
      const url = $("stImageUrl").value.trim();
      if (url) { addExtra(url, "URL"); $("stImageUrl").value = ""; }
    });
    $("stModalClose").addEventListener("click", () => $("stModal").classList.add("hidden"));
    $("stModal").addEventListener("click", (e) => {
      if (e.target.id === "stModal") $("stModal").classList.add("hidden");
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") $("stModal").classList.add("hidden");
    });
    document.addEventListener("paste", (e) => {
      if ($("studioView").classList.contains("hidden")) return;
      const items = (e.clipboardData || {}).items || [];
      for (const item of items) {
        if (item.type && item.type.startsWith("image/")) { readExtraFile(item.getAsFile()); break; }
      }
    });
  }

  async function boot() {
    bind();
    renderSlots();
    let view = "studio";
    try {
      const saved = localStorage.getItem("ps_view");
      if (saved === "workbench" || saved === "studio") view = saved;
      const health = await api("/api/health");
      state.comfyUrl = health.comfyui_url || "";
      if (!saved && health.default_view) view = health.default_view;
    } catch (err) { toast(err.message); }
    showView(view);
    if (view === "studio") { loadLibrary(); checkComfy(); }
  }

  boot();
})();
