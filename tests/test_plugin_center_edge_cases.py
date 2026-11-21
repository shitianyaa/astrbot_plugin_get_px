"""管理中心并发读取、失败恢复与控件可达性的浏览器回归。"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from test_plugin_center_browser import BRIDGE_SCRIPT, page as page  # noqa: E402 - 复用浏览器 fixture


@pytest.mark.parametrize("action", ["page", "search"])
def test_failed_member_read_preserves_displayed_page_and_query(page, action):
    page.evaluate("""() => {
      window.__memberRequests = [];
      window.__getHandlers['checkin-members'] = params => {
        window.__memberRequests.push(params);
        if (window.__failNextMemberRead) {
          window.__failNextMemberRead = false;
          return new Promise((resolve, reject) => { window.__failMemberRead = reject; });
        }
        return Promise.resolve({ success: true, total: 60, members: [{
          user_id: String(params.offset + 1), username: '成员' + (params.offset + 1), coins: 0,
        }] });
      };
    }""")
    page.click("#tab-members")
    expect(page.locator("#memberPageStatus")).to_have_text("第 1 / 3 页")
    page.fill("#memberSearch", "原查询")
    page.locator("#memberSearch").press("Enter")
    page.wait_for_function("__memberRequests.at(-1).query === '原查询'")
    expect(page.locator("#memberNextPage")).to_be_enabled()
    page.click("#memberNextPage")
    expect(page.locator("#memberPageStatus")).to_have_text("第 2 / 3 页")
    page.evaluate("window.__failNextMemberRead = true")
    if action == "page":
        page.click("#memberNextPage")
    else:
        page.fill("#memberSearch", "新查询")
        page.locator("#memberSearch").press("Enter")
    page.wait_for_function("typeof __failMemberRead === 'function'")
    expect(page.locator("#memberList .member-identity strong")).to_have_text("成员21")
    page.evaluate("window.__failMemberRead(new Error('模拟成员读取失败'))")
    expect(page.locator("#memberPageStatus")).to_have_text("第 2 / 3 页")
    expect(page.locator("#memberList .member-identity strong")).to_have_text("成员21")
    page.click("#memberNextPage")
    expect(page.locator("#memberList .member-identity strong")).to_have_text("成员41")
    assert page.evaluate("window.__memberRequests.at(-1)") == {
        "query": "原查询", "limit": 20, "offset": 40,
    }


@pytest.mark.parametrize("pending_action", ["initial", "search", "page"])
def test_import_supersedes_pending_member_read(page, pending_action):
    page.evaluate(
        """pendingAction => {
      window.__memberRequests = [];
      window.__imported = false;
      const snapshot = coins => ({ success: true, total: pendingAction === 'page' ? 60 : 1, members: [{
        user_id: '1', username: '成员', coins, affection: 0, total_days: 1, streak_days: 1,
      }] });
      window.__getHandlers['checkin-members'] = params => {
        window.__memberRequests.push(params);
        if (window.__imported) return snapshot(99);
        if (pendingAction !== 'initial' && window.__memberRequests.length === 1) return snapshot(10);
        return new Promise(resolve => { window.__finishOldMembers = () => resolve(snapshot(10)); });
      };
      window.AstrBotPluginPage.upload = () => {
        window.__imported = true;
        return Promise.resolve({ success: true, users: 1 });
      };
    }""",
        pending_action,
    )
    page.click("#tab-members")
    if pending_action != "initial":
        expect(page.locator("#memberList .member-row")).to_have_count(1)
    if pending_action == "search":
        page.fill("#memberSearch", "成员")
        page.locator("#memberSearch").press("Enter")
    elif pending_action == "page":
        page.click("#memberNextPage")
    page.wait_for_function("typeof __finishOldMembers === 'function'")
    page.click("#tab-data")
    page.locator("#importFile").set_input_files(
        {
            "name": "backup.json",
            "mimeType": "application/json",
            "buffer": b"{}",
        }
    )
    page.click("#importBtn")
    page.click("#dialogConfirm")
    expect(page.locator("#importResult")).to_contain_text("恢复成功")
    page.wait_for_function(
        "__memberRequests.length === " + str(2 if pending_action == "initial" else 3)
    )
    expect(page.locator("#refreshBtn")).to_be_enabled()
    page.evaluate("window.__finishOldMembers()")
    page.click("#tab-members")
    expect(page.locator("#memberList .member-metric strong").first).to_have_text("99")
    requests = page.evaluate("window.__memberRequests")
    assert requests[-1] == {
        "query": "成员" if pending_action == "search" else "",
        "limit": 20,
        "offset": 20 if pending_action == "page" else 0,
    }


def test_saved_global_mode_survives_group_save_and_stale_reload(page):
    page.evaluate("""() => {
      window.__postHandlers['content-safety/global'] = () => Promise.resolve({ success: true });
      window.__getHandlers['content-safety'] = () => {
        const snapshot = structuredClone(window.__responses['content-safety']);
        snapshot.keyword_match_mode = '严格';
        return new Promise(resolve => { window.__finishModeRead = () => resolve(snapshot); });
      };
      window.__postHandlers['content-safety/groups/save'] = group => Promise.resolve({ success: true, group });
    }""")
    page.click("#tab-safety")
    page.click("#keywordMatchMode .dd-trigger")
    page.click('#keywordMatchMode [data-value="严格"]')
    page.wait_for_function("typeof __finishModeRead === 'function'")
    page.click("#tab-policies")
    page.click('#policyList [data-edit-group="g1"]')
    page.fill("#safetyGroupName", "保存后名称")
    page.locator('#safetyGroupForm button[type="submit"]').click()
    expect(page.locator("#safetyGroupDialog")).not_to_be_visible()
    expect(page.locator("#keywordMatchMode .dd-label")).to_have_text("严格")
    page.evaluate("window.__finishModeRead()")
    expect(page.locator("#keywordMatchMode .dd-trigger")).to_be_enabled()
    expect(page.locator('#policyList [data-edit-group="g1"] strong')).to_have_text(
        "保存后名称"
    )
    page.click("#tab-safety")
    expect(
        page.get_by_role("button", name="关键词过滤 严格", exact=True)
    ).to_be_visible()


@pytest.mark.parametrize("term_action", ["add", "remove"])
@pytest.mark.parametrize("group_action", ["save", "remove"])
def test_saved_global_terms_survive_group_change_and_stale_reload(
    page, term_action, group_action,
):
    page.evaluate("""() => {
      const server = structuredClone(window.__responses['content-safety']);
      window.__postHandlers['content-safety/terms/add'] = () => {
        server.global_safety_terms = ['ＡＢＣ', ...server.global_safety_terms];
        return Promise.resolve({ success: true, global_safety_terms: [...server.global_safety_terms] });
      };
      window.__postHandlers['content-safety/terms/remove'] = () => {
        server.global_safety_terms = [];
        return Promise.resolve({ success: true, global_safety_terms: [] });
      };
      window.__getHandlers['content-safety'] = () => {
        const snapshot = structuredClone(server);
        return new Promise(resolve => { window.__finishTermRead = () => resolve(snapshot); });
      };
      window.__postHandlers['content-safety/groups/save'] = group => {
        server.safety_groups = [structuredClone(group)];
        return Promise.resolve({ success: true, group: structuredClone(group) });
      };
      window.__postHandlers['content-safety/groups/remove'] = () => {
        server.safety_groups = [];
        return Promise.resolve({ success: true });
      };
    }""")
    page.click("#tab-safety")
    if term_action == "add":
        page.fill("#termInput", "ＡＢＣ")
        page.locator("#termForm button").click()
    else:
        page.locator('#customTerms [data-remove-term="示例词"]').click()
        page.click("#dialogConfirm")
    page.wait_for_function("typeof __finishTermRead === 'function'")
    expected = ["ＡＢＣ", "示例词"] if term_action == "add" else []
    expect(page.locator("#customTerms strong")).to_have_text(expected)
    page.click("#tab-policies")
    page.click('#policyList [data-edit-group="g1"]')
    if group_action == "save":
        page.fill("#safetyGroupName", "保存后名称")
        page.locator('#safetyGroupForm button[type="submit"]').click()
    else:
        page.click("#safetyGroupDelete")
        page.click("#dialogConfirm")
    expect(page.locator("#safetyGroupDialog")).not_to_be_visible()
    page.evaluate("window.__finishTermRead()")
    expect(page.locator("#termForm button")).to_be_enabled()
    if group_action == "save":
        expect(page.locator('#policyList [data-edit-group="g1"] strong')).to_have_text(
            "保存后名称"
        )
    else:
        expect(page.locator("#policyList [data-edit-group]")).to_have_count(0)
    page.click("#tab-safety")
    expect(page.locator("#customTerms strong")).to_have_text(expected)


def test_reopening_pending_settings_shares_failure_and_allows_retry(page):
    context = page.context.browser.new_context()
    context.add_init_script(
        BRIDGE_SCRIPT
        + """
      window.__settingsReads = 0;
      window.__getHandlers.settings = () => {
        if (++window.__settingsReads > 1) return Promise.resolve(window.__responses.settings);
        return new Promise((resolve, reject) => { window.__failFirstSettings = reject; });
      };
    """
    )
    try:
        fresh = context.new_page()
        fresh.goto(page.url.split("#")[0], wait_until="networkidle")
        fresh.click("#tab-settings")
        fresh.wait_for_function("typeof __failFirstSettings === 'function'")
        fresh.click("#tab-ranking")
        fresh.click("#tab-settings")
        assert fresh.evaluate("window.__settingsReads") == 1
        fresh.evaluate("window.__failFirstSettings(new Error('首次读取失败'))")
        expect(fresh.locator("#settingsStatus")).to_contain_text("首次读取失败")
        fresh.click("#tab-ranking")
        fresh.click("#tab-settings")
        expect(fresh.locator("#setting-max_count")).to_be_visible()
        assert fresh.evaluate("window.__settingsReads") == 2
    finally:
        context.close()


@pytest.mark.parametrize(
    "groups", [[], [{"key": "broken"}], [{"key": "broken", "fields": [None]}]]
)
def test_invalid_reload_keeps_previous_fields_editable(page, groups):
    page.evaluate(
        "groups => { window.__getHandlers.settings = () => ({ success: true, groups }); }",
        groups,
    )
    page.click("#settingsReloadBtn")
    expect(page.locator("#settingsStatus")).to_contain_text("设置数据异常")
    page.fill("#setting-max_count", "5")
    expect(page.locator("#settingsSaveBtn")).to_be_enabled()
    page.click("#settingsSaveBtn")
    expect(page.locator("#settingsStatus")).to_contain_text("设置已保存")
    assert page.evaluate("window.__posts.at(-1).body.groups") == {
        "pixiv_source": {"max_count": 5}
    }


def test_secret_replacement_leaves_only_configured_state_in_snapshot(page):
    page.fill("#setting-pixiv_refresh_token", "replacement-test-token")
    page.click("#settingsSaveBtn")
    expect(page.locator("#settingsStatus")).to_contain_text("设置已保存")
    expect(page.locator("#setting-pixiv_refresh_token")).to_have_value("")
    assert page.evaluate("""() => {
      const field = window.__responses.settings.groups[0].fields.find(field => field.secret);
      return field.configured && !Object.hasOwn(field, 'value');
    }""")


def test_saved_legacy_choice_can_be_restored_after_switching_groups(page):
    page.evaluate("""() => {
      window.__responses.settings.groups[0].fields.find(field => field.key === 'preferred_image_source').value = 'Legacy';
    }""")
    page.click("#settingsReloadBtn")
    select = page.locator("#setting-preferred_image_source")
    expect(select.locator(".dd-label")).to_contain_text("Legacy")
    select.locator(".dd-trigger").click()
    select.locator('[data-value="Pixiv"]').click()
    page.click('#settingsNav [data-group="checkin_basic"]')
    page.click('#settingsNav [data-group="pixiv_source"]')
    select.locator(".dd-trigger").click()
    select.locator('[data-value="Legacy"]').click()
    expect(page.locator("#settingsSaveBtn")).to_be_disabled()
    expect(page.locator("#settingsStatus")).to_contain_text("配置已同步")


def test_settings_group_switch_preserves_keyboard_focus(page):
    group = page.locator('#settingsNav [data-group="checkin_basic"]')
    group.focus()
    group.press("Enter")
    expect(group).to_be_focused()
    expect(page.locator("#setting-checkin_enabled")).to_be_visible()


@pytest.mark.parametrize("mode", ["宽松", "严格"])
def test_safety_reload_preserves_open_option_focus(page, mode):
    page.click("#tab-safety")
    page.click("#keywordMatchMode .dd-trigger")
    option = page.locator('#keywordMatchMode [data-value="宽松"]')
    expect(option).to_be_focused()
    page.evaluate(
        """mode => {
      window.__responses['content-safety'].keyword_match_mode = mode;
      document.querySelector('#refreshBtn').click();
    }""",
        mode,
    )
    expect(page.locator("#refreshBtn")).to_be_enabled()
    expect(option).to_be_focused()
    expect(page.locator(f'#keywordMatchMode [data-value="{mode}"]')).to_have_attribute(
        "aria-selected", "true"
    )
    option.press("Escape")
    expect(
        page.get_by_role("button", name=f"关键词过滤 {mode}", exact=True)
    ).to_be_focused()


def test_mobile_can_locate_invalid_field_hidden_by_search(page):
    page.set_viewport_size({"width": 390, "height": 700})
    page.fill("#setting-max_count", "")
    page.fill("#settingsSearch", "token")
    expect(page.locator("#setting-max_count")).to_have_count(0)
    page.click("#settingsGroupPicker .dd-trigger")
    expect(
        page.locator('#settingsGroupPicker [data-value="pixiv_source"]')
    ).to_contain_text("待修正")
    page.locator('#settingsGroupPicker [data-value="pixiv_source"]').press("Escape")
    page.click("#settingsFixInvalidBtn")
    expect(page.locator("#settingsSearch")).to_have_value("")
    expect(page.locator("#setting-max_count")).to_be_focused()
    page.fill("#setting-max_count", "5")
    expect(page.locator("#settingsSaveBtn")).to_be_enabled()
    expect(page.locator("#settingsFixInvalidBtn")).to_be_hidden()


def test_short_window_keeps_navigation_and_save_reachable(page):
    page.set_viewport_size({"width": 820, "height": 230})
    page.click('#settingsNav [data-group="runtime"]')
    page.fill("#setting-request_timeout", "35")
    page.click("#settingsSaveBtn")
    expect(page.locator("#settingsStatus")).to_contain_text("设置已保存")
