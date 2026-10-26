// 底图库管理：增删改查 + 试排版预览。
//
// 这里刻意不引入框架：页面要求就是「网格 + 几个按钮」，
// 而插件页面运行在沙箱 iframe 里，依赖越少越不容易被宿主环境的变化打崩。
async function initComposePage() {
  const api = window.AstrBotPluginPage;
  await api.ready();
  api.getContext();

  if (window.MemeUI?.applySecureNavLinks) {
    await window.MemeUI.applySecureNavLinks();
  }

  const els = {
    grid: document.getElementById("base-grid"),
    empty: document.getElementById("empty-state"),
    emptyText: document.getElementById("empty-text"),
    statTotal: document.getElementById("stat-total"),
    statUsable: document.getElementById("stat-usable"),
    statCustom: document.getElementById("stat-custom"),
    statDisabled: document.getElementById("stat-disabled"),
    statRow: document.getElementById("stat-row"),
    dropzone: document.getElementById("dropzone"),
    fileInput: document.getElementById("file-input"),
    uploadList: document.getElementById("upload-list"),
    search: document.getElementById("search-input"),
    filterGroup: document.getElementById("filter-group"),
    refresh: document.getElementById("refresh-btn"),
    previewMask: document.getElementById("preview-mask"),
    previewImage: document.getElementById("preview-image"),
    previewPlaceholder: document.getElementById("preview-placeholder"),
    previewBaseName: document.getElementById("preview-base-name"),
    previewText: document.getElementById("preview-text"),
    previewLayout: document.getElementById("preview-layout"),
    previewMaxChars: document.getElementById("preview-max-chars"),
    previewMeta: document.getElementById("preview-meta"),
    previewRun: document.getElementById("preview-run"),
    previewClose: document.getElementById("preview-close"),
    renameMask: document.getElementById("rename-mask"),
    renameInput: document.getElementById("rename-input"),
    renameConfirm: document.getElementById("rename-confirm"),
    renameCancel: document.getElementById("rename-cancel"),
  };

  const state = {
    bases: [],
    stats: { total: 0, usable: 0, custom: 0, disabled: 0 },
    filter: "all",
    query: "",
    thumbs: new Map(),
    previewBaseId: "",
    renameBaseId: "",
    uploading: false,
  };

  const LAYOUT_LABEL = {
    top: "文字在上",
    bottom: "文字在下",
    left: "文字在左（竖排）",
    right: "文字在右（竖排）",
  };

  // ---------------------------------------------------------------- 请求

  async function apiGet(endpoint, params = {}) {
    return await api.apiGet(endpoint, params);
  }

  async function apiPost(endpoint, body = {}) {
    return await api.apiPost(endpoint, body);
  }

  function toast(message, type = "info") {
    if (window.MemeUI?.toast) {
      window.MemeUI.toast(message, type);
      return;
    }
    console[type === "error" ? "error" : "log"](message);
  }

  function errorText(error) {
    const message = String(error?.message || error || "").trim();
    // 桥接层有时把整段 JSON 塞进 message，这里尽量还原成人话
    const jsonStart = message.indexOf("{");
    if (jsonStart >= 0) {
      try {
        const parsed = JSON.parse(message.slice(jsonStart));
        if (parsed?.message) return String(parsed.message);
      } catch {
        /* 不是 JSON，按原文显示 */
      }
    }
    return message || "操作失败";
  }

  // ---------------------------------------------------------------- 数据

  async function load({ silent = false } = {}) {
    try {
      const data = await apiGet("compose/bases");
      state.bases = Array.isArray(data?.bases) ? data.bases : [];
      state.stats = data?.stats || state.stats;
      const maxChars = Number(data?.render?.max_chars);
      if (Number.isInteger(maxChars) && maxChars > 0) {
        els.previewMaxChars.textContent = String(maxChars);
        els.previewText.maxLength = Math.max(16, maxChars);
      }
      renderStats();
      renderGrid();
    } catch (error) {
      if (!silent) {
        toast(`读取底图库失败：${errorText(error)}`, "error");
      }
      console.error("加载底图库失败", error);
      els.grid.replaceChildren();
      els.empty.hidden = false;
      els.emptyText.textContent = `读取失败：${errorText(error)}`;
    }
  }

  function renderStats() {
    const { total, usable, custom, disabled } = state.stats;
    els.statTotal.textContent = String(total ?? 0);
    els.statUsable.textContent = String(usable ?? 0);
    els.statCustom.textContent = String(custom ?? 0);
    els.statDisabled.textContent = String(disabled ?? 0);
  }

  function matches(base) {
    if (state.filter === "usable" && !base.usable) return false;
    if (state.filter === "custom" && !base.custom) return false;
    if (state.filter === "disabled" && !base.disabled) return false;
    const query = state.query.trim().toLowerCase();
    if (!query) return true;
    const haystack = [base.name, base.id, ...(base.characters || [])]
      .join(" ")
      .toLowerCase();
    return haystack.includes(query);
  }

  function setFilter(filter) {
    state.filter = filter;
    els.filterGroup.querySelectorAll("button").forEach((button) => {
      button.setAttribute(
        "aria-pressed",
        String(button.dataset.filter === filter),
      );
    });
    els.statRow.querySelectorAll(".stat-card").forEach((card) => {
      card.setAttribute(
        "aria-pressed",
        String(card.dataset.filter === filter),
      );
    });
    renderGrid();
  }

  // ---------------------------------------------------------------- 渲染

  let thumbObserver = null;

  function thumbNode(base) {
    const holder = document.createElement("div");
    holder.className = "base-thumb";

    const img = document.createElement("img");
    img.alt = base.name;
    img.loading = "lazy";
    img.decoding = "async";

    const cached = state.thumbs.get(base.id);
    if (cached) {
      img.src = cached;
    } else if (!base.exists) {
      const hint = document.createElement("span");
      hint.className = "preview-placeholder";
      hint.textContent = "文件已丢失";
      holder.append(hint);
    } else {
      const skeleton = document.createElement("div");
      skeleton.className = "thumb-skeleton";
      holder.append(skeleton);
      img.dataset.pendingId = base.id;
      observeThumb(img);
    }
    holder.append(img);

    if (base.custom) {
      const badge = document.createElement("span");
      badge.className = "base-badge custom";
      badge.textContent = "自传";
      holder.append(badge);
    }
    if (base.disabled) {
      const badge = document.createElement("span");
      badge.className = "base-badge disabled";
      badge.textContent = "已停用";
      holder.append(badge);
    }
    return holder;
  }

  function observeThumb(img) {
    if (!("IntersectionObserver" in window)) {
      void loadThumb(img);
      return;
    }
    if (!thumbObserver) {
      thumbObserver = new IntersectionObserver(
        (entries) => {
          for (const entry of entries) {
            if (!entry.isIntersecting) continue;
            thumbObserver.unobserve(entry.target);
            void loadThumb(entry.target);
          }
        },
        { rootMargin: "240px 0px" },
      );
    }
    thumbObserver.observe(img);
  }

  async function loadThumb(img) {
    const id = img.dataset.pendingId;
    if (!id) return;
    delete img.dataset.pendingId;
    try {
      const data = await apiGet("compose/base/thumb", { id });
      const url = String(data?.data_url || "");
      if (!url) throw new Error("缩略图内容为空");
      state.thumbs.set(id, url);
      img.src = url;
      img.previousElementSibling?.remove();
    } catch (error) {
      const skeleton = img.previousElementSibling;
      if (skeleton?.classList?.contains("thumb-skeleton")) {
        skeleton.remove();
      }
      const hint = document.createElement("span");
      hint.className = "preview-placeholder";
      hint.textContent = "加载失败";
      img.after(hint);
      console.warn(`缩略图加载失败 ${id}`, error);
    }
  }

  function buildCard(base) {
    const card = document.createElement("article");
    card.className = "base-card";
    if (base.disabled) card.classList.add("is-disabled");
    if (!base.exists) card.classList.add("is-missing");
    card.dataset.id = base.id;

    card.append(thumbNode(base));

    const body = document.createElement("div");
    body.className = "base-body";

    const name = document.createElement("p");
    name.className = "base-name";
    name.textContent = base.name;
    name.title = base.name;
    body.append(name);

    const meta = document.createElement("p");
    meta.className = "base-meta";
    const size = document.createElement("span");
    size.textContent = `${base.width}×${base.height}`;
    const coverage = document.createElement("span");
    coverage.textContent = `主体 ${(Number(base.coverage || 0) * 100).toFixed(0)}%`;
    meta.append(size, coverage);
    if (base.characters?.length) {
      const roles = document.createElement("span");
      roles.textContent = base.characters.join("·");
      meta.append(roles);
    }
    body.append(meta);

    const bar = document.createElement("div");
    bar.className = "coverage-bar";
    bar.title = "主体占画面的比例；越高说明留白越少，配字空间越紧";
    const fill = document.createElement("span");
    fill.style.width = `${Math.min(100, Number(base.coverage || 0) * 100)}%`;
    bar.append(fill);
    body.append(bar);

    card.append(body);

    const actions = document.createElement("div");
    actions.className = "base-actions";

    const preview = document.createElement("button");
    preview.type = "button";
    preview.textContent = "试排版";
    preview.addEventListener("click", () => openPreview(base));
    actions.append(preview);

    const rename = document.createElement("button");
    rename.type = "button";
    rename.textContent = "改名";
    rename.addEventListener("click", () => openRename(base));
    actions.append(rename);

    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.textContent = base.disabled ? "恢复" : "停用";
    toggle.title = base.disabled
      ? "重新加入挑选池"
      : "不参与挑选，文件保留，随时可恢复";
    toggle.addEventListener("click", () => toggleDisabled(base));
    actions.append(toggle);

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "danger";
    remove.textContent = "删除";
    if (!base.custom) {
      remove.disabled = true;
      remove.title = "内置底图不提供删除，可以停用";
    } else {
      remove.addEventListener("click", () => removeBase(base));
    }
    actions.append(remove);

    card.append(actions);
    return card;
  }

  function renderGrid() {
    const visible = state.bases.filter(matches);
    const fragment = document.createDocumentFragment();
    for (const base of visible) {
      fragment.append(buildCard(base));
    }
    els.grid.replaceChildren(fragment);

    const isEmpty = visible.length === 0;
    els.empty.hidden = !isEmpty;
    if (isEmpty) {
      els.emptyText.textContent =
        state.bases.length === 0
          ? "还没有底图。上传一张透明 PNG 就能开始。"
          : "当前筛选条件下没有底图。";
    }
  }

  // ---------------------------------------------------------------- 上传

  function appendUploadRow(name) {
    const item = document.createElement("li");
    item.className = "upload-item";
    const icon = document.createElement("i");
    icon.className = "fas fa-spinner fa-spin";
    icon.setAttribute("aria-hidden", "true");
    const label = document.createElement("span");
    label.className = "name";
    label.textContent = name;
    const status = document.createElement("span");
    status.textContent = "上传中…";
    item.append(icon, label, status);
    els.uploadList.prepend(item);
    return { item, icon, status };
  }

  function isPng(file) {
    const type = String(file.type || "").toLowerCase();
    const name = String(file.name || "").toLowerCase();
    return type === "image/png" || name.endsWith(".png");
  }

  async function uploadFiles(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length || state.uploading) return;

    const pngs = files.filter(isPng);
    const skipped = files.length - pngs.length;
    if (skipped > 0) {
      toast(`已忽略 ${skipped} 个非 PNG 文件`, "warning");
    }
    if (!pngs.length) {
      toast("只支持 PNG 文件", "warning");
      return;
    }

    state.uploading = true;
    els.dropzone.classList.add("is-busy");
    let okCount = 0;
    let failCount = 0;

    for (const file of pngs) {
      const row = appendUploadRow(file.name || "未命名.png");
      try {
        const result = await api.upload("compose/base/upload", file);
        row.item.classList.add("ok");
        row.icon.className = "fas fa-circle-check";
        row.status.textContent = "已加入";
        const message = String(result?.message || "");
        if (message.includes("没有透明背景")) {
          toast(message, "warning");
        }
        okCount += 1;
        if (result?.base?.id) {
          state.thumbs.delete(result.base.id);
        }
      } catch (error) {
        row.item.classList.add("err");
        row.icon.className = "fas fa-circle-exclamation";
        row.status.textContent = errorText(error);
        failCount += 1;
      }
    }

    state.uploading = false;
    els.dropzone.classList.remove("is-busy");
    if (okCount) {
      toast(
        failCount
          ? `已添加 ${okCount} 张，${failCount} 张失败`
          : `已添加 ${okCount} 张底图`,
        failCount ? "warning" : "success",
      );
    } else if (failCount) {
      toast("全部上传失败，请看每一条的原因", "error");
    }
    await load({ silent: true });
  }

  // ---------------------------------------------------------------- 操作

  async function toggleDisabled(base) {
    const next = !base.disabled;
    try {
      await apiPost("compose/base/disable", { id: base.id, disabled: next });
      toast(`${base.name} 已${next ? "停用" : "恢复"}`, "success");
      await load({ silent: true });
    } catch (error) {
      toast(errorText(error), "error");
    }
  }

  async function removeBase(base) {
    const confirmed = window.MemeUI?.confirm
      ? await window.MemeUI.confirm({
          title: "删除底图",
          message: `确定删除「${base.name}」？图片文件会一并移除，无法恢复。`,
          confirmText: "删除",
          danger: true,
        })
      : window.confirm(`确定删除「${base.name}」？`);
    if (!confirmed) return;
    try {
      await apiPost("compose/base/delete", { id: base.id });
      state.thumbs.delete(base.id);
      toast(`已删除 ${base.name}`, "success");
      await load({ silent: true });
    } catch (error) {
      toast(errorText(error), "error");
    }
  }

  // ---------------------------------------------------------------- 重命名

  function openRename(base) {
    state.renameBaseId = base.id;
    els.renameInput.value = base.name;
    els.renameMask.classList.remove("hidden");
    els.renameMask.setAttribute("aria-hidden", "false");
    els.renameInput.focus();
    els.renameInput.select();
  }

  function closeRename() {
    state.renameBaseId = "";
    els.renameMask.classList.add("hidden");
    els.renameMask.setAttribute("aria-hidden", "true");
  }

  async function confirmRename() {
    const name = els.renameInput.value.trim();
    if (!state.renameBaseId || !name) {
      toast("名字不能为空", "warning");
      return;
    }
    try {
      await apiPost("compose/base/rename", {
        id: state.renameBaseId,
        name,
      });
      closeRename();
      toast("已改名", "success");
      await load({ silent: true });
    } catch (error) {
      toast(errorText(error), "error");
    }
  }

  // ---------------------------------------------------------------- 预览

  function openPreview(base) {
    state.previewBaseId = base.id;
    els.previewBaseName.textContent = base.name;
    els.previewImage.classList.remove("is-loaded");
    els.previewImage.removeAttribute("src");
    els.previewPlaceholder.hidden = false;
    els.previewPlaceholder.textContent = "正在生成…";
    els.previewMask.classList.remove("hidden");
    els.previewMask.setAttribute("aria-hidden", "false");
    void runPreview();
  }

  function closePreview() {
    state.previewBaseId = "";
    els.previewMask.classList.add("hidden");
    els.previewMask.setAttribute("aria-hidden", "true");
  }

  async function runPreview() {
    if (!state.previewBaseId) return;
    els.previewRun.disabled = true;
    els.previewMeta.textContent = "渲染中…";
    try {
      const data = await apiPost("compose/preview", {
        base_id: state.previewBaseId,
        text: els.previewText.value,
        layout: els.previewLayout.value,
      });
      const url = String(data?.data_url || "");
      if (!url) throw new Error("预览内容为空");
      els.previewImage.src = url;
      els.previewImage.classList.add("is-loaded");
      els.previewPlaceholder.hidden = true;
      const layout = LAYOUT_LABEL[data.layout] || data.layout || "随机";
      els.previewMeta.textContent = `${layout} · 字体 ${data.font || "自动"} · 「${data.caption || ""}」`;
    } catch (error) {
      els.previewMeta.textContent = errorText(error);
      els.previewImage.classList.remove("is-loaded");
      els.previewPlaceholder.hidden = false;
      els.previewPlaceholder.textContent = "生成失败";
    } finally {
      els.previewRun.disabled = false;
    }
  }

  // ---------------------------------------------------------------- 事件

  els.dropzone.addEventListener("click", () => {
    if (!state.uploading) els.fileInput.click();
  });

  els.dropzone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (!state.uploading) els.fileInput.click();
    }
  });

  els.fileInput.addEventListener("change", () => {
    const files = els.fileInput.files;
    els.fileInput.value = "";
    void uploadFiles(files);
  });

  for (const type of ["dragenter", "dragover"]) {
    els.dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      els.dropzone.classList.add("is-dragging");
    });
  }
  for (const type of ["dragleave", "drop"]) {
    els.dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      els.dropzone.classList.remove("is-dragging");
    });
  }
  els.dropzone.addEventListener("drop", (event) => {
    void uploadFiles(event.dataTransfer?.files);
  });

  els.search.addEventListener("input", () => {
    state.query = els.search.value;
    renderGrid();
  });

  els.filterGroup.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-filter]");
    if (button) setFilter(button.dataset.filter);
  });

  els.statRow.addEventListener("click", (event) => {
    const card = event.target.closest(".stat-card");
    if (card) setFilter(card.dataset.filter);
  });

  els.refresh.addEventListener("click", () => {
    state.thumbs.clear();
    void load();
  });

  els.previewRun.addEventListener("click", () => void runPreview());
  els.previewClose.addEventListener("click", closePreview);
  els.previewMask.addEventListener("click", (event) => {
    if (event.target === els.previewMask) closePreview();
  });
  els.previewText.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      void runPreview();
    }
  });

  els.renameConfirm.addEventListener("click", () => void confirmRename());
  els.renameCancel.addEventListener("click", closeRename);
  els.renameMask.addEventListener("click", (event) => {
    if (event.target === els.renameMask) closeRename();
  });
  els.renameInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      void confirmRename();
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (!els.previewMask.classList.contains("hidden")) closePreview();
    if (!els.renameMask.classList.contains("hidden")) closeRename();
  });

  setFilter("all");
  await load();
}

initComposePage().catch((error) => {
  console.error("底图库页面初始化失败", error);
  if (window.MemeUI?.showPageError) {
    window.MemeUI.showPageError(error);
  }
});
