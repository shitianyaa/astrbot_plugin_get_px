/**
 * 管理中心共享控件：自绘下拉与开关。
 *
 * 页面不使用原生 select 的展开层（系统绘制、圆角与选中色不可控），也不依赖
 * 浏览器 storage。下拉保留键盘 Home/End/↑/↓/Enter/Space/Escape/Tab、点击外部
 * 关闭与焦点归还；开关使用真实 checkbox + 独立轨道，跨浏览器与高对比度模式可辨。
 */

let openSelect = null;
let selectSeq = 0;

/** 规范化下拉选项：字符串值即标签，对象可带 disabled；保留显式空值。 */
export function normalizeSelectOptions(options) {
  const normalized = [];
  const seen = new Set();
  for (const item of Array.isArray(options) ? options : []) {
    const rawValue = typeof item === "string" ? item : item?.value;
    if (rawValue === undefined || rawValue === null) continue;
    const value = String(rawValue);
    if (seen.has(value)) continue;
    seen.add(value);
    const label = typeof item === "string"
      ? item
      : String(item?.label ?? item?.text ?? value);
    normalized.push({ value, label, disabled: item?.disabled === true });
  }
  return normalized;
}

/** 键盘移动后的选项下标：Home/End 直达，↑/↓ 到边界即停，不循环。 */
export function stepOptionIndex(index, length, key) {
  if (!Number.isInteger(length) || length <= 0) return -1;
  const current = Number.isInteger(index) && index >= 0 && index < length ? index : -1;
  if (key === "Home") return 0;
  if (key === "End") return length - 1;
  if (key === "ArrowDown") return current < 0 ? 0 : Math.min(current + 1, length - 1);
  if (key === "ArrowUp") return current < 0 ? length - 1 : Math.max(current - 1, 0);
  return current;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/**
 * 创建自绘下拉。返回根节点，暴露 value（读写，写入不触发 onChange）、
 * setOptions、setDisabled、onChange 与 input（隐藏表单字段，保证提交值完整）。
 */
export function createSelect({
  id = "",
  name = "",
  value = "",
  options = [],
  labelledBy = "",
  describedBy = "",
  disabled = false,
  onChange = null,
} = {}) {
  const root = element("div", "dd");
  if (id) root.id = id;
  selectSeq += 1;
  const listId = `${id || `dd-${selectSeq}`}-list`;

  let current = String(value ?? "");
  let items = normalizeSelectOptions(options);
  let changeHandler = typeof onChange === "function" ? onChange : null;
  let expanded = false;

  const trigger = element("button", "dd-trigger");
  trigger.type = "button";
  trigger.setAttribute("aria-haspopup", "listbox");
  trigger.setAttribute("aria-expanded", "false");
  trigger.setAttribute("aria-controls", listId);
  if (labelledBy) trigger.setAttribute("aria-labelledby", labelledBy);
  if (describedBy) trigger.setAttribute("aria-describedby", describedBy);

  const labelNode = element("span", "dd-label");
  const caret = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  caret.classList.add("dd-caret");
  caret.setAttribute("viewBox", "0 0 24 24");
  caret.setAttribute("aria-hidden", "true");
  caret.setAttribute("focusable", "false");
  const chevron = document.createElementNS("http://www.w3.org/2000/svg", "path");
  chevron.setAttribute("d", "m6 9 6 6 6-6");
  chevron.setAttribute("fill", "none");
  chevron.setAttribute("stroke", "currentColor");
  chevron.setAttribute("stroke-width", "2");
  chevron.setAttribute("stroke-linecap", "round");
  chevron.setAttribute("stroke-linejoin", "round");
  caret.append(chevron);
  trigger.append(labelNode, caret);

  const list = element("div", "dd-list");
  list.id = listId;
  list.setAttribute("role", "listbox");
  if (labelledBy) list.setAttribute("aria-labelledby", labelledBy);
  list.hidden = true;

  const hidden = name ? document.createElement("input") : null;
  if (hidden) {
    hidden.type = "hidden";
    hidden.name = name;
    hidden.value = current;
  }

  root.append(trigger, list);
  if (hidden) root.append(hidden);

  function labelOf(next) {
    const hit = items.find((item) => item.value === next);
    return hit ? hit.label : next;
  }

  function syncTrigger() {
    labelNode.textContent = labelOf(current);
  }

  function renderList() {
    list.replaceChildren(...items.map((item) => {
      const option = element("button", "dd-option");
      option.type = "button";
      option.tabIndex = -1;
      option.dataset.value = item.value;
      option.setAttribute("role", "option");
      option.setAttribute("aria-selected", item.value === current ? "true" : "false");
      option.textContent = item.label;
      if (item.disabled) {
        option.disabled = true;
        option.setAttribute("aria-disabled", "true");
      }
      option.addEventListener("click", () => choose(item.value));
      return option;
    }));
  }

  function optionNodes() {
    return [...list.querySelectorAll(".dd-option")];
  }

  function focusableOptions() {
    return optionNodes().filter((option) => !option.disabled);
  }

  function focusOption(index) {
    const options = focusableOptions();
    if (!options.length) return;
    const target = options[Math.max(0, Math.min(index, options.length - 1))];
    target.focus();
  }

  function focusSelected() {
    const options = focusableOptions();
    if (!options.length) return;
    const selected = options.findIndex((option) => option.dataset.value === current);
    focusOption(selected >= 0 ? selected : 0);
  }

  function setValue(next, notify) {
    const value = String(next ?? "");
    const changed = value !== current;
    current = value;
    syncTrigger();
    if (hidden) hidden.value = current;
    if (expanded) renderList();
    if (changed && notify && changeHandler) changeHandler(current);
  }

  function open() {
    if (expanded || trigger.disabled) return;
    if (openSelect && openSelect !== api) openSelect.close(false);
    renderList();
    list.hidden = false;
    expanded = true;
    root.classList.add("open");
    trigger.setAttribute("aria-expanded", "true");
    placeList();
    document.addEventListener("pointerdown", onDocumentPointerDown, true);
    openSelect = api;
    focusSelected();
  }

  // 矮屏/弹窗内按可用空间选向上或向下开，并把高度限制在可见区域内，
  // 避免选项被视口或 dialog 的滚动容器裁切。
  function placeList() {
    const triggerRect = trigger.getBoundingClientRect();
    const dialogRect = root.closest("dialog")?.getBoundingClientRect();
    const topEdge = dialogRect ? dialogRect.top : 0;
    const bottomEdge = dialogRect ? dialogRect.bottom : window.innerHeight;
    const above = triggerRect.top - topEdge - 8;
    const below = bottomEdge - triggerRect.bottom - 8;
    const wanted = Math.min(list.scrollHeight, 240);
    const up = below < wanted && above > below;
    root.classList.toggle("dd-up", up);
    list.style.maxHeight = `${Math.max(72, Math.min(240, up ? above : below))}px`;
  }

  function close(restoreFocus = false) {
    if (!expanded) return;
    expanded = false;
    list.hidden = true;
    root.classList.remove("open");
    trigger.setAttribute("aria-expanded", "false");
    document.removeEventListener("pointerdown", onDocumentPointerDown, true);
    if (openSelect === api) openSelect = null;
    if (restoreFocus) trigger.focus();
  }

  function choose(next) {
    const changed = String(next ?? "") !== current;
    setValue(next, false);
    close(true);
    if (changed && changeHandler) changeHandler(current);
  }

  function onDocumentPointerDown(event) {
    if (root.contains(event.target)) return;
    close(false);
  }

  trigger.addEventListener("click", (event) => {
    event.preventDefault();
    if (expanded) close(false);
    else open();
  });

  trigger.addEventListener("keydown", (event) => {
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      if (!expanded) open();
      const options = focusableOptions();
      const selected = options.findIndex((option) => option.dataset.value === current);
      if (event.key === "Home" || event.key === "End") {
        focusOption(stepOptionIndex(selected, options.length, event.key));
      } else if (selected < 0) {
        focusOption(event.key === "ArrowUp" ? options.length - 1 : 0);
      } else {
        focusOption(stepOptionIndex(selected, options.length, event.key));
      }
      return;
    }
    if (event.key === "Enter" || event.key === " " || event.key === "Spacebar") {
      event.preventDefault();
      if (expanded) close(true);
      else open();
      return;
    }
    if (event.key === "Escape" && expanded) {
      event.preventDefault();
      close(true);
    }
  });

  list.addEventListener("keydown", (event) => {
    const options = optionNodes();
    const index = options.indexOf(document.activeElement);
    if (event.key === "ArrowDown" || event.key === "ArrowUp" || event.key === "Home" || event.key === "End") {
      event.preventDefault();
      const next = stepOptionIndex(index, options.length, event.key);
      let cursor = next;
      while (cursor >= 0 && cursor < options.length && options[cursor].disabled) {
        cursor += event.key === "ArrowUp" || event.key === "End" ? -1 : 1;
      }
      if (cursor >= 0 && cursor < options.length) options[cursor].focus();
      return;
    }
    if (event.key === "Enter" || event.key === " " || event.key === "Spacebar") {
      event.preventDefault();
      const option = options[index];
      if (option && !option.disabled) choose(option.dataset.value);
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      close(true);
      return;
    }
    if (event.key === "Tab") {
      // 让浏览器继续移动焦点，但先收起列表，避免浮层盖住下一个控件。
      close(false);
    }
  });

  root.addEventListener("focusout", () => {
    // 焦点从触发按钮直接移到页面其它控件（如 Shift+Tab）时收起。
    setTimeout(() => {
      if (expanded && !root.contains(document.activeElement)) close(false);
    }, 0);
  });

  Object.defineProperty(root, "value", {
    get: () => current,
    set: (next) => setValue(next, false),
  });
  root.setOptions = (next) => {
    items = normalizeSelectOptions(next);
    if (!items.some((item) => item.value === current)) {
      current = items[0]?.value ?? "";
      if (hidden) hidden.value = current;
    }
    syncTrigger();
    if (expanded) renderList();
  };
  root.setDisabled = (next) => {
    const isDisabled = next === true;
    trigger.disabled = isDisabled;
    root.classList.toggle("is-disabled", isDisabled);
    if (hidden) hidden.disabled = isDisabled;
    if (isDisabled) close(false);
  };
  root.onChange = (handler) => {
    changeHandler = typeof handler === "function" ? handler : null;
  };
  root.setDisabled(disabled);

  const api = { open, close };
  syncTrigger();
  return root;
}

