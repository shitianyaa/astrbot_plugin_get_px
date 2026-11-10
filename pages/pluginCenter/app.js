import * as settingsModule from "./settings.mjs";
import { mountSafety } from "./safety.mjs";
import { createSelect } from "./ui.mjs";
const bridge = window.AstrBotPluginPage;

const state = {
  overview: {},
  groups: [],
  selectedGroup: "",
  rankingType: "today",
  trendDays: 7,
  safety: { global_safety_terms: [], safety_groups: [] },
  blacklist: [],
  thumbs: {},
  members: [],
  memberQuery: "",
  memberTotal: 0,
  memberOffset: 0,
  memberPageSize: 50,
  membersLoading: false,
  selectedMember: null,
  memberDialogTrigger: null,
  loading: false,
  loaded: {
    overview: false,
    groups: false,
    safety: false,
    blacklist: false,
    members: false,
    cache: false,
    settings: false,
  },
};

let settingsPage = null;
let memberBoostSelect = null;
// 宿主已给出主题（首帧注入或 theme 参数）时不再跟随系统偏好。
let hostThemeKnown = document.documentElement.getAttribute("data-theme-source") === "host"
  || new URLSearchParams(window.location.search).has("theme");

const MAX_BACKUP_BYTES = 5 * 1024 * 1024;
const dateFormatter = new Intl.DateTimeFormat("zh-CN", {
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});
const numberFormatter = new Intl.NumberFormat("zh-CN");

const $ = (id) => document.getElementById(id);
const els = {
  safetyStatusSummary: $("safetyStatusSummary"),
  groupList: $("groupList"),
  selectedGroupName: $("selectedGroupName"),
  selectedGroupMeta: $("selectedGroupMeta"),
  activityTrack: $("activityTrack"),
  trendCaption: $("trendCaption"),
  rankingContent: $("rankingContent"),
  memberCount: $("memberCount"),
  memberSearch: $("memberSearch"),
  memberList: $("memberList"),
  memberLoadMore: $("memberLoadMore"),
  memberDialog: $("memberDialog"),
  memberForm: $("memberForm"),
  memberDialogIdentity: $("memberDialogIdentity"),
  memberUserId: $("memberUserId"),
  memberCoins: $("memberCoins"),
  memberAffection: $("memberAffection"),
  memberTotalDays: $("memberTotalDays"),
  memberStreakDays: $("memberStreakDays"),
  memberBoostStatus: $("memberBoostStatus"),
  memberBoostAction: $("memberBoostAction"),
  memberBoostLabel: $("memberBoostLabel"),
  memberFormError: $("memberFormError"),

  customTerms: $("customTerms"),
  blacklistContent: $("blacklistContent"),
  blacklistCount: $("blacklistCount"),
  latestBackup: $("latestBackup"),
  importFile: $("importFile"),
  importBtn: $("importBtn"),
  importResult: $("importResult"),
  cacheStorageText: $("cacheStorageText"),
  cacheStorageDetail: $("cacheStorageDetail"),
  clearCacheBtn: $("clearCacheBtn"),
  refreshCacheBtn: $("refreshCacheBtn"),
  toast: $("toast"),
  dialog: $("confirmDialog"),
  dialogTitle: $("dialogTitle"),
  dialogMessage: $("dialogMessage"),
  globalError: $("globalError"),
  globalErrorMessage: $("globalErrorMessage"),
  blacklistError: $("blacklistError"),

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
  if (result?.success === false) throw new Error(result.error || "请求失败");
  return result || {};
}

function showToast(message, tone = "normal") {
  els.toast.textContent = message;
  els.toast.className = `toast show${tone === "error" ? " error" : ""}`;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { els.toast.className = "toast"; }, 3200);
}

function showGlobalError(messages) {
  const unique = [...new Set(messages.filter(Boolean))];
  els.globalErrorMessage.textContent = unique.length
    ? `部分数据加载失败：${unique.join("；")}`
    : "部分数据加载失败。";
  els.globalError.hidden = false;
}

function hideGlobalError() {
  els.globalError.hidden = true;
  els.globalErrorMessage.textContent = "";
}

