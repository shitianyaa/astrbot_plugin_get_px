const bridge = window.AstrBotPluginPage;

const state = {
  context: null,
  records: [],
  filtered: [],
  thumbs: {},
  limit: 0,
  loading: false,
  view: "grid",
  query: "",
  source: "all",
  r18: "all",
  session: "all",
  toastTimer: 0,
  queryTimer: 0,
  lastFocus: null,
};

const els = {
  summary: document.getElementById("summary"),
  refreshBtn: document.getElementById("refreshBtn"),
  clearBtn: document.getElementById("clearBtn"),
  queryInput: document.getElementById("queryInput"),
  sourceSelect: document.getElementById("sourceSelect"),
  r18Select: document.getElementById("r18Select"),
  sessionSelect: document.getElementById("sessionSelect"),
  gridBtn: document.getElementById("gridBtn"),
  listBtn: document.getElementById("listBtn"),
  content: document.getElementById("content"),
  toast: document.getElementById("toast"),
  lightbox: document.getElementById("lightbox"),
  lightboxImage: document.getElementById("lightboxImage"),
  lightboxClose: document.getElementById("lightboxClose"),
  visibleMetric: document.getElementById("visibleMetric"),
  totalMetric: document.getElementById("totalMetric"),
  r18Metric: document.getElementById("r18Metric"),
  groupMetric: document.getElementById("groupMetric"),
  clearDialog: document.getElementById("clearDialog"),
  clearCancelBtn: document.getElementById("clearCancelBtn"),
  clearConfirmBtn: document.getElementById("clearConfirmBtn"),
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function apiResult(result) {
  if (result && result.success === false) {
    throw new Error(result.error || "请求失败");
  }
  return result || {};
}

function thumbUrl(record) {
  return state.thumbs[record.record_id || ""] || "";
}

function pixivUrl(record) {
  const rawUrl = String(record.pixiv_url || "").trim();
  if (rawUrl) return rawUrl;

  const illustId = String(record.illust_id || "").trim();
  return illustId ? `https://www.pixiv.net/artworks/${encodeURIComponent(illustId)}` : "";
}

function isSafePixivUrl(value) {
  try {
    const url = new URL(value);
    return (
      url.protocol === "https:" &&
      (url.hostname === "www.pixiv.net" || url.hostname === "pixiv.net")
    );
  } catch {
    return false;
  }
}

function formatDate(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatBytes(value) {
  const size = Number(value || 0);
  if (!size) return "-";
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(2)} MB`;
}

function sourceLabel(value) {
  if (!value) return "-";
  if (value === "fortune") return "今日运势";
  if (value === "download") return "作品下载";
  if (value.startsWith("search:")) return `搜索: ${value.slice(7) || "-"}`;
  if (value.startsWith("rank:")) return `排行: ${value.slice(5) || "-"}`;
  return value;
}

function sourceTone(value) {
  if (!value) return "neutral";
  if (value === "fortune") return "fortune";
  if (value === "download") return "download";
  if (value.startsWith("rank:")) return "rank";
  if (value.startsWith("search:")) return "search";
  return "neutral";
}

function sessionType(record) {
  return record.group_id ? "group" : "private";
}

function sessionLabel(record) {
  const platform = record.platform || "-";
  if (record.group_id) return `${platform} / 群 ${record.group_id}`;
  if (record.sender_id) return `${platform} / 私聊 ${record.sender_id}`;
  return platform;
}

function dimensions(record) {
  const width = Number(record.width || 0);
  const height = Number(record.height || 0);
  if (!width || !height) return "-";
  return `${width} x ${height}`;
}

function pageLabel(record) {
  const page = Number(record.page || 1);
  const pageCount = Number(record.page_count || 1);
  if (pageCount > 1) return `p${page} / ${pageCount}`;
  return `p${page}`;
}

function updateMetric(element, value) {
  if (!element) return;
  element.textContent = String(value);
}

function setBusy(isBusy) {
  [els.refreshBtn, els.clearBtn, els.clearConfirmBtn].forEach((button) => {
    if (!button) return;
    button.disabled = isBusy;
  });
}

function applyFilters() {
  const query = state.query.trim().toLowerCase();
  state.filtered = state.records.filter((record) => {
    if (state.source !== "all" && record.source !== state.source) return false;
    if (state.r18 === "safe" && Number(record.x_restrict || 0) > 0) return false;
    if (state.r18 === "r18" && Number(record.x_restrict || 0) <= 0) return false;
    if (state.session !== "all" && sessionType(record) !== state.session) return false;
    if (!query) return true;

    const haystack = [
      record.title,
      record.author,
      record.illust_id,
      record.source,
      record.sender_id,
      record.group_id,
      record.session_id,
      ...(record.tags || []),
    ]
      .join(" ")
      .toLowerCase();
    return haystack.includes(query);
  });
}

function renderSourceOptions() {
  const selected = state.source;
  const sources = [...new Set(state.records.map((record) => record.source).filter(Boolean))]
    .sort((a, b) => sourceLabel(a).localeCompare(sourceLabel(b), "zh-CN"));
  els.sourceSelect.innerHTML = [
    '<option value="all">全部</option>',
    ...sources.map(
      (source) =>
        `<option value="${escapeHtml(source)}">${escapeHtml(sourceLabel(source))}</option>`,
    ),
  ].join("");
  els.sourceSelect.value = sources.includes(selected) ? selected : "all";
  state.source = els.sourceSelect.value;
}

function renderSummary() {
  if (state.loading) {
    els.summary.textContent = "正在加载";
    return;
  }
  const total = state.records.length;
  const visible = state.filtered.length;
  const cap = state.limit ? ` / 保留 ${state.limit}` : "";
  const query = state.query.trim();
  els.summary.textContent = query
    ? `筛选到 ${visible} 条，历史共 ${total} 条${cap}`
    : `显示 ${visible} 条，历史共 ${total} 条${cap}`;

  updateMetric(els.visibleMetric, visible);
  updateMetric(els.totalMetric, total);
  updateMetric(
    els.r18Metric,
    state.records.filter((record) => Number(record.x_restrict || 0) > 0).length,
  );
  updateMetric(
    els.groupMetric,
    state.records.filter((record) => sessionType(record) === "group").length,
  );
}

function renderCard(record) {
  const url = thumbUrl(record);
  const targetUrl = pixivUrl(record);
  const source = sourceLabel(record.source);
  const tags = (record.tags || [])
    .slice(0, 6)
    .map((tag) => `<span>${escapeHtml(tag)}</span>`)
    .join("");
  const image = url
    ? `<button class="thumb-button" type="button" data-preview="${escapeHtml(url)}" data-title="${escapeHtml(record.title)}"><img src="${escapeHtml(url)}" alt="${escapeHtml(record.title)}" loading="lazy" /></button>`
    : '<div class="thumb-placeholder"><span>无缩略图</span></div>';
  return `
    <article class="image-card">
      <div class="cover">
        ${image}
        <div class="cover-overlay">
          <span class="source-chip ${escapeHtml(sourceTone(record.source))}">${escapeHtml(source)}</span>
          <span class="page-chip">${escapeHtml(pageLabel(record))}</span>
        </div>
      </div>
      <div class="card-body">
        <div class="card-head">
          <h2><a href="${escapeHtml(targetUrl || "#")}" target="_blank" rel="noreferrer noopener" data-pixiv-url="${escapeHtml(targetUrl)}">${escapeHtml(record.title || "无标题")}</a></h2>
          <span class="badge ${Number(record.x_restrict || 0) > 0 ? "danger" : ""}">${Number(record.x_restrict || 0) > 0 ? "R18" : "全年龄"}</span>
        </div>
        <div class="meta">${escapeHtml(record.author || "未知作者")}</div>
        <dl>
          <div><dt>ID</dt><dd>${escapeHtml(record.illust_id || "-")}</dd></div>
          <div><dt>尺寸</dt><dd>${escapeHtml(dimensions(record))}</dd></div>
          <div><dt>会话</dt><dd>${escapeHtml(sessionLabel(record))}</dd></div>
          <div><dt>时间</dt><dd>${escapeHtml(formatDate(record.sent_at))}</dd></div>
        </dl>
        <div class="tags">${tags}</div>
        <div class="card-actions">
          <button class="open-button" type="button" data-pixiv-url="${escapeHtml(targetUrl)}">打开 Pixiv</button>
        </div>
      </div>
    </article>
  `;
}

function renderListRow(record) {
  const url = thumbUrl(record);
  const targetUrl = pixivUrl(record);
  const image = url
    ? `<button class="row-thumb" type="button" data-preview="${escapeHtml(url)}" data-title="${escapeHtml(record.title)}"><img src="${escapeHtml(url)}" alt="${escapeHtml(record.title)}" loading="lazy" /></button>`
    : '<div class="row-placeholder"></div>';
  return `
    <tr>
      <td data-label="缩略图">${image}</td>
      <td data-label="作品">
        <a class="record-title" href="${escapeHtml(targetUrl || "#")}" target="_blank" rel="noreferrer noopener" data-pixiv-url="${escapeHtml(targetUrl)}"><strong>${escapeHtml(record.title || "无标题")}</strong></a>
        <span>${escapeHtml(record.author || "未知作者")}</span>
      </td>
      <td data-label="ID">${escapeHtml(record.illust_id || "-")}<span>${escapeHtml(pageLabel(record))}</span></td>
      <td data-label="来源">${escapeHtml(sourceLabel(record.source))}</td>
      <td data-label="R18">${Number(record.x_restrict || 0) > 0 ? "R18" : "否"}</td>
      <td data-label="尺寸">${escapeHtml(dimensions(record))}</td>
      <td data-label="文件">${escapeHtml(record.quality || "-")}<br>${escapeHtml(formatBytes(record.file_size))}</td>
      <td data-label="会话">${escapeHtml(sessionLabel(record))}</td>
      <td data-label="时间">${escapeHtml(formatDate(record.sent_at))}</td>
      <td data-label="操作"><button class="link-button" type="button" data-pixiv-url="${escapeHtml(targetUrl)}">打开 Pixiv</button></td>
    </tr>
  `;
}

function copyUrlWithExecCommand(url) {
  const textarea = document.createElement("textarea");
  textarea.value = url;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  textarea.style.top = "0";
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();

  try {
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    textarea.remove();
  }
}

async function copyUrlToClipboard(url) {
  if (copyUrlWithExecCommand(url)) return true;

  if (!navigator.clipboard?.writeText) return false;
  try {
    await navigator.clipboard.writeText(url);
    return true;
  } catch {
    return false;
  }
}

function showBlockedLink(url) {
  window.clearTimeout(state.toastTimer);
  els.toast.className = "toast error link-toast";
  els.toast.innerHTML = `
    <div>浏览器阻止打开新窗口，且当前页面无法自动复制。请手动复制：</div>
    <input type="text" readonly value="${escapeHtml(url)}" />
  `;
  els.toast.hidden = false;
  const input = els.toast.querySelector("input");
  if (input) {
    input.focus();
    input.select();
  }
  state.toastTimer = window.setTimeout(() => {
    els.toast.hidden = true;
    els.toast.innerHTML = "";
  }, 12000);
}

async function openPixivUrl(url) {
  if (!url || !isSafePixivUrl(url)) {
    showToast("Pixiv 链接不可用", "error");
    return;
  }

  let opened = null;
  try {
    opened = window.open(url, "_blank", "noopener,noreferrer");
  } catch {
    opened = null;
  }
  if (opened) return;

  if (await copyUrlToClipboard(url)) {
    showToast("浏览器阻止打开新窗口，已复制 Pixiv 链接");
  } else {
    showBlockedLink(url);
  }
}

function openPreview(trigger) {
  state.lastFocus = document.activeElement;
  els.lightboxImage.src = trigger.dataset.preview || "";
  els.lightboxImage.alt = trigger.dataset.title || "";
  els.lightbox.hidden = false;
  els.lightboxClose.focus();
}

function closeLightbox() {
  els.lightbox.hidden = true;
  els.lightboxImage.removeAttribute("src");
  if (state.lastFocus instanceof HTMLElement) state.lastFocus.focus();
  state.lastFocus = null;
}

function openClearDialog() {
  state.lastFocus = document.activeElement;
  els.clearDialog.hidden = false;
  els.clearCancelBtn.focus();
}

function closeClearDialog() {
  els.clearDialog.hidden = true;
  if (state.lastFocus instanceof HTMLElement) state.lastFocus.focus();
  state.lastFocus = null;
}

function renderContent() {
  applyFilters();
  renderSummary();
  els.gridBtn.classList.toggle("active", state.view === "grid");
  els.listBtn.classList.toggle("active", state.view === "list");

  if (state.loading) {
    els.content.className = "content";
    els.content.innerHTML = `
      <div class="empty loading">
        <span class="spinner" aria-hidden="true"></span>
        <strong>正在同步图片历史</strong>
        <p>读取记录和缩略图中</p>
      </div>
    `;
    return;
  }
  if (!state.filtered.length) {
    els.content.className = "content";
    els.content.innerHTML = `
      <div class="empty">
        <strong>暂无匹配记录</strong>
        <p>调整关键词或筛选条件后再试。</p>
      </div>
    `;
    return;
  }
  if (state.view === "list") {
    els.content.className = "content list";
    els.content.innerHTML = `
      <table>
        <thead>
          <tr>
            <th></th>
            <th>作品</th>
            <th>ID</th>
            <th>来源</th>
            <th>R18</th>
            <th>尺寸</th>
            <th>文件</th>
            <th>会话</th>
            <th>时间</th>
            <th></th>
          </tr>
        </thead>
        <tbody>${state.filtered.map(renderListRow).join("")}</tbody>
      </table>
    `;
  } else {
    els.content.className = "content grid";
    els.content.innerHTML = state.filtered.map(renderCard).join("");
  }
}

function showToast(message, type = "success") {
  window.clearTimeout(state.toastTimer);
  els.toast.innerHTML = "";
  els.toast.textContent = message;
  els.toast.className = `toast ${type}`;
  els.toast.hidden = false;
  state.toastTimer = window.setTimeout(() => {
    els.toast.hidden = true;
  }, 2600);
}

async function preloadThumbnails(records) {
  const thumbs = {};
  const pending = records.filter((record) => record.record_id && record.thumb_id);
  const chunkSize = 8;

  for (let index = 0; index < pending.length; index += chunkSize) {
    const chunk = pending.slice(index, index + chunkSize);
    await Promise.allSettled(
      chunk.map(async (record) => {
      if (!record.record_id || !record.thumb_id) return;
      const result = apiResult(
        await bridge.apiGet("image-history/thumb-data", { id: record.record_id }),
      );
      if (result.data_url) thumbs[record.record_id] = result.data_url;
      }),
    );
    state.thumbs = { ...thumbs };
    renderContent();
  }
}

async function reload() {
  state.loading = true;
  state.thumbs = {};
  setBusy(true);
  renderContent();
  try {
    if (!bridge) throw new Error("AstrBotPluginPage bridge not available");
    state.context = await bridge.ready();
    const result = apiResult(await bridge.apiGet("image-history"));
    state.records = Array.isArray(result.records) ? result.records : [];
    state.limit = Number(result.limit || 0);
    renderSourceOptions();
    state.loading = false;
    renderContent();
    await preloadThumbnails(state.records);
  } catch (error) {
    state.records = [];
    state.thumbs = {};
    showToast(error.message || "加载失败", "error");
  } finally {
    state.loading = false;
    setBusy(false);
    renderContent();
  }
}

async function clearHistory() {
  closeClearDialog();
  state.loading = true;
  setBusy(true);
  renderContent();
  try {
    const result = apiResult(await bridge.apiPost("image-history/clear", {}));
    showToast(`已清空 ${result.deleted || 0} 条`);
    await reload();
  } catch (error) {
    state.loading = false;
    renderContent();
    showToast(error.message || "清空失败", "error");
  } finally {
    setBusy(false);
  }
}

els.refreshBtn.addEventListener("click", reload);
els.clearBtn.addEventListener("click", openClearDialog);
els.queryInput.addEventListener("input", (event) => {
  window.clearTimeout(state.queryTimer);
  state.queryTimer = window.setTimeout(() => {
    state.query = event.target.value;
    renderContent();
  }, 150);
});
els.sourceSelect.addEventListener("change", (event) => {
  state.source = event.target.value;
  renderContent();
});
els.r18Select.addEventListener("change", (event) => {
  state.r18 = event.target.value;
  renderContent();
});
els.sessionSelect.addEventListener("change", (event) => {
  state.session = event.target.value;
  renderContent();
});
els.content.addEventListener("click", (event) => {
  if (!(event.target instanceof Element)) return;

  const preview = event.target.closest("[data-preview]");
  if (preview && els.content.contains(preview)) {
    openPreview(preview);
    return;
  }

  const trigger = event.target.closest("[data-pixiv-url]");
  if (!trigger || !els.content.contains(trigger)) return;
  event.preventDefault();
  openPixivUrl(trigger.dataset.pixivUrl || "");
});
els.gridBtn.addEventListener("click", () => {
  state.view = "grid";
  renderContent();
});
els.listBtn.addEventListener("click", () => {
  state.view = "list";
  renderContent();
});
els.lightbox.addEventListener("click", (event) => {
  if (event.target === els.lightbox) closeLightbox();
});
els.lightboxClose.addEventListener("click", () => {
  closeLightbox();
});
els.clearDialog.addEventListener("click", (event) => {
  if (event.target === els.clearDialog) closeClearDialog();
});
els.clearCancelBtn.addEventListener("click", closeClearDialog);
els.clearConfirmBtn.addEventListener("click", clearHistory);
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (!els.lightbox.hidden) closeLightbox();
  if (!els.clearDialog.hidden) closeClearDialog();
});

reload();
