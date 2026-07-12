import { bridge, state, els, apiResult } from "./core.js";
import {
  renderCheckinImportPanel,
  setBusy,
  renderSourceOptions,
  renderContent,
  showToast,
} from "./render.js";

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
  renderCheckinImportPanel();
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
    renderCheckinImportPanel();
    renderContent();
  }
}

async function importCheckinBackup() {
  if (!state.checkinImportFile || state.checkinImporting) return;
  state.checkinImporting = true;
  state.checkinImportResult = null;
  renderCheckinImportPanel();
  try {
    const result = apiResult(
      await bridge.upload("checkin-import", state.checkinImportFile),
    );
    state.checkinImportResult = { success: true, ...result };
    state.checkinImportFile = null;
    if (els.checkinImportInput) {
      els.checkinImportInput.value = "";
    }
    showToast("签到备份导入成功");
    await reload();
  } catch (error) {
    state.checkinImportResult = {
      success: false,
      error: error.message || "导入失败",
    };
    showToast(state.checkinImportResult.error, "error");
  } finally {
    state.checkinImporting = false;
    renderCheckinImportPanel();
  }
}

export {
  preloadBlacklistThumbnails,
  switchMode,
  reload,
  importCheckinBackup,
};
