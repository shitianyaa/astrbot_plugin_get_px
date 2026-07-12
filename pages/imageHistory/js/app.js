import { state, els, isBlacklistMode } from "./core.js";
import { findRecord, renderContent, renderCheckinImportPanel } from "./render.js";
import {
  copyPixivUrl,
  trapFocus,
  openPreview,
  closeLightbox,
  openActionDialog,
  closeActionDialog,
  performPendingAction,
} from "./actions.js";
import {
  switchMode,
  reload,
  importCheckinBackup,
} from "./data.js";

if (els.checkinImportInput) {
  els.checkinImportInput.addEventListener("change", (event) => {
    const files = event.target?.files;
    state.checkinImportFile = files && files.length ? files[0] : null;
    state.checkinImportResult = null;
    renderCheckinImportPanel();
  });
}

if (els.checkinImportBtn) {
  els.checkinImportBtn.addEventListener("click", importCheckinBackup);
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
