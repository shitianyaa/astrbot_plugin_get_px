const bridge = window.AstrBotPluginPage;

const state = {
  context: null,
  mode: "history",
  records: [],
  filtered: [],
  thumbs: {},
  blacklistRecords: [],
  blacklistFiltered: [],
  blacklistThumbs: {},
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
  pendingAction: null,
  preloadGeneration: 0,
};

const els = {
  summary: document.getElementById("summary"),
  historyModeBtn: document.getElementById("historyModeBtn"),
  blacklistModeBtn: document.getElementById("blacklistModeBtn"),
  refreshBtn: document.getElementById("refreshBtn"),
  inlineRefreshBtn: document.getElementById("inlineRefreshBtn"),
  queryInput: document.getElementById("queryInput"),
  sourceSelect: document.getElementById("sourceSelect"),
  r18Select: document.getElementById("r18Select"),
  sessionSelect: document.getElementById("sessionSelect"),
  gridBtn: document.getElementById("gridBtn"),
  listBtn: document.getElementById("listBtn"),
  viewToggle: document.querySelector(".view-toggle"),
  content: document.getElementById("content"),
  toast: document.getElementById("toast"),
  lightbox: document.getElementById("lightbox"),
  lightboxImage: document.getElementById("lightboxImage"),
  lightboxClose: document.getElementById("lightboxClose"),
  visibleMetric: document.getElementById("visibleMetric"),
  totalMetric: document.getElementById("totalMetric"),
  r18Metric: document.getElementById("r18Metric"),
  groupMetric: document.getElementById("groupMetric"),
  visibleMetricLabel: document.getElementById("visibleMetricLabel"),
  totalMetricLabel: document.getElementById("totalMetricLabel"),
  r18MetricLabel: document.getElementById("r18MetricLabel"),
  groupMetricLabel: document.getElementById("groupMetricLabel"),
  actionDialog: document.getElementById("actionDialog"),
  actionDialogTitle: document.getElementById("actionDialogTitle"),
  actionDialogDesc: document.getElementById("actionDialogDesc"),
  actionCancelBtn: document.getElementById("actionCancelBtn"),
  actionConfirmBtn: document.getElementById("actionConfirmBtn"),
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

function blacklistThumbUrl(record) {
  return state.blacklistThumbs[record.illust_id || ""] || "";
}

function isBlacklistMode() {
  return state.mode === "blacklist";
}

function activeRecords() {
  return isBlacklistMode() ? state.blacklistRecords : state.records;
}

function activeFilteredRecords() {
  return isBlacklistMode() ? state.blacklistFiltered : state.filtered;
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
  if (value === "checkin") return "签到背景";
  if (value === "download") return "作品下载";
  if (value.startsWith("search:")) return `搜索: ${value.slice(7) || "-"}`;
  if (value.startsWith("rank:")) return `排行: ${value.slice(5) || "-"}`;
  return value;
}

function sourceTone(value) {
  if (!value) return "neutral";
  if (value === "checkin") return "checkin";
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
  [els.refreshBtn, els.actionConfirmBtn, els.historyModeBtn, els.blacklistModeBtn].forEach((button) => {
    if (!button) return;
    button.disabled = isBusy;
  });
  document.querySelectorAll(".icon-button").forEach((button) => {
    button.disabled = isBusy;
  });
}

function iconSvg(name) {
  const icons = {
    copy: '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"></rect><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"></path>',
    trash: '<path d="M3 6h18"></path><path d="M8 6V4c0-1 .8-2 2-2h4c1.2 0 2 .8 2 2v2"></path><path d="M19 6l-1 14c-.1 1.1-.9 2-2 2H8c-1.1 0-1.9-.9-2-2L5 6"></path><path d="M10 11v6"></path><path d="M14 11v6"></path>',
    ban: '<circle cx="12" cy="12" r="9"></circle><path d="M5.6 5.6l12.8 12.8"></path>',
    undo: '<path d="M9 14 4 9l5-5"></path><path d="M4 9h10a6 6 0 0 1 0 12h-2"></path>',
    image: '<rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect><circle cx="8.5" cy="8.5" r="1.5"></circle><path d="M21 15l-5-5L5 21"></path>',
  };
  return `<svg viewBox="0 0 24 24" aria-hidden="true">${icons[name] || ""}</svg>`;
}

function iconButton(label, icon, attrs = "", extraClass = "", disabled = false) {
  return `
    <button
      class="icon-button ${extraClass}"
      type="button"
      aria-label="${escapeHtml(label)}"
      title="${escapeHtml(label)}"
      ${attrs}
      ${disabled ? "disabled" : ""}
    >${iconSvg(icon)}</button>
  `;
}

function findRecord(recordId) {
  return state.records.find((record) => String(record.record_id || "") === recordId);
}

function upsertBlacklistRecord(record) {
  if (!record || !record.illust_id) return;
  const illustId = String(record.illust_id || "");
  state.blacklistRecords = [
    record,
    ...state.blacklistRecords.filter(
      (item) => String(item.illust_id || "") !== illustId,
    ),
  ];
}

function removeRecord(recordId) {
  state.records = state.records.filter(
    (record) => String(record.record_id || "") !== recordId,
  );
  delete state.thumbs[recordId];
  renderSourceOptions();
  renderContent();
}

function removeRecordsByIllustId(illustId) {
  const removedIds = new Set();
  state.records = state.records.filter((record) => {
    if (String(record.illust_id || "") !== illustId) return true;
    removedIds.add(String(record.record_id || ""));
    return false;
  });
  removedIds.forEach((recordId) => {
    delete state.thumbs[recordId];
  });
  renderSourceOptions();
  renderContent();
}

function removeBlacklistRecord(illustId) {
  state.blacklistRecords = state.blacklistRecords.filter(
    (record) => String(record.illust_id || "") !== illustId,
  );
  delete state.blacklistThumbs[illustId];
  renderSourceOptions();
  renderContent();
}

function matchesQuery(record, query) {
  if (!query) return true;
  const haystack = [
    record.title,
    record.author,
    record.illust_id,
    record.source,
    record.record_id,
    record.sender_id,
    record.group_id,
    record.session_id,
    record.added_at,
    ...(record.tags || []),
  ]
    .join(" ")
    .toLowerCase();
  return haystack.includes(query);
}

function applyFilters() {
  const query = state.query.trim().toLowerCase();
  state.filtered = state.records.filter((record) => {
    if (state.source !== "all" && record.source !== state.source) return false;
    if (state.r18 === "safe" && Number(record.x_restrict || 0) > 0) return false;
    if (state.r18 === "r18" && Number(record.x_restrict || 0) <= 0) return false;
    if (state.session !== "all" && sessionType(record) !== state.session) return false;
    return matchesQuery(record, query);
  });
  state.blacklistFiltered = state.blacklistRecords.filter((record) => {
    if (state.source !== "all" && record.source !== state.source) return false;
    return matchesQuery(record, query);
  });
}

function renderSourceOptions() {
  const selected = state.source;
  const sources = [...new Set(activeRecords().map((record) => record.source).filter(Boolean))]
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

function clearHistoryOnlyFilters() {
  state.r18 = "all";
  state.session = "all";
  els.r18Select.value = "all";
  els.sessionSelect.value = "all";
}

function renderModeControls() {
  const blacklistMode = isBlacklistMode();
  els.historyModeBtn.classList.toggle("active", !blacklistMode);
  els.blacklistModeBtn.classList.toggle("active", blacklistMode);
  els.historyModeBtn.setAttribute("aria-pressed", String(!blacklistMode));
  els.blacklistModeBtn.setAttribute("aria-pressed", String(blacklistMode));

  if (blacklistMode) clearHistoryOnlyFilters();
  els.queryInput.placeholder = blacklistMode
    ? "标题、作者、ID、来源"
    : "标题、作者、ID、标签、会话";

  const r18Field = els.r18Select.closest(".field");
  const sessionField = els.sessionSelect.closest(".field");
  if (r18Field) r18Field.hidden = blacklistMode;
  if (sessionField) sessionField.hidden = blacklistMode;
  els.viewToggle.hidden = blacklistMode;
}

function renderSummary() {
  if (state.loading) {
    els.summary.textContent = isBlacklistMode() ? "正在加载黑名单" : "正在加载";
    return;
  }
  const records = activeRecords();
  const filtered = activeFilteredRecords();
  const total = records.length;
  const visible = filtered.length;
  const cap = !isBlacklistMode() && state.limit ? ` / 保留 ${state.limit}` : "";
  const query = state.query.trim();
  if (isBlacklistMode()) {
    els.summary.textContent = query
      ? `筛选到 ${visible} 条，黑名单共 ${total} 条`
      : `显示 ${visible} 条，黑名单共 ${total} 条`;
    els.visibleMetricLabel.textContent = "当前显示";
    els.totalMetricLabel.textContent = "黑名单总数";
    els.r18MetricLabel.textContent = "有缩略图";
    els.groupMetricLabel.textContent = "来源数";
    updateMetric(els.visibleMetric, visible);
    updateMetric(els.totalMetric, total);
    updateMetric(els.r18Metric, records.filter((record) => record.thumb_id).length);
    updateMetric(
      els.groupMetric,
      new Set(records.map((record) => record.source).filter(Boolean)).size,
    );
    return;
  }
  els.summary.textContent = query
    ? `筛选到 ${visible} 条，历史共 ${total} 条${cap}`
    : `显示 ${visible} 条，历史共 ${total} 条${cap}`;
  els.visibleMetricLabel.textContent = "当前显示";
  els.totalMetricLabel.textContent = "历史总数";
  els.r18MetricLabel.textContent = "R18";
  els.groupMetricLabel.textContent = "群聊记录";

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

function renderCard(record, index = 0) {
  const url = thumbUrl(record);
  const targetUrl = pixivUrl(record);
  const recordId = String(record.record_id || "");
  const illustId = String(record.illust_id || "");
  const source = sourceLabel(record.source);
  const tags = (record.tags || [])
    .slice(0, 6)
    .map((tag) => `<span>${escapeHtml(tag)}</span>`)
    .join("");
  const image = url
    ? `<button class="thumb-button" type="button" data-preview="${escapeHtml(url)}" data-title="${escapeHtml(record.title)}"><img src="${escapeHtml(url)}" alt="${escapeHtml(record.title)}" loading="lazy" /></button>`
    : `<div class="thumb-placeholder">${iconSvg("image")}<span>无缩略图</span></div>`;
  return `
    <article class="image-card" style="--i: ${Math.min(index, 12)}">
      <div class="cover">
        ${image}
        <div class="cover-overlay">
          <span class="source-chip ${escapeHtml(sourceTone(record.source))}">${escapeHtml(source)}</span>
          <span class="page-chip">${escapeHtml(pageLabel(record))}</span>
        </div>
      </div>
      <div class="card-body">
        <div class="card-head">
          <h2>${escapeHtml(record.title || "无标题")}</h2>
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
        <div class="card-actions" aria-label="图片操作">
          ${iconButton("复制 Pixiv 链接", "copy", `data-copy-url="${escapeHtml(targetUrl)}"`, "primary", !targetUrl)}
          ${iconButton("加入黑名单", "ban", `data-blacklist-record="${escapeHtml(recordId)}" data-illust-id="${escapeHtml(illustId)}"`, "", !illustId)}
          ${iconButton("删除这条记录", "trash", `data-delete-record="${escapeHtml(recordId)}"`, "danger", !recordId)}
        </div>
      </div>
    </article>
  `;
}

function renderListRow(record, index = 0) {
  const url = thumbUrl(record);
  const targetUrl = pixivUrl(record);
  const recordId = String(record.record_id || "");
  const illustId = String(record.illust_id || "");
  const image = url
    ? `<button class="row-thumb" type="button" data-preview="${escapeHtml(url)}" data-title="${escapeHtml(record.title)}"><img src="${escapeHtml(url)}" alt="${escapeHtml(record.title)}" loading="lazy" /></button>`
    : `<div class="row-placeholder">${iconSvg("image")}</div>`;
  return `
    <tr style="--i: ${Math.min(index, 12)}">
      <td data-label="缩略图">${image}</td>
      <td data-label="作品">
        <span class="record-title"><strong>${escapeHtml(record.title || "无标题")}</strong></span>
        <span>${escapeHtml(record.author || "未知作者")}</span>
      </td>
      <td data-label="ID">${escapeHtml(record.illust_id || "-")}<span>${escapeHtml(pageLabel(record))}</span></td>
      <td data-label="来源">${escapeHtml(sourceLabel(record.source))}</td>
      <td data-label="R18">${Number(record.x_restrict || 0) > 0 ? "R18" : "否"}</td>
      <td data-label="尺寸">${escapeHtml(dimensions(record))}</td>
      <td data-label="文件">${escapeHtml(record.quality || "-")}<br>${escapeHtml(formatBytes(record.file_size))}</td>
      <td data-label="会话">${escapeHtml(sessionLabel(record))}</td>
      <td data-label="时间">${escapeHtml(formatDate(record.sent_at))}</td>
      <td data-label="操作">
        <div class="row-actions">
          ${iconButton("复制 Pixiv 链接", "copy", `data-copy-url="${escapeHtml(targetUrl)}"`, "primary", !targetUrl)}
          ${iconButton("加入黑名单", "ban", `data-blacklist-record="${escapeHtml(recordId)}" data-illust-id="${escapeHtml(illustId)}"`, "", !illustId)}
          ${iconButton("删除这条记录", "trash", `data-delete-record="${escapeHtml(recordId)}"`, "danger", !recordId)}
        </div>
      </td>
    </tr>
  `;
}

function renderBlacklistRow(record, index = 0) {
  const url = blacklistThumbUrl(record);
  const targetUrl = pixivUrl(record);
  const illustId = String(record.illust_id || "");
  const image = url
    ? `<button class="row-thumb blacklist-thumb" type="button" data-preview="${escapeHtml(url)}" data-title="${escapeHtml(record.title)}"><img src="${escapeHtml(url)}" alt="${escapeHtml(record.title)}" loading="lazy" /></button>`
    : `<div class="row-placeholder blacklist-placeholder">${iconSvg("image")}</div>`;
  return `
    <tr style="--i: ${Math.min(index, 12)}">
      <td data-label="缩略图">${image}</td>
      <td data-label="作品">
        <span class="record-title"><strong>${escapeHtml(record.title || "无标题")}</strong></span>
        <span>${escapeHtml(record.author || "未知作者")}</span>
      </td>
      <td data-label="ID">${escapeHtml(illustId || "-")}</td>
      <td data-label="来源">${escapeHtml(sourceLabel(record.source))}</td>
      <td data-label="拉黑时间">${escapeHtml(formatDate(record.added_at))}</td>
      <td data-label="操作">
        <div class="row-actions">
          ${iconButton("复制 Pixiv 链接", "copy", `data-copy-url="${escapeHtml(targetUrl)}"`, "primary", !targetUrl)}
          ${iconButton("移出黑名单", "undo", `data-remove-blacklist="${escapeHtml(illustId)}"`, "danger", !illustId)}
        </div>
      </td>
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
  // Try modern Clipboard API first
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(url);
      return true;
    } catch {
      // Clipboard API failed, fallback to execCommand
    }
  }

  // Fallback for older browsers or insecure contexts
  return copyUrlWithExecCommand(url);
}

function showBlockedLink(url) {
  window.clearTimeout(state.toastTimer);
  els.toast.className = "toast error link-toast";
  els.toast.innerHTML = `
    <div>当前页面无法自动复制。请手动复制：</div>
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

async function copyPixivUrl(url) {
  if (!url || !isSafePixivUrl(url)) {
    showToast("Pixiv 链接不可用", "error");
    return;
  }

  if (await copyUrlToClipboard(url)) {
    showToast("已复制 Pixiv 链接");
  } else {
    showBlockedLink(url);
  }
}

const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

// Keep Tab focus inside an open modal. Cycles between the first and last
// focusable element, and pulls focus back if it has escaped the container.
function trapFocus(container, event) {
  const focusable = [...container.querySelectorAll(FOCUSABLE_SELECTOR)];
  if (!focusable.length) {
    event.preventDefault();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const active = document.activeElement;
  if (event.shiftKey) {
    if (active === first || !container.contains(active)) {
      event.preventDefault();
      last.focus();
    }
  } else if (active === last || !container.contains(active)) {
    event.preventDefault();
    first.focus();
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

function openActionDialog(action) {
  state.lastFocus = document.activeElement;
  state.pendingAction = action;
  if (action.type === "blacklist") {
    els.actionDialogTitle.textContent = "加入黑名单？";
    els.actionDialogDesc.textContent = `作品 ID ${action.illustId} 会加入黑名单，并从历史中移除同 ID 记录。之后搜索、下载和签到背景会避开它。`;
    els.actionConfirmBtn.textContent = "加入黑名单";
  } else if (action.type === "remove-blacklist") {
    els.actionDialogTitle.textContent = "移出黑名单？";
    els.actionDialogDesc.textContent = `作品 ID ${action.illustId} 会从作品黑名单中移除。历史记录不会恢复。`;
    els.actionConfirmBtn.textContent = "移出黑名单";
  } else {
    els.actionDialogTitle.textContent = "删除这条图片记录？";
    els.actionDialogDesc.textContent = "这会删除当前历史记录和本地缩略图缓存，不会影响 Pixiv 原作品。";
    els.actionConfirmBtn.textContent = "删除记录";
  }
  els.actionDialog.hidden = false;
  els.actionCancelBtn.focus();
}

function closeActionDialog() {
  els.actionDialog.hidden = true;
  state.pendingAction = null;
  if (state.lastFocus instanceof HTMLElement) state.lastFocus.focus();
  state.lastFocus = null;
}

async function performPendingAction() {
  const action = state.pendingAction;
  if (!action) return;
  closeActionDialog();
  setBusy(true);
  try {
    if (action.type === "blacklist") {
      const result = apiResult(
        await bridge.apiPost("image-history/blacklist", {
          record_id: action.recordId,
          illust_id: action.illustId,
        }),
      );
      removeRecordsByIllustId(action.illustId);
      if (result.record) {
        upsertBlacklistRecord(result.record);
        await preloadBlacklistThumbnails([result.record]);
      }
      const deleted = Number(result.deleted || 0);
      showToast(deleted ? `已加入黑名单，移除 ${deleted} 条历史记录` : "已加入黑名单");
      return;
    }

    if (action.type === "remove-blacklist") {
      apiResult(
        await bridge.apiPost("image-blacklist/remove", {
          illust_id: action.illustId,
        }),
      );
      removeBlacklistRecord(action.illustId);
      showToast("已移出黑名单");
      return;
    }

    apiResult(
      await bridge.apiPost("image-history/delete", {
        record_id: action.recordId,
      }),
    );
    removeRecord(action.recordId);
    showToast("已删除图片记录");
  } catch (error) {
    showToast(error.message || "操作失败", "error");
  } finally {
    setBusy(false);
  }
}

function renderContent() {
  renderModeControls();
  applyFilters();
  renderSummary();
  els.gridBtn.classList.toggle("active", state.view === "grid");
  els.listBtn.classList.toggle("active", state.view === "list");
  const filtered = activeFilteredRecords();

  if (state.loading) {
    els.content.className = "content";
    els.content.innerHTML = `
      <div class="empty loading">
        <span class="spinner" aria-hidden="true"></span>
        <strong>${isBlacklistMode() ? "正在同步黑名单" : "正在同步图片历史"}</strong>
        <p>读取记录和缩略图中</p>
      </div>
    `;
    return;
  }
  if (!filtered.length) {
    els.content.className = "content";
    els.content.innerHTML = `
      <div class="empty">
        ${iconSvg("image")}
        <strong>暂无匹配记录</strong>
        <p>调整关键词或筛选条件后再试。</p>
      </div>
    `;
    return;
  }
  if (isBlacklistMode()) {
    els.content.className = "content list blacklist-list";
    els.content.innerHTML = `
      <table>
        <thead>
          <tr>
            <th></th>
            <th>作品</th>
            <th>ID</th>
            <th>来源</th>
            <th>拉黑时间</th>
            <th></th>
          </tr>
        </thead>
        <tbody>${filtered.map(renderBlacklistRow).join("")}</tbody>
      </table>
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
        <tbody>${filtered.map(renderListRow).join("")}</tbody>
      </table>
    `;
  } else {
    els.content.className = "content grid";
    els.content.innerHTML = filtered.map(renderCard).join("");
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


const THUMB_CHUNK_SIZE = 32;

// Shared thumbnail preloader for both history and blacklist views. Loads in
// chunks, seeds from any already-cached thumbs (skipping those), and bails out
// if the active preload generation changes (mode switch / refresh) to avoid
// writing stale results over fresh state.
async function preloadThumbs({ records, idKey, endpoint, seed, apply }) {
  const generation = state.preloadGeneration;
  const thumbs = { ...seed };
  const pending = records.filter(
    (record) => record[idKey] && record.thumb_id && !thumbs[record[idKey]],
  );

  for (let index = 0; index < pending.length; index += THUMB_CHUNK_SIZE) {
    const chunk = pending.slice(index, index + THUMB_CHUNK_SIZE);
    const ids = chunk.map((record) => record[idKey]).filter(Boolean);
    const result = apiResult(await bridge.apiPost(endpoint, { ids }));
    Object.assign(thumbs, result.thumbs || {});
    if (state.preloadGeneration !== generation) return;
    apply({ ...thumbs });
    renderContent();
  }
}

function preloadThumbnails(records) {
  return preloadThumbs({
    records,
    idKey: "record_id",
    endpoint: "image-history/thumb-data-batch",
    seed: {},
    apply: (thumbs) => {
      state.thumbs = thumbs;
    },
  });
}

function preloadBlacklistThumbnails(records) {
  return preloadThumbs({
    records,
    idKey: "illust_id",
    endpoint: "image-blacklist/thumb-data-batch",
    seed: state.blacklistThumbs,
    apply: (thumbs) => {
      state.blacklistThumbs = thumbs;
    },
  });
}
function switchMode(mode) {
  if (state.mode === mode) return;
  state.mode = mode;
  state.preloadGeneration += 1;
  state.source = "all";
  renderSourceOptions();
  renderContent();
}

async function loadFontConfig() {
  try {
    const result = await bridge.apiGet("config");
    if (result && result.success && result.font_url) {
      const link = document.createElement("link");
      link.rel = "stylesheet";
      link.href = result.font_url;
      document.head.appendChild(link);
    }
  } catch {
    // Font config not available, use system fonts
  }
}
async function reload() {
  state.loading = true;
  state.preloadGeneration += 1;
  state.thumbs = {};
  state.blacklistThumbs = {};
  setBusy(true);
  renderContent();
  try {
    if (!bridge) throw new Error("AstrBotPluginPage bridge not available");
    state.context = await bridge.ready();

    // Load font config in parallel with data
    const [, historyResult, blacklistResult] = await Promise.all([
      loadFontConfig(),
      bridge.apiGet("image-history"),
      bridge.apiGet("image-blacklist"),
    ]);
    const history = apiResult(historyResult);
    const blacklist = apiResult(blacklistResult);
    state.records = Array.isArray(history.records) ? history.records : [];
    state.blacklistRecords = Array.isArray(blacklist.records) ? blacklist.records : [];
    state.limit = Number(history.limit || 0);
    renderSourceOptions();
    state.loading = false;
    renderContent();
    await Promise.allSettled([
      preloadThumbnails(state.records),
      preloadBlacklistThumbnails(state.blacklistRecords),
    ]);
  } catch (error) {
    state.records = [];
    state.blacklistRecords = [];
    state.thumbs = {};
    state.blacklistThumbs = {};
    showToast(error.message || "加载失败", "error");
  } finally {
    state.loading = false;
    setBusy(false);
    renderContent();
  }
}
els.refreshBtn.addEventListener("click", reload);
els.inlineRefreshBtn.addEventListener("click", reload);
els.historyModeBtn.addEventListener("click", () => {
  switchMode("history");
});
els.blacklistModeBtn.addEventListener("click", () => {
  switchMode("blacklist");
});
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

  const copyTrigger = event.target.closest("[data-copy-url]");
  if (copyTrigger && els.content.contains(copyTrigger)) {
    event.preventDefault();
    copyPixivUrl(copyTrigger.dataset.copyUrl || "");
    return;
  }

  const removeBlacklistTrigger = event.target.closest("[data-remove-blacklist]");
  if (removeBlacklistTrigger && els.content.contains(removeBlacklistTrigger)) {
    event.preventDefault();
    openActionDialog({
      type: "remove-blacklist",
      illustId: removeBlacklistTrigger.dataset.removeBlacklist || "",
    });
    return;
  }

  const deleteTrigger = event.target.closest("[data-delete-record]");
  if (deleteTrigger && els.content.contains(deleteTrigger)) {
    event.preventDefault();
    openActionDialog({
      type: "delete",
      recordId: deleteTrigger.dataset.deleteRecord || "",
    });
    return;
  }

  const blacklistTrigger = event.target.closest("[data-blacklist-record]");
  if (blacklistTrigger && els.content.contains(blacklistTrigger)) {
    event.preventDefault();
    const recordId = blacklistTrigger.dataset.blacklistRecord || "";
    const record = findRecord(recordId);
    openActionDialog({
      type: "blacklist",
      recordId,
      illustId: blacklistTrigger.dataset.illustId || String(record?.illust_id || ""),
    });
  }
});
els.gridBtn.addEventListener("click", () => {
  if (isBlacklistMode()) return;
  state.view = "grid";
  renderContent();
});
els.listBtn.addEventListener("click", () => {
  if (isBlacklistMode()) return;
  state.view = "list";
  renderContent();
});
els.lightbox.addEventListener("click", (event) => {
  if (event.target === els.lightbox) closeLightbox();
});
els.lightboxClose.addEventListener("click", () => {
  closeLightbox();
});
els.actionDialog.addEventListener("click", (event) => {
  if (event.target === els.actionDialog) closeActionDialog();
});
els.actionCancelBtn.addEventListener("click", closeActionDialog);
els.actionConfirmBtn.addEventListener("click", performPendingAction);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    if (!els.lightbox.hidden) closeLightbox();
    if (!els.actionDialog.hidden) closeActionDialog();
    return;
  }
  if (event.key === "Tab") {
    if (!els.lightbox.hidden) trapFocus(els.lightbox, event);
    else if (!els.actionDialog.hidden) trapFocus(els.actionDialog, event);
  }
});

reload();