function errorMessage(reason, fallback) {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

function setButtonBusy(button, busy, busyLabel, idleLabel) {
  button.disabled = busy;
  button.textContent = busy ? busyLabel : idleLabel;
  button.setAttribute("aria-busy", String(busy));
}

function formatDate(value) {
  if (!value) return "尚无记录";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return dateFormatter.format(date);
}

function formatCount(value) {
  const number = Number(value);
  return numberFormatter.format(Number.isFinite(number) ? number : 0);
}

function currentGroup() {
  return state.groups.find((item) => item.group_id === state.selectedGroup) || null;
}

async function apiGet(endpoint, params = {}) {
  return apiResult(await bridge.apiGet(endpoint, params));
}

async function apiPost(endpoint, body = {}) {
  return apiResult(await bridge.apiPost(endpoint, body));
}

async function loadFont() {
  try {
    const config = await apiGet("config");
    if (!config.font_url) return;
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = config.font_url;
    document.head.append(link);
  } catch {  }
}

// Batch load thumbnails for blacklist
async function loadBlacklistThumbnails() {
  const idsWithThumb = state.blacklist
    .filter(item => item.thumb_id)
    .map(item => item.illust_id);

  if (!idsWithThumb.length) {
    state.thumbs = {};
    return;
  }

  try {
    const res = await apiPost("image-blacklist/thumb-data-batch", { ids: idsWithThumb });
    state.thumbs = res.thumbs || {};
  } catch (error) {
    console.warn("Failed to load blacklist thumbnails:", error);
    state.thumbs = {};
  }
}

async function reloadAll() {
  if (state.loading) return;
  state.loading = true;
  const refreshButton = $("refreshBtn");
  setButtonBusy(refreshButton, true, "正在刷新…", "刷新数据");
  hideGlobalError();
  const failures = [];
  let shouldLoadRanking = false;
  try {
    const [overviewResult, groupsResult, safetyResult, blacklistResult] =
      await Promise.allSettled([
        apiGet("overview"),
        apiGet("checkin-groups"),
        apiGet("content-safety"),
        apiGet("image-blacklist"),
      ]);

    if (overviewResult.status === "fulfilled") {
      state.overview = overviewResult.value;
      state.loaded.overview = true;
      renderOverview();
      renderData();
    } else {
      failures.push(errorMessage(overviewResult.reason, "概览数据读取失败"));
      if (!state.loaded.overview) renderOverviewError();
    }

    if (groupsResult.status === "fulfilled") {
      state.groups = Array.isArray(groupsResult.value.groups)
        ? groupsResult.value.groups
        : [];
      state.groups.sort((a, b) => {
        const tA = new Date(a.last_seen_at || 0).getTime();
        const tB = new Date(b.last_seen_at || 0).getTime();
        return tB - tA;
      });
      state.loaded.groups = true;
      if (!state.groups.some((item) => item.group_id === state.selectedGroup)) {
        state.selectedGroup = state.groups[0]?.group_id || "";
      }
      renderGroups();
      shouldLoadRanking = true;
    } else {
      failures.push(errorMessage(groupsResult.reason, "群列表读取失败"));
      if (!state.loaded.groups) renderGroupsError();
    }

    if (safetyResult.status === "fulfilled") {
      state.safety = safetyResult.value;
      state.loaded.safety = true;
      renderSafety();
    } else {
      failures.push(errorMessage(safetyResult.reason, "内容安全数据读取失败"));
      if (!state.loaded.safety) renderSafetyError();
    }

    if (blacklistResult.status === "fulfilled") {
      state.blacklist = Array.isArray(blacklistResult.value.records)
        ? blacklistResult.value.records
        : [];
      state.loaded.blacklist = true;
      await loadBlacklistThumbnails();
      renderBlacklist();
    } else {
      failures.push(errorMessage(blacklistResult.reason, "作品黑名单读取失败"));
      if (!state.loaded.blacklist) renderBlacklistError();
    }

    if (shouldLoadRanking) await loadRanking();

    if (state.loaded.members) {
      try {
        await loadMembers({ reset: true });
      } catch (error) {
        failures.push(errorMessage(error, "成员资料读取失败"));
      }
    }

    if (failures.length) {
      showGlobalError(failures);
      showToast("部分数据加载失败，可重试", "error");
    }
  } finally {
    state.loading = false;
    setButtonBusy(refreshButton, false, "正在刷新…", "刷新数据");
  }
}

function renderOverview() {
  const data = state.overview;
  const terms = formatCount(
    data.custom_term_count ?? state.safety.global_safety_terms?.length ?? 0,
  );
  const blacklist = formatCount(data.blacklist_count ?? state.blacklist.length ?? 0);
  els.safetyStatusSummary.textContent =
    `全局词 ${terms} · 作品黑名单 ${blacklist} · 未分组默认关闭 R18`;
}

function renderOverviewError() {
  els.safetyStatusSummary.textContent = "安全摘要读取失败，请重新加载";
}

function renderGroups() {
  if (!state.groups.length) {
    els.groupList.innerHTML = '<div class="empty">群排行将在群成员首次签到后出现</div>';
    els.selectedGroupName.textContent = "暂无群签到数据";
    els.selectedGroupMeta.textContent = "GROUP SCOPE EMPTY";
    return;
  }
  els.groupList.innerHTML = state.groups.map((group) => {
    const groupName = group.group_name || group.group_id;
    const groupMeta = `群号: ${group.group_id} (${group.platform})`;
    return `
    <button class="group-button${group.group_id === state.selectedGroup ? " active" : ""}"
      type="button" data-group="${escapeHtml(group.group_id)}" title="${escapeHtml(groupName)} - ${escapeHtml(groupMeta)}">
      <strong title="${escapeHtml(groupName)}">${escapeHtml(groupName)}</strong>
      <small title="${escapeHtml(groupMeta)}">${escapeHtml(groupMeta)}</small>
      <small>今日 ${formatCount(group.today_count)} · 本月 ${formatCount(group.month_count)}</small>
    </button>
  `;
  }).join("");
  els.groupList.querySelectorAll("[data-group]").forEach((button) => {
    button.addEventListener("click", async () => {
      state.selectedGroup = button.dataset.group || "";
      renderGroups();
      await loadRanking();
    });
  });
  const group = currentGroup();
  els.selectedGroupName.textContent = group?.group_name || group?.group_id || "群签到轨道";
  els.selectedGroupMeta.textContent = `${group?.platform || "unknown"} / GROUP ${group?.group_id || "-"}`;
}

function renderGroupsError() {
  els.groupList.innerHTML = '<div class="empty error">群列表读取失败，请重新加载。</div>';
  els.selectedGroupName.textContent = "群签到数据暂不可用";
  els.selectedGroupMeta.textContent = "GROUP SCOPE ERROR";
  els.activityTrack.innerHTML = "";
  els.rankingContent.innerHTML = '<div class="empty error">请在群列表恢复后重试。</div>';
}

async function loadRanking() {
  if (!state.selectedGroup) {
    els.activityTrack.innerHTML = "";
    els.rankingContent.innerHTML = '<div class="empty">等待选择群聊数据</div>';
    return;
  }
  els.rankingContent.innerHTML = '<div class="empty">正在读取榜单，请稍候…</div>';
  try {
    const [ranking, trend] = await Promise.all([
      apiGet("checkin-ranking", {
        group_id: state.selectedGroup,
        type: state.rankingType,
        limit: 100,
      }),
      apiGet("checkin-trend", {
        group_id: state.selectedGroup,
        days: state.trendDays,
      }),
    ]);
    renderRanking(ranking);
    renderTrend(trend.trend || []);
  } catch (error) {
    els.rankingContent.innerHTML = `
      <div class="empty error">
        <p>读取榜单失败：${escapeHtml(error.message)}</p>
        <button type="button" id="retryRankingBtn" class="danger">重新加载</button>
      </div>`;
    const retryBtn = $("retryRankingBtn");
    if (retryBtn) {
      retryBtn.addEventListener("click", loadRanking);
    }
  }
}

function renderRanking(result) {
  const entries = Array.isArray(result.entries) ? result.entries : [];
  if (!entries.length) {
    els.rankingContent.innerHTML = '<div class="empty">当前榜单还没有记录，快去签到吧！</div>';
    return;
  }
  const units = { month: "天", streak: "天", total: "天" };
  els.rankingContent.innerHTML = entries.map((entry) => {
    const raw = String(entry.value ?? "");
    let value = "";
    if (state.rankingType === "today") {
      const match = raw.match(/\d{2}:\d{2}:\d{2}/);
      value = match ? match[0] : (raw.slice(11, 19) || raw);
    } else {
      value = `${raw}${units[state.rankingType] || "天"}`;
    }
    return `
      <div class="ranking-row">
        <span class="rank-number">${String(entry.rank).padStart(2, "0")}</span>
        <span class="rank-user"><strong>${escapeHtml(entry.username)}</strong><small>${escapeHtml(entry.user_id)}</small></span>
        <span class="rank-value">${escapeHtml(value)}</span>
      </div>`;
  }).join("");
}

function renderTrend(trend) {
  const max = Math.max(1, ...trend.map((item) => Number(item.count || 0)));
  els.activityTrack.className = `activity-track${state.trendDays === 30 ? " days-30" : ""}`;
  els.trendCaption.textContent = `最近 ${state.trendDays} 天`;
  els.activityTrack.innerHTML = trend.map((item) => {
    const count = Number(item.count || 0);
    const date = String(item.date || "");
    const label = date.slice(5).replace("-", "/");
    const height = count ? Math.max(10, Math.round((count / max) * 92)) : 3;
    return `
      <div class="track-day" tabindex="0" title="${escapeHtml(date)}：${count} 人" aria-label="${escapeHtml(date)}，签到人数 ${count} 人">
        <b>${count}</b><div class="track-bar" style="height:${height}%"></div><small>${escapeHtml(label)}</small>
      </div>`;
  }).join("");
}

function renderMembers() {
  els.memberCount.textContent = `${formatCount(state.members.length)} / ${formatCount(state.memberTotal)} 位`;
  els.memberLoadMore.hidden = state.members.length >= state.memberTotal;
  if (!state.members.length) {
    els.memberList.innerHTML = state.memberQuery
      ? '<div class="empty">没有找到匹配的签到成员</div>'
      : '<div class="empty">目前还没有签到成员资料</div>';
    return;
  }
  els.memberList.innerHTML = state.members.map((member) => {
    const remaining = Number(member.boost_remaining_days || 0);
    const boostBadge = member.boost_active
      ? `<span class="badge-boost active" title="${escapeHtml(member.boost_status || '加持生效中')}">🔥 双倍好感·余${remaining}天</span>`
      : remaining > 0
        ? `<span class="badge-boost pending" title="${escapeHtml(member.boost_status || '加持待生效')}">⏳ 待生效·余${remaining}天</span>`
        : `<span class="badge-boost">无加持</span>`;
    return `
    <article class="member-row">
      <div class="member-identity">
        <strong>${escapeHtml(member.username || member.user_id)}</strong>
        <small>${escapeHtml(member.user_id)} · 最后签到 ${escapeHtml(member.last_checkin_date || "尚无记录")}${boostBadge}</small>
      </div>
      <div class="member-metric"><small>金币</small><strong>${formatCount(member.coins)}</strong></div>
      <div class="member-metric"><small>好感度</small><strong>${Number(member.affection || 0).toFixed(2)}</strong></div>
      <div class="member-metric"><small>累计签到</small><strong>${formatCount(member.total_days)} 天</strong></div>
      <div class="member-metric"><small>连续签到</small><strong>${formatCount(member.streak_days)} 天</strong></div>
      <button class="member-edit" type="button" data-edit-member="${escapeHtml(member.user_id)}"
        aria-label="编辑 ${escapeHtml(member.username || member.user_id)} 的签到数值">编辑</button>
    </article>
  `;
  }).join("");
  els.memberList.querySelectorAll("[data-edit-member]").forEach((button) => {
    button.addEventListener("click", () => {
      const member = state.members.find((item) => item.user_id === button.dataset.editMember);
      if (member) openMemberEditor(member, button);
    });
  });
}

function renderMembersError(message = "成员资料读取失败，请重新加载。") {
  els.memberCount.textContent = "读取失败";
  els.memberList.innerHTML = `<div class="empty error">${escapeHtml(message)}</div>`;
  els.memberLoadMore.hidden = true;
}

async function loadMembers({ reset = false } = {}) {
  if (state.membersLoading) return;
  state.membersLoading = true;
  if (reset) {
    state.memberOffset = 0;
    state.members = [];
    els.memberList.innerHTML = '<div class="empty">正在读取成员资料…</div>';
  }
  try {
    const result = await apiGet("checkin-members", {
      query: state.memberQuery,
      limit: state.memberPageSize,
      offset: state.memberOffset,
    });
    const incoming = Array.isArray(result.members) ? result.members : [];
    state.members = reset ? incoming : state.members.concat(incoming);
    state.memberTotal = Number(result.total || 0);
    state.memberOffset = state.members.length;
    state.loaded.members = true;
    renderMembers();
  } catch (error) {
    renderMembersError(error.message);
    throw error;
  } finally {
    state.membersLoading = false;
  }
}

function openMemberEditor(member, trigger) {
  state.selectedMember = member;
  state.memberDialogTrigger = trigger || null;
  els.memberDialogIdentity.textContent = `${member.username || member.user_id} · ${member.user_id}`;
  els.memberUserId.value = member.user_id;
  els.memberCoins.value = String(member.coins ?? 0);
  els.memberAffection.value = Number(member.affection ?? 0).toFixed(2);
  els.memberTotalDays.value = String(member.total_days ?? 0);
  els.memberStreakDays.value = String(member.streak_days ?? 0);
  els.memberBoostStatus.textContent = member.boost_status || "无加持";
  els.memberBoostStatus.className = `boost-badge${member.boost_active ? " active" : ""}`;
  if (memberBoostSelect) memberBoostSelect.value = "keep";
  els.memberFormError.textContent = "";
  els.memberDialog.showModal();
  els.memberCoins.focus();
  els.memberCoins.select();
}

function readMemberForm() {
  const values = {
    user_id: els.memberUserId.value,
    coins: Number(els.memberCoins.value),
    affection: Number(els.memberAffection.value),
    total_days: Number(els.memberTotalDays.value),
    streak_days: Number(els.memberStreakDays.value),
    boost_action: memberBoostSelect?.value || "keep",
  };
  const integers = [
    ["金币", values.coins],
    ["累计签到", values.total_days],
    ["连续签到", values.streak_days],
  ];
  for (const [label, value] of integers) {
    if (!Number.isInteger(value) || value < 0 || value > 2147483647) {
      throw new Error(`${label}必须是 0 至 2147483647 之间的整数`);
    }
  }
  if (!Number.isFinite(values.affection) || values.affection < -10 || values.affection > 1000000) {
    throw new Error("好感度必须在 -10 至 1000000 之间");
  }
  if (values.streak_days > values.total_days) {
    throw new Error("连续签到不能大于累计签到");
  }
  values.affection = Math.round(values.affection * 100) / 100;
  return values;
}

const safetyPanel = mountSafety({ apiGet, apiPost, escapeHtml, showToast, confirmAction, onChange(value) {
  state.safety = value;
  state.loaded.safety = true;
  state.overview.custom_term_count = value.global_safety_terms.length;
  if (state.loaded.overview) renderOverview();
} });

function renderSafety() {
  safetyPanel.update(state.safety);
}


function renderSafetyError() {
  els.customTerms.textContent = "安全规则读取失败，请刷新后重试。";
}

async function reloadSafety() {
  try {
    state.safety = await apiGet("content-safety");
    state.loaded.safety = true;
    state.overview.custom_term_count = state.safety.global_safety_terms?.length || 0;
    renderSafety();
    if (state.loaded.overview) renderOverview();
  } catch (error) {
    renderSafetyError();
    showGlobalError([errorMessage(error, "内容安全数据读取失败")]);
    throw error;
  }
}

function renderBlacklist() {
  els.blacklistCount.textContent = `${state.blacklist.length} 项`;
  els.blacklistContent.innerHTML = state.blacklist.map((item) => {
    const thumbUrl = state.thumbs && state.thumbs[item.illust_id];
    const thumbHtml = thumbUrl
      ? `<div class="blacklist-thumb"><img src="${escapeHtml(thumbUrl)}" alt="作品 ${escapeHtml(item.illust_id)} 缩略图" width="50" height="50" loading="lazy" data-blacklist-thumb /></div>`
      : "";

    return `
      <div class="blacklist-item">
        ${thumbHtml}
        <span class="blacklist-id">#${escapeHtml(item.illust_id)}</span>
        <span class="blacklist-copy">
          <strong>${escapeHtml(item.title || "未获取作品标题")}</strong>
          <small>${escapeHtml(item.author || "作者未知")} · ${escapeHtml(item.reason || "未填写加入原因")} · ${escapeHtml(item.added_by || "管理员")}</small>
        </span>
        <span class="blacklist-meta">${escapeHtml(formatDate(item.added_at))}</span>
        <button type="button" data-remove-illust="${escapeHtml(item.illust_id)}">解除</button>
      </div>
    `;
  }).join("") || '<div class="empty">作品黑名单为空</div>';

  els.blacklistContent.querySelectorAll("[data-blacklist-thumb]").forEach((image) => {
    image.addEventListener("error", () => {
      image.closest(".blacklist-thumb")?.remove();
    }, { once: true });
  });

  els.blacklistContent.querySelectorAll("[data-remove-illust]").forEach((button) => {
    button.addEventListener("click", async () => {
      const illustId = button.dataset.removeIllust;
      if (!await confirmAction("解除作品黑名单", `确定要将作品 ID ${illustId} 移出黑名单吗？`)) return;
      button.disabled = true;
      try {
        const result = await apiPost("image-blacklist/remove", { illust_id: illustId });
        showToast(result.warning || "作品已移出黑名单");
        await reloadBlacklist();
      } catch (error) {
        button.disabled = false;
        showToast(error.message, "error");
      }
    });
  });
}

function renderBlacklistError() {
  els.blacklistCount.textContent = "读取失败";
  els.blacklistContent.innerHTML = '<div class="empty error">作品黑名单暂时无法读取。</div>';
}

async function reloadBlacklist() {
  try {
    const result = await apiGet("image-blacklist");
    state.blacklist = result.records || [];
    state.loaded.blacklist = true;
    await loadBlacklistThumbnails();
    state.overview.blacklist_count = state.blacklist.length;
    renderBlacklist();
    if (state.loaded.overview) renderOverview();
  } catch (error) {
    renderBlacklistError();
    showGlobalError([errorMessage(error, "作品黑名单读取失败")]);
    throw error;
  }
}

function renderData() {
  els.latestBackup.textContent = state.overview.latest_backup_at
    ? `最近备份：${formatDate(state.overview.latest_backup_at)}` : "最近备份：尚无备份记录";
}

async function loadCacheStorage() {
  if (!els.cacheStorageText) return;
  state.loaded.cache = true;
  els.cacheStorageText.innerHTML = '<span class="muted">正在读取…</span>';
  try {
    const res = await apiGet("cache-storage");
    els.cacheStorageText.innerHTML = `<strong>${escapeHtml(res.total_human || "0 B")}</strong> <small class="muted">(${formatCount(res.total_count || 0)} 个文件)</small>`;
    els.cacheStorageDetail.textContent = `卡片缓存：${res.card_cache_human} (${formatCount(res.card_cache_count)}) · 临时下载：${res.temp_download_human} (${formatCount(res.temp_download_count)})`;
  } catch (error) {
    els.cacheStorageText.textContent = "读取失败";
    els.cacheStorageDetail.textContent = error.message || "无法读取缓存统计";
  }
}

async function clearCacheStorage() {
  const confirmed = await confirmAction("清空临时缓存", "确定要清理已生成的卡片缓存和临时下载文件吗？这不会影响任何签到记录。");
  if (!confirmed) return;
  setButtonBusy(els.clearCacheBtn, true, "正在清理…", "一键清理临时缓存");
  try {
    const res = await apiPost("cache-storage/clear");
    showToast(`缓存已清空，释放了 ${res.freed_human} (${formatCount(res.deleted_files)} 个文件)`);
    await loadCacheStorage();
  } catch (error) {
    showToast(error.message || "清理缓存失败", "error");
  } finally {
    setButtonBusy(els.clearCacheBtn, false, "正在清理…", "一键清理临时缓存");
  }
}

function backupFileError(file) {
  if (!file) return "请先选择备份文件。";
  if (!file.name.toLocaleLowerCase("zh-CN").endsWith(".json")) {
    return "只能选择 JSON 备份文件。";
  }
  if (file.size > MAX_BACKUP_BYTES) {
    return "备份文件不能超过 5 MiB。";
  }
  return "";
}

function tabButtons() {
  return [...document.querySelectorAll(".nav-tabs [data-view]")];
}

function switchView(name) {
  const validNames = new Set(["ranking", "members", "safety", "policies", "data", "settings"]);
  if (!validNames.has(name)) name = "ranking";
  for (const button of tabButtons()) {
    const active = button.dataset.view === name;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
    button.tabIndex = active ? 0 : -1;
  }
  document.querySelectorAll(".view").forEach((view) => {
    const active = view.id === `${name}View`;
    view.classList.toggle("active", active);
    view.hidden = !active;
  });
  document.documentElement.dataset.view = name;
  history.replaceState(null, "", `#${name}`);
  if (name === "members" && !state.loaded.members) {
    loadMembers({ reset: true }).catch((error) => {
      showGlobalError([errorMessage(error, "成员资料读取失败")]);
    });
  }
  if (name === "data" && !state.loaded.cache) {
    loadCacheStorage();
  }
  if ((name === "policies" || name === "safety") && !state.loaded.safety) {
    reloadSafety().catch((error) => showToast(error.message, "error"));
  }
  if (name === "settings" && settingsPage && !state.loaded.settings) {
    settingsPage.load().then(() => { state.loaded.settings = true; })
      .catch((error) => showToast(error.message || "设置读取失败", "error"));
  }
}

function confirmAction(title, message, confirmLabel = "确认执行") {
  const triggerEl = document.activeElement;
  els.dialogTitle.textContent = title;
  els.dialogMessage.textContent = message;
  $("dialogConfirm").textContent = confirmLabel;
  els.dialog.showModal();
  return new Promise((resolve) => {
    const onClose = () => {
      els.dialog.removeEventListener("close", onClose);
      if (triggerEl && typeof triggerEl.focus === "function") {
        triggerEl.focus();
      }
      resolve(els.dialog.returnValue === "confirm");
    };
    els.dialog.addEventListener("close", onClose);
  });
}

function applyTheme(isDark) {
  const dark = typeof isDark === "boolean"
    ? isDark
    : Boolean(window.matchMedia?.("(prefers-color-scheme: dark)").matches);
  document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
  document.querySelector('meta[name="theme-color"]')?.setAttribute(
    "content", dark ? "#171c2b" : "#f5f7fc",
  );
}

function followHostTheme() {
  if (!bridge) return;
  const current = bridge.getContext?.();
  if (typeof current?.isDark === "boolean") {
    hostThemeKnown = true;
    applyTheme(current.isDark);
  }
  bridge.onContext?.((next) => {
    if (typeof next?.isDark !== "boolean") return;
    hostThemeKnown = true;
    applyTheme(next.isDark);
  });
  window.matchMedia?.("(prefers-color-scheme: dark)").addEventListener?.("change", (event) => {
    if (!hostThemeKnown) applyTheme(event.matches);
  });
}

function mountMemberBoostSelect() {
  memberBoostSelect = createSelect({
    name: "boost_action",
    labelledBy: els.memberBoostLabel?.id || "",
    value: "keep",
    options: [
      { value: "keep", label: "保持当前加持不变" },
      { value: "clear", label: "清除加持（重置为无加持）" },
      { value: "add_1", label: "延长 1 天双倍好感加持" },
      { value: "add_3", label: "延长 3 天双倍好感加持" },
      { value: "add_7", label: "延长 7 天双倍好感加持" },
    ],
  });
  els.memberBoostAction.replaceChildren(memberBoostSelect);
}

function bindEvents() {
  mountMemberBoostSelect();
  document.querySelectorAll(".nav-tabs [data-view]").forEach((button) =>
    button.addEventListener("click", () => switchView(button.dataset.view)));

  // 自动激活：方向键/Home/End 移动焦点同时切换面板，与鼠标点击一致。
  $("navTabs").addEventListener("keydown", (event) => {
    const tabs = tabButtons();
    const index = tabs.indexOf(event.target);
    if (index < 0) return;
    let next = null;
    if (event.key === "ArrowRight") next = index + 1;
    else if (event.key === "ArrowLeft") next = index - 1;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = tabs.length - 1;
    if (next === null) return;
    event.preventDefault();
    const target = tabs[(next + tabs.length) % tabs.length];
    target.focus();
    switchView(target.dataset.view);
  });

  document.querySelectorAll("[data-rank]").forEach((button) => button.addEventListener("click", async () => {
    state.rankingType = button.dataset.rank;
    document.querySelectorAll("[data-rank]").forEach((item) => item.classList.toggle("active", item === button));
    await loadRanking();
  }));

  document.querySelectorAll("[data-days]").forEach((button) => button.addEventListener("click", async () => {
    state.trendDays = Number(button.dataset.days || 7);
    document.querySelectorAll("[data-days]").forEach((item) => item.classList.toggle("active", item === button));
    await loadRanking();
  }));

  $("refreshBtn").addEventListener("click", reloadAll);
  $("retryAllBtn").addEventListener("click", reloadAll);

  $("memberSearchForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    state.memberQuery = els.memberSearch.value.trim();
    try {
      await loadMembers({ reset: true });
    } catch (error) {
      showToast(error.message || "搜索成员失败", "error");
    }
  });

  $("memberClearBtn").addEventListener("click", async () => {
    els.memberSearch.value = "";
    state.memberQuery = "";
    try {
      await loadMembers({ reset: true });
      els.memberSearch.focus();
    } catch (error) {
      showToast(error.message || "成员资料读取失败", "error");
    }
  });

  els.memberLoadMore.addEventListener("click", async () => {
    setButtonBusy(els.memberLoadMore, true, "正在加载…", "加载更多成员");
    try {
      await loadMembers();
    } catch (error) {
      showToast(error.message || "加载更多成员失败", "error");
    } finally {
      setButtonBusy(els.memberLoadMore, false, "正在加载…", "加载更多成员");
    }
  });

  $("memberCancelBtn").addEventListener("click", () => els.memberDialog.close());
  els.memberDialog.addEventListener("close", () => {
    state.selectedMember = null;
    state.memberDialogTrigger?.focus();
    state.memberDialogTrigger = null;
  });
  els.memberForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!els.memberForm.reportValidity()) return;
    const submitButton = $("memberSaveBtn");
    els.memberFormError.textContent = "";
    try {
      const payload = readMemberForm();
      setButtonBusy(submitButton, true, "正在保存…", "保存修改");
      const result = await apiPost("checkin-members/update", payload);
      const index = state.members.findIndex((item) => item.user_id === result.member?.user_id);
      if (index >= 0) state.members[index] = result.member;
      renderMembers();
      els.memberDialog.close();
      showToast("成员签到数值已更新");
    } catch (error) {
      els.memberFormError.textContent = error.message || "保存失败，请检查输入后重试。";
      showToast(els.memberFormError.textContent, "error");
    } finally {
      setButtonBusy(submitButton, false, "正在保存…", "保存修改");
    }
  });


  $("blacklistForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const illustId = $("blacklistId").value.trim();
    const reason = $("blacklistReason").value.trim();
    const submitButton = event.currentTarget.querySelector('button[type="submit"]');
    els.blacklistError.textContent = "";
    setButtonBusy(submitButton, true, "正在加入…", "加入黑名单");
    try {
      const result = await apiPost("image-blacklist/add", { illust_id: illustId, reason });
      event.target.reset();
      showToast(result.warning || "作品已加入黑名单");
      await reloadBlacklist();
    } catch (error) {
      els.blacklistError.textContent = error.message || "加入失败，请检查作品 ID 后重试。";
      $("blacklistId").focus();
      showToast(els.blacklistError.textContent, "error");
    } finally {
      setButtonBusy(submitButton, false, "正在加入…", "加入黑名单");
    }
  });

  $("exportBtn").addEventListener("click", async () => {
    const button = $("exportBtn");
    setButtonBusy(button, true, "正在准备…", "下载备份 (.json)");
    try {
      await bridge.download("data-export", {}, "get-px-data.json");
      showToast("插件数据导出已开始下载");
    } catch (error) {
      showToast(error.message || "下载备份失败，请重试。", "error");
    } finally {
      setButtonBusy(button, false, "正在准备…", "下载备份 (.json)");
    }
  });

  els.importFile.addEventListener("change", () => {
    const file = els.importFile.files?.[0];
    const validationError = backupFileError(file);
    els.importBtn.disabled = Boolean(validationError);
    els.importResult.textContent = file && !validationError
      ? `${file.name} · ${(file.size / 1024).toFixed(1)} KiB`
      : validationError;
  });

  els.importBtn.addEventListener("click", async () => {
    const file = els.importFile.files?.[0];
    const validationError = backupFileError(file);
    if (validationError) {
      els.importResult.textContent = validationError;
      els.importFile.focus();
      return;
    }
    if (!await confirmAction("恢复签到数据", "这将会覆盖当前所有签到记录与用户购买主题数据！恢复前系统会自动将现有数据创建为回滚备份。确认要继续吗？")) return;

    els.importBtn.disabled = true;
    els.importResult.textContent = "正在上传并恢复，请稍候…";

    try {
      const result = apiResult(await bridge.upload("checkin-import", file));
      els.importResult.textContent = `恢复成功！已导入：
        ${result.users || 0} 位用户，
        ${result.records || 0} 条签到历史，
        ${result.group_presence || 0} 条群活跃记录，
        ${result.user_themes || 0} 个用户主题，
        回滚文件为 ${result.rollback_file || "未知"}。`;
      els.importFile.value = "";
      showToast("签到备份已成功恢复！");
      await reloadAll();
    } catch (error) {
      els.importResult.textContent = `恢复失败：${error.message || "未知错误"}`;
      showToast(error.message || "恢复失败", "error");
    } finally {
      els.importBtn.disabled = !els.importFile.files?.length;
    }
  });

  els.clearCacheBtn?.addEventListener("click", clearCacheStorage);
  els.refreshCacheBtn?.addEventListener("click", loadCacheStorage);
}

function followStickyHeights() {
  const root = document.documentElement;
  const shell = document.querySelector(".nav-shell");
  const toolbar = document.querySelector(".config-actionbar");
  const observer = new ResizeObserver((entries) => {
    for (const entry of entries) {
      const height = entry.borderBoxSize?.[0]?.blockSize ?? entry.target.getBoundingClientRect().height;
      if (!height) continue;
      root.style.setProperty(
        entry.target === shell ? "--shell-height" : "--config-toolbar-height",
        `${Math.ceil(height)}px`,
      );
    }
  });
  observer.observe(shell);
  observer.observe(toolbar);
}

async function start() {
  followStickyHeights();
  if (!bridge) {
    showToast("AstrBot 页面桥接不可用，请在 AstrBot 内置环境中打开", "error");
    return;
  }
  await bridge.ready();
  followHostTheme();
  bindEvents();
  settingsPage = settingsModule.createSettingsPage({
    bridge,
    notify: showToast,
    navigate: switchView,
    confirm: ({ title, message, confirmLabel }) =>
      confirmAction(title, message, confirmLabel),
  });
  const initialView = location.hash.slice(1);
  switchView(initialView || "ranking");
  loadFont();
  await reloadAll();
}

start();
