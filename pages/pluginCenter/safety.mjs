import { createSelect, closeDialog } from "./ui.mjs";

const modeHints = {
  宽松: "仅匹配完整原标签或翻译标签；忽略标题和简介。",
  严格: "匹配完整标签，并检查标题包含关键词；忽略简介。",
};

export function splitLines(value) {
  return [...new Set(String(value).split(/\r?\n/).map(item => item.trim()).filter(Boolean))];
}

export function createGroupDraft(group = {}) {
  return { group_id: group.group_id || "", name: group.name || "", sessions: [...(group.sessions || [])], allow_r18: group.allow_r18 === true, custom_terms: [...(group.custom_terms || [])] };
}

export function validateGroupDraft(draft, groups) {
  if (!draft.name.trim()) throw new Error("请填写分组名称");
  const claimed = new Set(groups.filter(group => group.group_id !== draft.group_id).flatMap(group => group.sessions));
  for (const session of draft.sessions) {
    if (!/^[^:\s]+:(GroupMessage|FriendMessage|OtherMessage):\S.*$/.test(session)) throw new Error("关联会话请填写 /sid 的完整值");
    if (claimed.has(session)) throw new Error("一个会话只能属于一个分组");
  }
  return draft;
}

export function mountSafety({ apiGet, apiPost, escapeHtml, showToast, confirmAction, onChange }) {
  const $ = id => document.getElementById(id);
  let settings = { safety_groups: [], global_safety_terms: [], keyword_match_mode: "宽松" };
  let draft = null, baseline = null, busy = false, trigger = null;
  const dialog = $("safetyGroupDialog");
  const form = $("safetyGroupForm");
  const modeSelect = createSelect({
    labelledBy: "keywordMatchModeLabel",
    value: "宽松",
    options: [{ value: "宽松", label: "宽松" }, { value: "严格", label: "严格" }],
    onChange: async (next) => {
      modeSelect.setDisabled(true);
      try {
        await apiPost("content-safety/global", { keyword_match_mode: next });
        await reload();
        showToast("全局匹配方式已保存");
      } catch (error) {
        mode();
        showToast(error.message, "error");
      } finally {
        modeSelect.setDisabled(false);
      }
    },
  });
  $("keywordMatchMode").replaceChildren(modeSelect);
  const dirty = () => draft && JSON.stringify(readDraft()) !== JSON.stringify(baseline);
  const readDraft = () => ({ ...draft, name: $("safetyGroupName").value.trim(), sessions: splitLines($("safetyGroupSessions").value), allow_r18: $("safetyGroupR18").checked, custom_terms: splitLines($("safetyGroupTerms").value) });
  function mode() {
    modeSelect.value = settings.keyword_match_mode;
    $("keywordModeHint").textContent = modeHints[settings.keyword_match_mode];
  }
  function render() {
    mode();
    $("customCount").textContent = `${settings.global_safety_terms.length} 项`;
    $("customTerms").innerHTML = settings.global_safety_terms.map(term => `<div class="term-item"><strong>${escapeHtml(term)}</strong><button class="btn btn-sm btn-quiet btn-danger-quiet" type="button" data-remove-term="${escapeHtml(term)}">删除</button></div>`).join("") || '<div class="empty">没有全局屏蔽关键词。点击上方输入框添加。</div>';
    $("policyCount").textContent = String(settings.safety_groups.length);
    const query = $("policySearch").value.trim().toLocaleLowerCase();
    const groups = settings.safety_groups.filter(group => `${group.name} ${group.sessions.join(" ")}`.toLocaleLowerCase().includes(query));
    $("policyList").innerHTML = groups.map(group => `<button type="button" class="safety-group-card" data-edit-group="${escapeHtml(group.group_id)}"><strong>${escapeHtml(group.name)}</strong><span class="safety-rating ${group.allow_r18 ? "sensitive" : ""}">${group.allow_r18 ? "允许 R18" : "仅普通分级"}</span><span>${group.sessions.length} 个会话 · ${group.custom_terms.length} 个附加关键词</span><small>${group.sessions.length ? escapeHtml(group.sessions[0]) : "尚未关联会话"}</small></button>`).join("") || '<div class="empty">暂无匹配分组。未分组会话仍遵循全局规则，R18 关闭。</div>';
  }
  async function reload() {
    settings = await apiGet("content-safety");
    onChange(settings);
    render();
    return settings;
  }
  function open(group, button) {
    draft = createGroupDraft(group);
    baseline = createGroupDraft(group);
    trigger = button;
    $("safetyGroupName").value = draft.name;
    $("safetyGroupSessions").value = draft.sessions.join("\n");
    $("safetyGroupR18").checked = draft.allow_r18;
    $("safetyGroupTerms").value = draft.custom_terms.join("\n");
    $("safetyGroupError").textContent = "";
    $("safetyGroupDelete").hidden = !draft.group_id;
    $("safetyGroupTitle").textContent = draft.group_id ? "编辑安全分组" : "新建安全分组";
    dialog.showModal();
    $("safetyGroupName").focus();
  }
  async function close() {
    if (busy) return;
    if (dirty() && !await confirmAction("放弃未保存修改", "关闭将丢弃当前分组草稿，确认继续吗？")) return;
    await closeDialog(dialog);
  }
  dialog.addEventListener("cancel", event => { event.preventDefault(); close(); });
  dialog.addEventListener("close", () => { draft = baseline = null; trigger?.focus(); });
  $("safetyGroupCancel").addEventListener("click", close);
  $("policyAddBtn").addEventListener("click", event => open({}, event.currentTarget));
  $("policySearch").addEventListener("input", render);
  $("policyList").addEventListener("click", event => {
    const button = event.target.closest("[data-edit-group]");
    if (button) open(settings.safety_groups.find(group => group.group_id === button.dataset.editGroup), button);
  });
  $("customTerms").addEventListener("click", async event => {
    const button = event.target.closest("[data-remove-term]");
    if (!button || !await confirmAction("删除全局关键词", "删除后所有会话都不再使用此关键词，确认继续吗？")) return;
    button.disabled = true;
    try { await apiPost("content-safety/terms/remove", { term: button.dataset.removeTerm }); await reload(); }
    catch (error) { showToast(error.message, "error"); button.disabled = false; }
  });
  $("termForm").addEventListener("submit", async event => {
    event.preventDefault();
    const button = event.currentTarget.querySelector("button");
    button.disabled = true;
    $("termError").textContent = "";
    try { await apiPost("content-safety/terms/add", { term: $("termInput").value.trim() }); $("termInput").value = ""; await reload(); }
    catch (error) { $("termError").textContent = error.message; }
    finally { button.disabled = false; }
  });
  function setBusy(value, action = "save") {
    busy = value;
    form.setAttribute("aria-busy", String(value));
    form.querySelectorAll("input,textarea,button").forEach(control => { control.disabled = value; });
    const save = form.querySelector('button[type="submit"]');
    const remove = $("safetyGroupDelete");
    save.textContent = value && action === "save" ? "正在保存…" : "保存分组";
    save.setAttribute("aria-busy", String(value && action === "save"));
    remove.textContent = value && action === "remove" ? "正在删除…" : "删除分组";
    remove.setAttribute("aria-busy", String(value && action === "remove"));
  }
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (busy) return;
    try {
      const payload = validateGroupDraft(readDraft(), settings.safety_groups);
      if (!payload.group_id) delete payload.group_id;
      setBusy(true);
      const result = await apiPost("content-safety/groups/save", payload);
      const savedGroup = result.group;
      if (savedGroup) {
        settings.safety_groups = settings.safety_groups.filter(
          group => group.group_id !== savedGroup.group_id,
        ).concat(savedGroup);
        baseline = createGroupDraft(savedGroup);
        onChange(settings);
        render();
      }
      await closeDialog(dialog);
      showToast("安全分组已保存");
      // 当前接口返回规范化分组，无需再等一轮 GET；旧响应才补读。
      if (!savedGroup) {
        try { await reload(); }
        catch { showToast("安全分组已保存，列表刷新失败，请刷新数据", "error"); }
      }
    } catch (error) { if (dialog.open) $("safetyGroupError").textContent = error.message; else showToast(error.message, "error"); }
    finally { setBusy(false); }
  });
  $("safetyGroupDelete").addEventListener("click", async () => {
    if (busy || !await confirmAction("删除安全分组", "该组会话将恢复全局规则并关闭 R18，确认删除吗？")) return;
    setBusy(true, "remove");
    try {
      const groupId = draft.group_id;
      await apiPost("content-safety/groups/remove", { group_id: groupId });
      settings.safety_groups = settings.safety_groups.filter(group => group.group_id !== groupId);
      onChange(settings);
      render();
      await closeDialog(dialog);
      showToast("安全分组已删除");
    }
    catch (error) { if (dialog.open) $("safetyGroupError").textContent = error.message; else showToast(error.message, "error"); }
    finally { setBusy(false); }
  });
  return { reload, update(value) { settings = value; render(); } };
}
