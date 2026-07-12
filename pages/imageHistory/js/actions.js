import {
  bridge,
  state,
  els,
  escapeHtml,
  apiResult,
  isSafePixivUrl,
} from "./core.js";
import {
  setBusy,
  upsertBlacklistRecord,
  removeRecord,
  removeRecordsByIllustId,
  removeBlacklistRecord,
  showToast,
} from "./render.js";
import { preloadBlacklistThumbnails } from "./data.js";

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


export {
  copyPixivUrl,
  trapFocus,
  openPreview,
  closeLightbox,
  openActionDialog,
  closeActionDialog,
  performPendingAction,
};
