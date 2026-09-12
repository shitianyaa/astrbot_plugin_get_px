from __future__ import annotations

import re
from pathlib import Path


PAGE_DIR = Path(__file__).resolve().parents[1] / "pages" / "pluginCenter"


def test_plugin_center_page_exposes_management_workspaces() -> None:
    html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
    assert "插件管理中心" in html
    assert 'data-view="ranking"' in html
    assert 'data-view="members"' in html
    assert 'data-view="safety"' in html
    assert 'data-view="policies"' in html
    assert 'id="policiesView"' in html
    assert 'id="policyAddDialog"' in html
    assert 'id="policyList"' in html
    assert 'id="policySearch"' in html
    assert 'data-view="data"' in html
    assert "群签到轨道" in html
    assert "签到成员数值" in html
    assert "内置安全词" in html
    assert "签到数据管理" in html
    assert "imageHistory" not in html
    assert "cacheStats" not in html
    assert "schema v6" in html
    for element_id in ("policyRuleModeStatus", "policyCustomTermInput", "policyCustomTermAddBtn", "policyCustomTermList", "policyIllustIdInput", "policyIllustIdAddBtn", "policyIllustIdList"):
        assert f'id="{element_id}"' in html
    assert html.index('id="policyBuiltinToggle"') < html.index('id="policyRuleModeStatus"')
    assert html.count('data-policy-field="custom_terms"') == 3
    assert html.count('data-policy-field="blacklisted_illust_ids"') == 3
    assert 'id="policyBatchStatus"' in html and 'aria-live="polite"' in html
    assert 'id="policyBatchStatus" class="policy-mode-status policy-batch-status" aria-live="polite" hidden' in html
    assert 'class="policy-rule-mode-group"' in html
    mode = html.index('id="policyRuleModeStatus"')
    custom = html.index('id="policyCustomTermTitle"')
    assert mode < custom < html.index('id="policyIllustIdTitle"')
    assert html.index('id="policyError"') < html.index('id="policyBatchStatus"') < html.index('class="policy-actions"')
    app = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    assert "async function applyPolicyField" in app
    assert "content-safety/policies/apply-field" in app
    assert "请先保存当前修改再批量应用。" in app
    assert "els.policyBatchStatus.hidden = !dirty" in app


def test_policy_editor_uses_compact_rule_mode_and_hidden_batch_status_styles() -> None:
    styles = (PAGE_DIR / "styles.css").read_text(encoding="utf-8")
    assert ".policy-rule-mode-group" in styles
    assert ".policy-rule-mode-group .policy-list-editor" in styles
    assert "overflow: hidden" in styles
    assert ".policy-batch-status { margin: 0; }" in styles


def test_plugin_center_import_accepts_json_backups_only() -> None:
    html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
    script = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    assert 'accept="application/json,.json"' in html
    assert "只能选择 JSON 备份文件。" in script
    assert "备份文件不能超过 5 MiB。" in script
    assert "恢复签到数据" in script
    assert "sqlite" not in html.lower()
    assert "sqlite" not in script.lower()


def test_plugin_center_uses_relative_bridge_endpoints() -> None:
    source = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    assert "window.AstrBotPluginPage" in source
    assert "bridge.ready()" in source
    assert 'bridge.download("checkin-export"' in source
    assert 'bridge.upload("checkin-import"' in source
    endpoints = re.findall(r'(?:apiGet|apiPost)\("([^"]+)"', source)
    assert endpoints
    assert all(not endpoint.startswith("/") for endpoint in endpoints)
    assert "image-history" not in source
    assert "cache_cleanup" not in source


def test_plugin_center_exposes_independent_group_safety_switches() -> None:
    html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
    source = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    assert 'data-view="policies" type="button">会话策略</button>' in html
    assert 'id="policyGroupScopeBtn"' in html and 'aria-pressed="true"' in html
    assert 'id="policyPrivateScopeBtn"' in html and 'aria-pressed="false"' in html
    assert 'id="policyCount"' in html
    assert 'id="policyEditorEmpty"' in html
    assert 'id="policyIdLabel"' in html
    assert 'class="policy-toggle-card"' in html
    assert 'id="policyAddTitle"' in html and 'id="policyAddLabel"' in html
    assert 'import * as policyState from "./policy-state.mjs"' in source

def test_policy_add_error_is_bound_before_add_handlers_use_it() -> None:
    source = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    mapping = source[source.index("const els = {"):source.index("};", source.index("const els = {"))]
    assert 'policyAddError: $("policyAddError")' in mapping
    assert source.index('policyAddError: $("policyAddError")') < source.index("els.policyAddError.textContent")

