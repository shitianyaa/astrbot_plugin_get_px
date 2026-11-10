import { createSelect, createSwitch } from "./ui.mjs";

const byId = (id) => document.getElementById(id);
const hasOwn = (object, key) => Object.prototype.hasOwnProperty.call(object, key);

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function sameSettingValue(left, right) {
  if (Array.isArray(left) && Array.isArray(right)) {
    // 多选只比较集合，选项顺序变化不算修改。
    return JSON.stringify([...left].sort()) === JSON.stringify([...right].sort());
  }
  return JSON.stringify(left) === JSON.stringify(right);
}

/** 声明选项 + 当前已保存值；provider 额外提供显式空值。 */
export function normalizeChoiceOptions(options, current, allowEmpty = false) {
  const normalized = [...(Array.isArray(options) ? options : [])].map((item) => (
    typeof item === "string"
      ? { value: item, label: item }
      : { value: String(item?.value ?? ""), label: String(item?.label ?? item?.value ?? "") }
  ));
  const result = allowEmpty ? [{ value: "", label: "（留空）使用会话默认模型" }, ...normalized] : normalized;
  if (current && !result.some((item) => item.value === current)) {
    result.unshift({ value: current, label: `${current}（当前已保存）` });
  }
  return result;
}

/** 按标签、键名与说明匹配字段；空查询返回全部。 */
export function filterSettingsFields(fields, query) {
  const needle = String(query ?? "").trim().toLocaleLowerCase();
  if (!needle) return [...(Array.isArray(fields) ? fields : [])];
  return (Array.isArray(fields) ? fields : []).filter((field) => (
    [field.label, field.key, field.hint]
      .some((text) => String(text ?? "").toLocaleLowerCase().includes(needle))
  ));
}

/** 分组本身或任一字段命中搜索词；分组名命中时右栏可能没有字段。 */
export function settingsGroupMatches(group, query) {
  const needle = String(query ?? "").trim().toLocaleLowerCase();
  if (!needle) return true;
  if (String(group?.label ?? "").toLocaleLowerCase().includes(needle)) return true;
  return filterSettingsFields(group?.fields, needle).length > 0;
}

/** 每个分组在搜索词下的命中数，导航与自动定位共用同一口径。 */
export function settingsSearchHits(groups, query) {
  return (Array.isArray(groups) ? groups : []).map((group) => ({
    key: group.key,
    hits: filterSettingsFields(group.fields, query).length,
    matched: settingsGroupMatches(group, query),
  }));
}

/** 搜索时停在命中字段最多的分组；用户手动选组后不再自动跳走。 */
export function pickSearchGroup(groups, query, activeKey, pinned = false) {
  const list = Array.isArray(groups) ? groups : [];
  if (!list.length) return "";
  const current = list.find((group) => group.key === activeKey) || null;
  const needle = String(query ?? "").trim();
  if (!needle) return current?.key ?? list[0].key;
  if (pinned && current) return current.key;
  let best = null;
  let bestHits = 0;
  for (const group of list) {
    const hits = filterSettingsFields(group.fields, needle).length;
    if (hits > bestHits) {
      best = group;
      bestHits = hits;
    }
  }
  const named = list.find((group) => settingsGroupMatches(group, needle));
  return (best || named || current || list[0]).key;
}

/**
 * 数字输入的提交判定：空文本、非数字、越界都返回 invalid，
 * 调用方不得把空文本转成 null 提交。
 */
export function parseNumberDraft(raw, field = {}) {
  const text = String(raw ?? "").trim();
  if (!text) return { valid: false, message: "请填写数值" };
  const value = Number(text);
  if (!Number.isFinite(value)) return { valid: false, message: "请填写有效数值" };
  if (field.type === "int" && !Number.isInteger(value)) {
    return { valid: false, message: "请填写整数" };
  }
  const slider = field.slider || {};
  if (slider.min !== undefined && value < slider.min) {
    return { valid: false, message: `不能小于 ${slider.min}` };
  }
  if (slider.max !== undefined && value > slider.max) {
    return { valid: false, message: `不能大于 ${slider.max}` };
  }
  // 与 HTML step 同语义：相对 slider.min 的倍数，允许浮点误差；"any" 不限制。
  const step = slider.step;
  if (step !== undefined && step !== null && String(step) !== "any" && Number(step) > 0) {
    const base = Number.isFinite(Number(slider.min)) ? Number(slider.min) : 0;
    const steps = (value - base) / Number(step);
    if (Math.abs(steps - Math.round(steps)) > 1e-9 * Math.max(1, Math.abs(steps))) {
      return { valid: false, message: `需为 ${base} 起每 ${step} 的倍数` };
    }
  }
  return { valid: true, value };
}

