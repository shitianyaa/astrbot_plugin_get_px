const byId = (id) => document.getElementById(id);

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function addOption(select, value, label = value) {
  const option = element("option", "", label);
  option.value = value;
  select.append(option);
}

function renderField(field) {
  if (field.delegated) {
    const row = element("div", "setting-delegation");
    row.append(element("div", "setting-field-copy", field.label));
    row.append(element("p", "setting-hint", field.hint || "由安全分组编辑器管理。"));
    const button = element("button", "", "打开安全分组");
    button.type = "button";
    button.dataset.navigate = field.view || "policies";
    row.append(button);
    return row;
  }

  const row = element("div", "setting-field");
  const label = element("label", "setting-field-copy", field.label);
  const name = `setting-${field.key}`;
  label.htmlFor = name;
  row.append(label);
  let input;
  if (field.secret) {
    const secretState = element("small", "setting-secret-state", field.configured ? "已配置；留空表示保持不变" : "尚未配置");
    row.append(secretState);
    input = element("input");
    input.type = "password";
    input.autocomplete = "new-password";
    input.spellcheck = false;
    input.name = field.key;
    input.id = name;
    input.dataset.settingType = "secret";
    row.append(input);
    const clearLabel = element("label", "setting-clear-secret");
    const clear = element("input");
    clear.type = "checkbox";
    clear.name = `clear-${field.key}`;
    clear.dataset.clearSecret = field.key;
    clearLabel.append(clear, element("span", "", "清除已保存的令牌"));
    row.append(clearLabel);
  } else if (field.type === "bool") {
    input = element("input");
    input.type = "checkbox";
    input.checked = field.value === true;
    input.dataset.settingType = "bool";
    input.name = field.key;
    input.id = name;
    const toggle = element("label", "setting-toggle");
    toggle.htmlFor = name;
    toggle.append(input, element("span", "", input.checked ? "已启用" : "已关闭"));
    input.addEventListener("change", () => { toggle.lastChild.textContent = input.checked ? "已启用" : "已关闭"; });
    row.append(toggle);
  } else if (field.type === "list" && Array.isArray(field.options) && field.options.length) {
    input = element("div", "setting-options");
    input.dataset.settingType = "list";
    input.dataset.settingKey = field.key;
    const selected = new Set(Array.isArray(field.value) ? field.value : []);
    field.options.forEach((value, index) => {
      const optionLabel = element("label", "setting-option");
      const checkbox = element("input");
      checkbox.type = "checkbox";
      checkbox.name = field.key;
      checkbox.value = value;
      checkbox.checked = selected.has(value);
      checkbox.id = `${name}-${index}`;
      optionLabel.htmlFor = checkbox.id;
      optionLabel.append(checkbox, element("span", "", value));
      input.append(optionLabel);
    });
    row.append(input);
  } else if (field.type === "string" && Array.isArray(field.options) && field.options.length) {
    input = element("select");
    input.name = field.key;
    input.id = name;
    input.dataset.settingType = "string";
    const options = [...field.options];
    if (field.value && !options.includes(field.value)) options.unshift(field.value);
    options.forEach((value) => addOption(input, value));
    input.value = field.value ?? "";
    row.append(input);
  } else if (field.type === "text") {
    input = element("textarea");
    input.name = field.key;
    input.id = name;
    input.dataset.settingType = "string";
    input.rows = 4;
    input.value = field.value ?? "";
    row.append(input);
  } else {
    input = element("input");
    input.name = field.key;
    input.id = name;
    input.dataset.settingType = field.type;
    if (field.type === "int" || field.type === "float") {
      input.type = "number";
      const slider = field.slider || {};
      if (slider.min !== undefined) input.min = slider.min;
      if (slider.max !== undefined) input.max = slider.max;
      input.step = slider.step ?? (field.type === "int" ? "1" : "any");
      input.value = field.value ?? field.default ?? 0;
    } else {
      input.type = "text";
      input.value = field.value ?? "";
    }
    row.append(input);
  }
  if (field.hint) row.append(element("p", "setting-hint", field.hint));
  return row;
}

