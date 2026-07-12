import {
  state,
  els,
  escapeHtml,
  thumbUrl,
  blacklistThumbUrl,
  isBlacklistMode,
  activeRecords,
  activeFilteredRecords,
  pixivUrl,
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
} from "./core.js";

function renderCheckinImportPanel() {
  if (!els.checkinImportMeta || !els.checkinImportBtn || !els.checkinImportResult) {
    return;
  }

  const file = state.checkinImportFile;
  els.checkinImportMeta.textContent = file
    ? `${file.name} · ${formatUploadBytes(file.size)}`
    : "未选择备份文件";
  els.checkinImportBtn.disabled = !file || state.checkinImporting;
  els.checkinImportBtn.textContent = state.checkinImporting ? "导入中..." : "导入并覆盖";

  const result = state.checkinImportResult;
  if (!result) {
    els.checkinImportResult.hidden = true;
    els.checkinImportResult.className = "checkin-transfer-result";
    els.checkinImportResult.innerHTML = "";
    return;
  }

  if (result.success) {
    els.checkinImportResult.hidden = false;
    els.checkinImportResult.className = "checkin-transfer-result success";
    els.checkinImportResult.innerHTML = `
      <strong>导入完成</strong>
      <div>用户数：${escapeHtml(result.profiles ?? "-")}</div>
      <div>签到记录：${escapeHtml(result.records ?? "-")}</div>
      <div>导出时间：${escapeHtml(result.exported_at || "-")}</div>
      <div>导入时间：${escapeHtml(result.imported_at || "-")}</div>
      <div>回滚备份：${escapeHtml(result.rollback_path || "-")}</div>
    `;
    return;
  }

  els.checkinImportResult.hidden = false;
  els.checkinImportResult.className = "checkin-transfer-result error";
  els.checkinImportResult.innerHTML = `
    <strong>导入失败</strong>
    <div>${escapeHtml(result.error || "未知错误")}</div>
  `;
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

export {
  renderCheckinImportPanel,
  setBusy,
  findRecord,
  upsertBlacklistRecord,
  removeRecord,
  removeRecordsByIllustId,
  removeBlacklistRecord,
  renderSourceOptions,
  renderContent,
  showToast,
};