/**
 * 创建开关：真实 checkbox 负责值与表单提交，独立轨道只负责视觉。
 * 返回根节点，暴露 input、checked（读写）与 setDisabled。
 */
export function createSwitch({
  id = "",
  name = "",
  checked = false,
  labelledBy = "",
  describedBy = "",
  disabled = false,
  labelOn = "已启用",
  labelOff = "已关闭",
  onChange = null,
} = {}) {
  const root = element("label", "switch");
  const input = document.createElement("input");
  input.type = "checkbox";
  if (id) input.id = id;
  if (name) input.name = name;
  input.checked = checked === true;
  input.disabled = disabled;
  if (labelledBy) input.setAttribute("aria-labelledby", labelledBy);
  if (describedBy) input.setAttribute("aria-describedby", describedBy);

  const track = element("span", "switch-track");
  track.setAttribute("aria-hidden", "true");
  const text = element("span", "switch-text", input.checked ? labelOn : labelOff);

  root.append(input, track, text);
  input.addEventListener("change", () => {
    text.textContent = input.checked ? labelOn : labelOff;
    if (typeof onChange === "function") onChange(input.checked);
  });

  root.input = input;
  Object.defineProperty(root, "checked", {
    get: () => input.checked,
    set: (next) => {
      input.checked = next === true;
      text.textContent = input.checked ? labelOn : labelOff;
    },
  });
  root.setDisabled = (next) => {
    input.disabled = next === true;
    root.classList.toggle("is-disabled", next === true);
  };
  root.setDisabled(disabled);
  return root;
}