function readField(field, root) {
  if (field.secret) {
    const clear = root.querySelector(`[data-clear-secret="${field.key}"]`);
    const value = root.querySelector(`[data-setting-type="secret"][name="${field.key}"]`).value;
    if (clear?.checked) return { changed: true, value: "" };
    return value ? { changed: true, value } : { changed: false };
  }
  if (field.delegated) return { changed: false };
  if (field.type === "list") {
    const values = [...root.querySelectorAll(`[data-setting-key="${field.key}"] input:checked`)].map((item) => item.value);
    const initial = Array.isArray(field.value) ? field.value : [];
    return {
      changed: JSON.stringify([...values].sort()) !== JSON.stringify([...initial].sort()),
      value: values,
    };
  }
  const input = root.querySelector(`[name="${field.key}"]`);
  if (!input) return { changed: false };
  const value = field.type === "bool"
    ? input.checked
    : field.type === "int" || field.type === "float"
      ? (input.value === "" ? null : Number(input.value))
      : input.value;
  return { changed: value !== field.value, value };
}

export function createSettingsPage({ bridge, notify, navigate }) {
  const form = byId("configSettingsForm");
  const groupsNode = byId("settingsGroups");
  const status = byId("settingsStatus");
  const saveButton = byId("settingsSaveBtn");
  const reloadButton = byId("settingsReloadBtn");
  let groups = [];
  let dirty = false;
  let busy = false;

  function render(snapshot) {
    groups = snapshot.groups || [];
    groupsNode.replaceChildren();
    for (const group of groups) {
      const panel = element("section", "settings-group");
      panel.append(element("h3", "", group.label));
      if (group.hint) panel.append(element("p", "settings-group-hint", group.hint));
      const fields = element("div", "settings-fields");
      group.fields.forEach((field) => fields.append(renderField(field)));
      panel.append(fields);
      groupsNode.append(panel);
    }
  }

  function updateDirty() {
    dirty = groups.some((group) => group.fields.some((field) => readField(field, form).changed));
    status.textContent = dirty ? "有未保存的修改" : "修改已恢复为已保存状态";
  }

  async function load() {
    if (busy) return;
    busy = true;
    setBusy(true);
    status.textContent = "正在读取设置…";
    try {
      await fetchAndRender();
      dirty = false;
      status.textContent = "设置已载入";
    } catch (error) {
      status.textContent = error?.message || "设置读取失败，请重试。";
      throw error;
    } finally {
      busy = false;
      setBusy(false);
    }
  }

  async function fetchAndRender() {
    const snapshot = await bridge.apiGet("settings");
    if (snapshot?.success === false) throw new Error(snapshot.error || "设置读取失败");
    render(snapshot || {});
  }

  function setBusy(value) {
    saveButton.disabled = value;
    reloadButton.disabled = value;
    form.setAttribute("aria-busy", String(value));
    form.querySelectorAll("input, textarea, select, button").forEach((control) => {
      control.disabled = value;
    });
  }

  async function save(event) {
    event.preventDefault();
    if (busy) return;
    const patch = {};
    for (const group of groups) {
      const values = {};
      for (const field of group.fields) {
        const result = readField(field, form);
        if (result.changed) values[field.key] = result.value;
      }
      if (Object.keys(values).length) patch[group.key] = values;
    }
    if (!Object.keys(patch).length) {
      status.textContent = "没有需要保存的修改";
      return;
    }
    busy = true;
    setBusy(true);
    status.textContent = "正在保存设置…";
    try {
      const result = await bridge.apiPost("settings/update", { groups: patch });
      if (result?.success === false) throw new Error(result.error || "设置保存失败");
      await fetchAndRender();
      dirty = false;
      status.textContent = "设置已保存。建议重载插件后确认新配置完整生效。";
      notify("插件设置已保存");
    } catch (error) {
      dirty = true;
      status.textContent = error?.message || "设置保存失败；修改仍保留在页面中。";
      notify(status.textContent, "error");
    } finally {
      busy = false;
      setBusy(false);
      groupsNode.querySelectorAll("[data-clear-secret]:checked").forEach((clear) => {
        const secret = form.querySelector(`[data-setting-type="secret"][name="${clear.dataset.clearSecret}"]`);
        if (secret) secret.disabled = true;
      });
    }
  }

  form.addEventListener("submit", save);
  groupsNode.addEventListener("click", (event) => {
    const button = event.target.closest("[data-navigate]");
    if (button) navigate(button.dataset.navigate);
  });
  groupsNode.addEventListener("input", updateDirty);
  groupsNode.addEventListener("change", (event) => {
    if (event.target.matches("[data-clear-secret]")) {
      const secret = form.querySelector(`[data-setting-type="secret"][name="${event.target.dataset.clearSecret}"]`);
      secret.disabled = event.target.checked;
      secret.value = "";
    }
    updateDirty();
  });
  reloadButton.addEventListener("click", async () => {
    if (dirty && !window.confirm("重新载入会丢弃尚未保存的修改，继续吗？")) return;
    await load().catch(() => {});
  });

  return { load };
}