/** 多选当前值：声明选项按声明顺序，保留的旧值按已保存顺序排在后面。 */
export function collectListSelection(optionValues, legacyValues, checkedValues) {
  const checked = new Set(Array.isArray(checkedValues) ? checkedValues : []);
  const declared = (Array.isArray(optionValues) ? optionValues : [])
    .filter((value) => checked.has(value));
  const legacy = (Array.isArray(legacyValues) ? legacyValues : [])
    .filter((value) => checked.has(value) && !declared.includes(value));
  return [...declared, ...legacy];
}

/** 只提交真正改动过的字段；与已保存值相同的草稿不产生补丁。 */
export function collectSettingsPatch(groups, draft, clears = new Set()) {
  const patch = {};
  const clearSecrets = [];
  for (const group of groups) {
    const values = {};
    for (const field of group.fields) {
      if (field.secret) {
        if (clears.has(field.key)) {
          clearSecrets.push(field.key);
          continue;
        }
        if (!hasOwn(draft, field.key)) continue;
        const next = draft[field.key];
        if (typeof next === "string" && next.trim()) values[field.key] = next.trim();
        continue;
      }
      if (field.delegated || field.editable === false) continue;
      if (!hasOwn(draft, field.key)) continue;
      const next = draft[field.key];
      if (sameSettingValue(next, field.value)) continue;
      values[field.key] = next;
    }
    if (Object.keys(values).length) patch[group.key] = values;
  }
  return { patch, clearSecrets };
}

function patchCount(patch, clearSecrets) {
  let total = clearSecrets.length;
  for (const values of Object.values(patch)) total += Object.keys(values).length;
  return total;
}

function legacyListValues(field) {
  const declared = new Set(Array.isArray(field.options) ? field.options : []);
  return (Array.isArray(field.value) ? field.value : [])
    .filter((value) => !declared.has(value));
}

function statusLabel({ count, invalidCount, saving, loaded, saved, error }) {
  if (saving) return "正在保存设置…";
  if (error) return error;
  if (invalidCount) return `有 ${invalidCount} 项数值需要修正`;
  if (count) return `存在未保存的修改（${count} 项）`;
  if (saved) return "设置已保存，部分项需重载插件后生效";
  return loaded ? "配置已同步" : "正在读取设置…";
}

