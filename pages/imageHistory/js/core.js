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
  checkinImportFile: null,
  checkinImporting: false,
  checkinImportResult: null,
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
  checkinImportInput: document.getElementById("checkinImportInput"),
  checkinImportMeta: document.getElementById("checkinImportMeta"),
  checkinImportBtn: document.getElementById("checkinImportBtn"),
  checkinImportResult: document.getElementById("checkinImportResult"),
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

function formatUploadBytes(value) {
  const size = Number(value || 0);
  if (size <= 0) return "0 B";
  if (size < 1024) return `${size} B`;
  return formatBytes(size);
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

export {
  bridge,
  state,
  els,
  escapeHtml,
  apiResult,
  thumbUrl,
  blacklistThumbUrl,
  isBlacklistMode,
  activeRecords,
  activeFilteredRecords,
  pixivUrl,
  isSafePixivUrl,
  formatDate,
  formatBytes,
  formatUploadBytes,
  sourceLabel,
  sourceTone,
  sessionType,
  sessionLabel,
  dimensions,
  pageLabel,
  updateMetric,
};