def _function_body(source, name):
    start = source.index(f"function {name}")
    depth = 0
    opened = False
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
            opened = True
        elif source[index] == "}":
            depth -= 1
            if opened and depth == 0:
                return source[start:index + 1]
    raise AssertionError("unterminated function")

def test_policy_state_module_drives_real_selection_and_crud_paths():
    source = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    reload_body = _function_body(source, "reloadPolicies")
    select_body = _function_body(source, "requestPolicySelection")
    add_body = _function_body(source, "addPolicy")
    save_body = _function_body(source, "savePolicy")
    delete_body = _function_body(source, "deletePolicy")
    assert "policyState.replacePolicyRecords(" in reload_body
    assert "policyState.selectPolicyRecord(" in select_body
    assert "policyState.upsertPolicyRecord(" in add_body
    assert "policyState.upsertPolicyRecord(" in save_body
    assert "policyState.discardPolicyDraft(bucket)" in save_body
    assert "policyState.removePolicyRecord(" in delete_body
    assert all("finally" in body for body in (reload_body, add_body, save_body, delete_body))

def test_policy_batch_uses_executable_state_helpers():
    source = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    body = _function_body(source, "applyPolicyField")
    assert "policyState.buildPolicyBatchRequest(" in body
    assert "policyState.policyBatchRefreshScopes(" in body
    assert "await reloadPolicies(refreshScope)" in body


def test_policy_switches_use_reachable_dirty_confirmation():
    source = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    scope_body = _function_body(source, "requestPolicyScopeChange")
    selection_body = _function_body(source, "requestPolicySelection")
    confirm_body = _function_body(source, "confirmPolicyDraftDiscard")
    assert "await confirmPolicyDraftDiscard(" in scope_body
    assert "await confirmPolicyDraftDiscard(" in selection_body
    assert "policyState.hasUnsavedPolicyDraft(" in confirm_body
    assert "policyState.discardPolicyDraft(" in confirm_body


def test_policy_render_updates_dynamic_copy_aria_and_busy_controls():
    source = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    body = _function_body(source, "renderPolicyManager")
    for token in (
        'setAttribute("aria-pressed"',
        "policyCount.textContent",
        "policyIdLabel.textContent",
        "policyAddTitle.textContent",
        "policyAddLabel.textContent",
        "policyEditorEmpty.hidden",
        "policyAddSubmit",
        'setAttribute("aria-busy"',
    ):
        assert token in body
    assert "control.disabled = busy" in body
    assert 'setAttribute("aria-busy", String(busy))' in body
    assert "busy || dirty || !draft" in body


def test_policy_frontend_has_no_legacy_mirror_or_contract_comments():
    source = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    for token in (
        "syncLegacyPolicyState",
        "Legacy endpoint contracts",
        "state.groupPolicies",
        "state.privatePolicies",
        "state.policyQuery",
        "state.policyDraft",
        "state.policySnapshot",
    ):
        assert token not in source


def test_policy_css_uses_sakura_tokens_and_responsive_actions():
    css = (PAGE_DIR / "styles.css").read_text(encoding="utf-8")
    assert css.count("{") == css.count("}")
    assert '.policy-scope-switch button[aria-pressed="true"]' in css
    assert ".policy-toggle-card" in css and ".policy-badge" in css
    for selector in ("#policyAddBtn", ".empty-action", "#policyAddSubmit", "#policySaveBtn", "#policyDeleteBtn"):
        assert selector in css
    assert "background: var(--primary);" in css
    assert "background: var(--primary-hover);" in css
    assert "color: #ffffff;" in css
    assert "font-size: 14px;" in css
    assert "font-weight: 700;" in css
    assert "line-height: 1.2;" in css
    assert "padding: 9px 15px;" in css
    assert 'id="policyDeleteBtn" class="danger"' not in (PAGE_DIR / "index.html").read_text(encoding="utf-8")
    assert "#policySearch,\n#policyGroupId,\n#policyAddInput" in css
    assert "border-radius: var(--radius-sm);" in css
    assert "#policyAddBtn,\n#policyAddSubmit,\n#policySaveBtn,\n#policyDeleteBtn" in css
    assert '.policy-toggle-card input[type="checkbox"]' in css
    assert "border-radius: 4px;" in css
    assert ".policy-toggle-card input[type=\"checkbox\"]:focus-visible" in css
    assert "@media (max-width: 900px)" in css
    mobile = css[css.rindex("@media (max-width: 620px)"):]
    assert "#policySaveBtn" in mobile and "#policyDeleteBtn" in mobile
    for undefined in ("--surface-muted", "--radius-pill", "--text-secondary", "--shadow-sm", "--radius-md"):
        assert undefined not in css