export function createSettingsPage({ bridge, notify, navigate, confirm }) {
  const form = byId("configSettingsForm");
  const fieldsNode = byId("settingsGroups");
  const navNode = byId("settingsNav");
  const searchInput = byId("settingsSearch");
  const groupTitle = byId("settingsGroupTitle");
  const groupCount = byId("settingsGroupCount");
  const status = byId("settingsStatus");
  const saveButton = byId("settingsSaveBtn");
  const reloadButton = byId("settingsReloadBtn");
  const mobileSlot = byId("settingsGroupSelect");

  let groups = [];
  let draft = {};
  let clears = new Set();
  let invalid = new Map();
  let activeKey = "";
  let query = "";
  let pinned = false;
  let saving = false;
  let loading = false;
  let loaded = false;
  let saved = false;
  let errorNotice = "";
  let mobileSelect = null;
  let busy = false;
  // 冻结期间记录每个控件原本的 disabled，结束后逐项恢复：
  // 清除令牌勾选后密码框本就禁用，不能被统一的 false 解除。
  const busyStates = new Map();
  let customControls = [];

  function groupOf(key) {
    return groups.find((group) => group.key === key) || null;
  }

  function fieldOf(key) {
    for (const group of groups) {
      const field = group.fields.find((item) => item.key === key);
      if (field) return field;
    }
    return null;
  }

  function valueOf(field) {
    return hasOwn(draft, field.key) ? draft[field.key] : field.value;
  }

  function changedCount() {
    const { patch, clearSecrets } = collectSettingsPatch(groups, draft, clears);
    return patchCount(patch, clearSecrets);
  }

  function updateChrome() {
    const { patch, clearSecrets } = collectSettingsPatch(groups, draft, clears);
    const count = patchCount(patch, clearSecrets);
    status.textContent = statusLabel({
      count,
      invalidCount: invalid.size,
      saving,
      loaded,
      saved,
      error: errorNotice,
    });
    status.dataset.state = saving
      ? "busy"
      : errorNotice || invalid.size || count
        ? "dirty"
        : saved ? "saved" : "clean";
    saveButton.disabled = saving || loading || !count || invalid.size > 0;
    reloadButton.disabled = saving || loading;
    if (searchInput) searchInput.disabled = saving || loading;
    for (const button of navNode.querySelectorAll("[data-group]")) {
      const group = groupOf(button.dataset.group);
      if (!group) continue;
      const groupDirty = patchCount(
        patch[group.key] ? { [group.key]: patch[group.key] } : {},
        clearSecrets.filter((key) => group.fields.some((field) => field.key === key)),
      );
      // 无效草稿可能停在别的分组：导航徽标要能把它标出来。
      const groupInvalid = [...invalid.keys()]
        .filter((key) => group.fields.some((field) => field.key === key)).length;
      const hits = filterSettingsFields(group.fields, query).length;
      const badge = button.querySelector(".config-nav-badge");
      if (badge) {
        badge.textContent = groupDirty
          ? `${groupDirty} 改`
          : groupInvalid ? `${groupInvalid} 待修正` : `${hits} 项`;
        badge.dataset.state = groupDirty || groupInvalid ? "dirty" : "idle";
      }
      button.classList.toggle("active", group.key === activeKey);
      button.setAttribute("aria-current", group.key === activeKey ? "true" : "false");
      button.classList.toggle(
        "no-hit",
        Boolean(query.trim()) && !settingsGroupMatches(group, query),
      );
    }
    const active = groupOf(activeKey);
    if (groupTitle) groupTitle.textContent = active?.label || "插件配置";
    if (groupCount) {
      const hits = active ? filterSettingsFields(active.fields, query).length : 0;
      groupCount.textContent = query.trim() ? `匹配 ${hits} 项` : `${hits} 项`;
    }
  }

  function renderNav() {
    navNode.replaceChildren(...groups.map((group) => {
      const button = element("button", "config-nav-item");
      button.type = "button";
      button.dataset.group = group.key;
      const name = element("span", "config-nav-name", group.label);
      const badge = element("span", "config-nav-badge");
      button.append(name, badge);
      button.addEventListener("click", () => selectGroup(group.key, true));
      return button;
    }));
  }

  function selectGroup(key, fromUser) {
    if (busy || key === activeKey) return;
    activeKey = key;
    if (fromUser) pinned = true;
    if (mobileSelect && mobileSelect.value !== key) mobileSelect.value = key;
    renderNav();
    renderFields();
    updateChrome();
  }

  function commit(key, value) {
    if (busy) return;
    const field = fieldOf(key);
    if (!field) return;
    saved = false;
    errorNotice = "";
    draft[key] = value;
    if (sameSettingValue(value, field.value)) delete draft[key];
    updateChrome();
  }

  function setInvalid(key, message, raw) {
    invalid.set(key, { message, raw });
    const row = fieldsNode.querySelector(`[data-field-key="${CSS.escape(key)}"]`);
    const error = row?.querySelector("[data-field-error]");
    if (error) error.textContent = message;
    const input = row?.querySelector("input");
    input?.classList.add("is-invalid");
    input?.setAttribute("aria-invalid", "true");
    updateChrome();
  }

  function clearInvalid(key) {
    if (!invalid.has(key)) return;
    invalid.delete(key);
    const row = fieldsNode.querySelector(`[data-field-key="${CSS.escape(key)}"]`);
    const error = row?.querySelector("[data-field-error]");
    if (error) error.textContent = "";
    const input = row?.querySelector("input");
    input?.classList.remove("is-invalid");
    input?.removeAttribute("aria-invalid");
    updateChrome();
  }

  function fieldLabel(field) {
    return field.label || field.key;
  }

  function buildDelegation(field) {
    const row = element("div", "config-item config-item-delegated");
    const head = element("div", "config-item-head");
    head.append(element("span", "config-item-label", fieldLabel(field)));
    head.append(element("span", "config-item-key", field.key));
    row.append(head);
    // 说明由 renderFields 统一渲染一次，避免重复 hint。
    if (!field.hint) {
      row.append(element("p", "config-hint", "该项由独立页面维护，此处不写入。"));
    }
    const button = element(
      "button",
      "btn",
      field.view === "safety" ? "打开内容安全" : "打开安全分组",
    );
    button.type = "button";
    button.dataset.navigate = field.view || "policies";
    row.append(button);
    return row;
  }

  function buildReadonly(field) {
    const row = element("div", "config-item");
    const head = element("div", "config-item-head");
    head.append(element("span", "config-item-label", fieldLabel(field)));
    head.append(element("span", "config-item-key", field.key));
    row.append(head);
    const values = Array.isArray(field.value) ? field.value.join("、") : String(field.value ?? "");
    row.append(element("p", "config-readonly", values || "（空）"));
    row.append(element("p", "config-hint", "该列表没有可选清单，需在配置文件中维护，本页不写入。"));
    return row;
  }

  function buildSecret(field) {
    const row = element("div", "config-item");
    const head = element("div", "config-item-head");
    const labelId = `setting-label-${field.key}`;
    const label = element("span", "config-item-label", fieldLabel(field));
    label.id = labelId;
    head.append(label);
    head.append(element("span", "config-item-key", field.key));
    row.append(head);
    row.append(element(
      "p",
      "config-secret-state",
      field.configured ? "已配置；留空表示保持不变" : "尚未配置",
    ));
    const input = element("input", "config-input");
    input.type = "password";
    input.autocomplete = "new-password";
    input.spellcheck = false;
    input.name = field.key;
    input.id = `setting-${field.key}`;
    input.setAttribute("aria-labelledby", labelId);
    input.value = hasOwn(draft, field.key) ? String(draft[field.key] ?? "") : "";
    // 已勾选清除时密码框从首次渲染起就不可输入，切组/搜索重渲同样保持。
    input.disabled = clears.has(field.key);
    input.addEventListener("input", () => commit(field.key, input.value));
    row.append(input);
    const clearLabel = element("label", "config-check");
    const clear = element("input");
    clear.type = "checkbox";
    clear.name = `clear-${field.key}`;
    clear.checked = clears.has(field.key);
    clear.addEventListener("change", () => {
      if (clear.checked) {
        clears.add(field.key);
        delete draft[field.key];
        input.value = "";
        input.disabled = true;
      } else {
        clears.delete(field.key);
        input.disabled = false;
      }
      updateChrome();
    });
    clearLabel.append(clear, element("span", "", "清除已保存的令牌"));
    row.append(clearLabel);
    return row;
  }

  function buildBool(field) {
    const row = element("div", "config-item");
    const head = element("div", "config-item-head");
    const labelId = `setting-label-${field.key}`;
    const label = element("span", "config-item-label", fieldLabel(field));
    label.id = labelId;
    head.append(label);
    head.append(element("span", "config-item-key", field.key));
    row.append(head);
    const control = createSwitch({
      id: `setting-${field.key}`,
      name: field.key,
      labelledBy: labelId,
      checked: valueOf(field) === true,
      onChange: (checked) => commit(field.key, checked),
    });
    customControls.push(control);
    row.append(control);
    return row;
  }

  function buildList(field) {
    const row = element("div", "config-item");
    const head = element("div", "config-item-head");
    head.append(element("span", "config-item-label", fieldLabel(field)));
    head.append(element("span", "config-item-key", field.key));
    row.append(head);
    const options = Array.isArray(field.options) ? field.options : [];
    const legacy = legacyListValues(field);
    const current = new Set(Array.isArray(valueOf(field)) ? valueOf(field) : []);
    const wrap = element("div", "config-options");
    const entries = [
      ...options.map((value) => ({ value, legacy: false })),
      ...legacy.map((value) => ({ value, legacy: true })),
    ];
    entries.forEach(({ value, legacy: isLegacy }, index) => {
      const label = element("label", "config-check");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.name = field.key;
      checkbox.value = value;
      checkbox.id = `setting-${field.key}-${index}`;
      checkbox.checked = current.has(value);
      checkbox.addEventListener("change", () => {
        const checked = [...wrap.querySelectorAll("input:checked")].map((item) => item.value);
        commit(field.key, collectListSelection(options, legacy, checked));
      });
      label.append(checkbox, element("span", "", isLegacy ? `${value}（已保存的旧值）` : value));
      wrap.append(label);
    });
    row.append(wrap);
    return row;
  }

  function buildChoice(field) {
    const row = element("div", "config-item");
    const head = element("div", "config-item-head");
    const labelId = `setting-label-${field.key}`;
    const label = element("span", "config-item-label", fieldLabel(field));
    label.id = labelId;
    head.append(label);
    head.append(element("span", "config-item-key", field.key));
    row.append(head);
    const control = createSelect({
      id: `setting-${field.key}`,
      name: field.key,
      labelledBy: labelId,
      value: String(valueOf(field) ?? ""),
      options: normalizeChoiceOptions(
        field.options,
        String(valueOf(field) ?? ""),
        field.special === "select_provider",
      ),
      onChange: (next) => commit(field.key, next),
    });
    customControls.push(control);
    row.append(control);
    return row;
  }

  function buildNumber(field) {
    const row = element("div", "config-item");
    const head = element("div", "config-item-head");
    const labelId = `setting-label-${field.key}`;
    const label = element("span", "config-item-label", fieldLabel(field));
    label.id = labelId;
    head.append(label);
    head.append(element("span", "config-item-key", field.key));
    row.append(head);
    const input = element("input", "config-input");
    input.type = "number";
    input.id = `setting-${field.key}`;
    input.name = field.key;
    input.setAttribute("aria-labelledby", labelId);
    const slider = field.slider || {};
    if (slider.min !== undefined) input.min = slider.min;
    if (slider.max !== undefined) input.max = slider.max;
    input.step = slider.step ?? (field.type === "int" ? "1" : "any");
    input.value = invalid.has(field.key)
      ? String(invalid.get(field.key).raw ?? "")
      : String(valueOf(field) ?? "");
    if (invalid.has(field.key)) {
      input.classList.add("is-invalid");
      input.setAttribute("aria-invalid", "true");
    }
    input.addEventListener("input", () => {
      const result = parseNumberDraft(input.value, field);
      if (!result.valid) {
        setInvalid(field.key, result.message, input.value);
        return;
      }
      clearInvalid(field.key);
      commit(field.key, result.value);
    });
    row.append(input);
    return row;
  }

  function buildText(field) {
    const row = element("div", "config-item");
    const head = element("div", "config-item-head");
    const labelId = `setting-label-${field.key}`;
    const label = element("span", "config-item-label", fieldLabel(field));
    label.id = labelId;
    head.append(label);
    head.append(element("span", "config-item-key", field.key));
    row.append(head);
    const isText = field.type === "text";
    const input = document.createElement(isText ? "textarea" : "input");
    input.className = "config-input";
    input.id = `setting-${field.key}`;
    input.name = field.key;
    input.setAttribute("aria-labelledby", labelId);
    if (isText) input.rows = 4;
    else input.type = "text";
    input.value = String(valueOf(field) ?? "");
    input.addEventListener("input", () => commit(field.key, input.value));
    row.append(input);
    return row;
  }

  function buildField(field) {
    if (field.delegated) return buildDelegation(field);
    if (field.editable === false) return buildReadonly(field);
    if (field.secret) return buildSecret(field);
    if (field.type === "bool") return buildBool(field);
    if (field.type === "list" && Array.isArray(field.options) && field.options.length) {
      return buildList(field);
    }
    if (field.type === "string" && Array.isArray(field.options) && field.options.length) {
      return buildChoice(field);
    }
    if (field.type === "int" || field.type === "float") return buildNumber(field);
    return buildText(field);
  }

  function renderFields() {
    const group = groupOf(activeKey);
    customControls = [];
    fieldsNode.replaceChildren();
    if (!group) {
      fieldsNode.append(element("div", "empty", "配置数据不可用，请重新载入。"));
      return;
    }
    const fields = filterSettingsFields(group.fields, query);
    if (!fields.length) {
      fieldsNode.append(element(
        "div",
        "empty",
        query.trim() ? `“${query.trim()}”在本分组没有匹配项` : "该分组没有可编辑项",
      ));
      return;
    }
    for (const field of fields) {
      const row = buildField(field);
      row.dataset.fieldKey = field.key;
      const error = element("p", "config-error");
      error.dataset.fieldError = "true";
      error.id = `setting-error-${field.key}`;
      error.setAttribute("role", "alert");
      if (invalid.has(field.key)) error.textContent = invalid.get(field.key).message;
      row.append(error);
      const describedBy = [error.id];
      if (field.hint) {
        const hint = element("p", "config-hint", field.hint);
        hint.id = `setting-hint-${field.key}`;
        row.append(hint);
        describedBy.push(hint.id);
      }
      if (field.warning) row.append(element("p", "config-warning", field.warning));
      const control = row.querySelector(
        "input:not([type=hidden]), textarea, .dd-trigger, button",
      );
      control?.setAttribute("aria-describedby", describedBy.join(" "));
      fieldsNode.append(row);
    }
    for (const key of invalid.keys()) {
      const row = fieldsNode.querySelector(`[data-field-key="${CSS.escape(key)}"]`);
      const error = row?.querySelector("[data-field-error]");
      if (error) error.textContent = invalid.get(key).message;
    }
    // 保存/读取期间重建的控件也必须处于冻结状态，结束后统一恢复。
    if (busy) freezeControls();
  }

  function ensureMobileSelect() {
    if (!mobileSlot || mobileSelect) return;
    mobileSelect = createSelect({
      id: "settingsGroupPicker",
      labelledBy: "settingsGroupPickerLabel",
      value: activeKey,
      options: groups.map((group) => ({ value: group.key, label: group.label })),
      onChange: (next) => selectGroup(next, true),
    });
    mobileSlot.replaceChildren(mobileSelect);
    if (busy) mobileSelect.setDisabled(true);
  }

  function freezableControls() {
    return [
      ...form.querySelectorAll("input, textarea, button"),
      ...document.querySelectorAll(
        "#settingsDiscardBtn, #settingsReloadBtn, #settingsSaveBtn",
      ),
    ];
  }

  function freezeControls() {
    for (const control of freezableControls()) {
      if (!busyStates.has(control)) busyStates.set(control, control.disabled);
      control.disabled = true;
    }
    for (const control of customControls) control.setDisabled(true);
    mobileSelect?.setDisabled(true);
  }

  function setBusy(value) {
    busy = value;
    if (value) {
      freezeControls();
    } else {
      for (const [control, wasDisabled] of busyStates) {
        if (control.isConnected) control.disabled = wasDisabled;
      }
      busyStates.clear();
      for (const control of customControls) control.setDisabled(false);
      mobileSelect?.setDisabled(false);
    }
    form.setAttribute("aria-busy", String(value));
  }

  async function load() {
    if (busy) return;
    loading = true;
    setBusy(true);
    status.textContent = "正在读取设置…";
    status.dataset.state = "busy";
    try {
      const snapshot = await bridge.apiGet("settings");
      if (snapshot?.success === false) throw new Error(snapshot.error || "设置读取失败");
      groups = Array.isArray(snapshot?.groups) ? snapshot.groups : [];
      if (!groups.length) throw new Error("设置数据异常，请重试。");
      draft = {};
      clears = new Set();
      invalid = new Map();
      if (!groupOf(activeKey)) activeKey = groups[0].key;
      loaded = true;
      saved = false;
      errorNotice = "";
      ensureMobileSelect();
      if (mobileSelect) {
        mobileSelect.setOptions(groups.map((group) => ({ value: group.key, label: group.label })));
        mobileSelect.value = activeKey;
      }
      renderNav();
      renderFields();
    } catch (error) {
      loaded = false;
      errorNotice = error?.message || "设置读取失败，请重试。";
      throw error;
    } finally {
      loading = false;
      setBusy(false);
      updateChrome();
    }
  }

  function focusFirstInvalid() {
    const key = invalid.keys().next().value;
    if (!key) return;
    const field = fieldOf(key);
    const group = field ? groups.find((item) => item.fields.includes(field)) : null;
    if (group && group.key !== activeKey) {
      pinned = true;
      activeKey = group.key;
      renderNav();
      renderFields();
    }
    const input = fieldsNode.querySelector(`[data-field-key="${CSS.escape(key)}"] input`);
    input?.focus();
    input?.scrollIntoView({ block: "center" });
  }

  async function save(event) {
    event?.preventDefault?.();
    if (busy) return;
    if (invalid.size) {
      status.textContent = `有 ${invalid.size} 项数值需要修正`;
      focusFirstInvalid();
      return;
    }
    const { patch, clearSecrets } = collectSettingsPatch(groups, draft, clears);
    const count = patchCount(patch, clearSecrets);
    if (!count) {
      status.textContent = "没有需要保存的修改";
      return;
    }
    saving = true;
    saved = false;
    errorNotice = "";
    setBusy(true);
    updateChrome();
    try {
      const result = await bridge.apiPost("settings/update", {
        groups: patch,
        clear_secrets: clearSecrets,
      });
      if (result?.success === false) throw new Error(result.error || "设置保存失败");
      for (const [groupKey, values] of Object.entries(patch)) {
        const group = groupOf(groupKey);
        for (const [key, value] of Object.entries(values)) {
          const field = group?.fields.find((item) => item.key === key);
          if (field) field.value = value;
        }
      }
      for (const key of clearSecrets) {
        const field = fieldOf(key);
        if (field) field.configured = false;
      }
      for (const groupKey of Object.keys(patch)) {
        const group = groupOf(groupKey);
        for (const field of group?.fields || []) {
          if (field.secret && hasOwn(patch[groupKey], field.key)) field.configured = true;
        }
      }
      draft = {};
      clears = new Set();
      invalid = new Map();
      saved = true;
      renderFields();
      notify("插件设置已保存");
    } catch (error) {
      errorNotice = `${error?.message || "设置保存失败"}；修改仍保留在页面中，可重试。`;
      notify(errorNotice, "error");
    } finally {
      saving = false;
      setBusy(false);
      updateChrome();
    }
  }

  async function discard() {
    if (busy) return;
    // 无效数字也是未保存的编辑，不能因为不进补丁就静默丢弃。
    if (!changedCount() && !invalid.size) return;
    const confirmed = await confirm({
      title: "放弃未保存的修改",
      message: invalid.size
        ? "当前有未保存的修改和需要修正的数值，放弃后恢复为已保存的值。"
        : "当前配置有未保存的修改，放弃后恢复为已保存的值。",
      confirmLabel: "放弃修改",
    });
    if (!confirmed) return;
    draft = {};
    clears = new Set();
    invalid = new Map();
    saved = false;
    errorNotice = "";
    renderFields();
    updateChrome();
  }

  async function reload() {
    if (busy) return;
    if (changedCount() || invalid.size) {
      const confirmed = await confirm({
        title: "重新载入设置",
        message: "重新载入会丢弃尚未保存的修改，继续吗？",
        confirmLabel: "重新载入",
      });
      if (!confirmed) return;
    }
    await load().catch(() => {});
  }

  function onSearch() {
    if (busy) return;
    query = searchInput?.value || "";
    pinned = false;
    errorNotice = "";
    if (query.trim()) activeKey = pickSearchGroup(groups, query, activeKey, false);
    if (mobileSelect && mobileSelect.value !== activeKey) mobileSelect.value = activeKey;
    renderNav();
    renderFields();
    updateChrome();
  }

  form.addEventListener("submit", save);
  searchInput?.addEventListener("input", onSearch);
  reloadButton.addEventListener("click", () => { reload(); });
  const discardButton = byId("settingsDiscardBtn");
  discardButton?.addEventListener("click", () => { discard(); });
  fieldsNode.addEventListener("click", (event) => {
    const button = event.target.closest("[data-navigate]");
    if (button) navigate(button.dataset.navigate);
  });

  updateChrome();
  return { load };
}
