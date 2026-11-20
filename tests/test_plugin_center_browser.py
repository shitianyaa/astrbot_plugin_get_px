"""管理中心真实浏览器交互回归：保存/读取冻结、草稿保留与后台刷新。

依赖本机已安装的 Python Playwright 与 Chromium；缺失时整组跳过，
CI 只跑其余静态与单元测试。页面通过本地 HTTP 提供，bridge 与后端数据
全部为测试内合成，不访问 AstrBot 宿主、网络、QQ 或真实用户配置。
"""

from __future__ import annotations

import functools
import http.server
import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect, sync_playwright  # noqa: E402

PAGE_DIR = Path(__file__).resolve().parents[1] / "pages" / "pluginCenter"

SETTINGS = {
    "success": True,
    "groups": [
        {
            "key": "pixiv_source",
            "label": "图源",
            "fields": [
                {
                    "key": "max_count",
                    "label": "单次最大数量",
                    "type": "int",
                    "hint": "测试用数值项",
                    "options": [],
                    "slider": {"min": 1, "max": 20, "step": 1},
                    "default": 3,
                    "value": 3,
                },
                {
                    "key": "p_coin_cost",
                    "label": "单次发图金币消耗",
                    "type": "int",
                    "hint": "测试用步长项",
                    "options": [],
                    "slider": {"min": 0, "max": 200, "step": 5},
                    "default": 20,
                    "value": 20,
                },
                {
                    "key": "preferred_image_source",
                    "label": "首选图片源",
                    "type": "string",
                    "hint": "",
                    "options": ["Lolicon", "Pixiv"],
                    "slider": None,
                    "default": "Lolicon",
                    "value": "Lolicon",
                },
                {
                    "key": "pixiv_refresh_token",
                    "label": "Pixiv refresh token",
                    "type": "string",
                    "hint": "留空保持",
                    "options": [],
                    "slider": None,
                    "secret": True,
                    "configured": True,
                },
            ],
        },
        {
            "key": "checkin_basic",
            "label": "签到基础",
            "fields": [
                {
                    "key": "checkin_enabled",
                    "label": "签到总开关",
                    "type": "bool",
                    "hint": "",
                    "options": [],
                    "slider": None,
                    "default": True,
                    "value": True,
                },
                {
                    "key": "checkin_ai_greeting_prompt",
                    "label": "AI 问候提示词",
                    "type": "text",
                    "hint": "",
                    "options": [],
                    "slider": None,
                    "default": "",
                    "value": "原始提示词",
                },
                {
                    "key": "checkin_hitokoto_categories",
                    "label": "一言类型",
                    "type": "list",
                    "hint": "测试用多选说明",
                    "options": ["全部", "动画", "文学"],
                    "slider": None,
                    "default": ["全部"],
                    "value": ["全部", "旧分类"],
                },
            ],
        },
        {
            "key": "runtime",
            "label": "运行参数",
            "fields": [
                {
                    "key": "request_timeout",
                    "label": "网络请求超时（秒）",
                    "type": "float",
                    "hint": "",
                    "slider": {"min": 5, "max": 120, "step": 5},
                    "default": 30,
                    "value": 30,
                    "options": [],
                }
            ],
        },
    ],
}

SAFETY = {
    "success": True,
    "global_safety_terms": ["示例词"],
    "keyword_match_mode": "宽松",
    "global_blacklisted_illust_ids": [],
    "safety_groups": [
        {
            "group_id": "g1",
            "name": "日常插画",
            "sessions": ["bot:GroupMessage:10001"],
            "allow_r18": False,
            "custom_terms": [],
        }
    ],
}

RESPONSES = {
    "settings": SETTINGS,
    "overview": {
        "success": True,
        "custom_term_count": 1,
        "blacklist_count": 0,
        "latest_backup_at": "",
    },
    "checkin-groups": {"success": True, "groups": []},
    "content-safety": SAFETY,
    "image-blacklist": {"success": True, "records": []},
    "checkin-members": {"success": True, "members": [], "total": 0},
    # 模拟旧服务端仍声明在线字体，当前页面不得读取或加载。
    "config": {
        "success": True,
        "font_source": "mirror",
        "font_url": "https://font.test/legacy.css",
        "font_family": "MiSans",
    },
    "cache-storage": {
        "success": True,
        "total_human": "0 B",
        "total_count": 0,
        "card_cache_human": "0 B",
        "card_cache_count": 0,
        "temp_download_human": "0 B",
        "temp_download_count": 0,
    },
}

BRIDGE_SCRIPT = """
window.__responses = %s;
window.__getHandlers = {};
window.__postHandlers = {};
window.__posts = [];
window.AstrBotPluginPage = {
  ready: () => Promise.resolve({ isDark: false }),
  getContext: () => ({ isDark: false }),
  onContext: (handler) => { handler({ isDark: false }); return () => {}; },
  apiGet: (endpoint, params) => (
    window.__getHandlers[endpoint]
      ? window.__getHandlers[endpoint](params)
      : Promise.resolve(window.__responses[endpoint] || { success: true })
  ),
  apiPost: (endpoint, body) => {
    window.__posts.push({ endpoint, body });
    return window.__postHandlers[endpoint]
      ? window.__postHandlers[endpoint](body)
      : Promise.resolve({ success: true });
  },
  download: () => Promise.resolve(),
  upload: () => Promise.resolve({ success: true }),
};
""" % json.dumps(RESPONSES, ensure_ascii=False)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args) -> None:  # noqa: D102 - 静默测试服务器日志
        pass

    def do_GET(self) -> None:  # noqa: D102 - 额外提供不透明源沙箱包装页
        if self.path.split("?")[0] == "/sandbox.html":
            body = (
                '<!doctype html><meta charset="utf-8"><title>sandbox</title>'
                '<iframe sandbox="allow-scripts" src="./index.html"'
                ' width="1200" height="800"></iframe>'
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def end_headers(self) -> None:  # noqa: D102 - 页面资源不缓存
        self.send_header("Cache-Control", "no-store")
        # 不透明源沙箱里的模块脚本需要 CORS 头，与真实宿主静态资源一致。
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()


@pytest.fixture()
def page() -> Iterator[object]:
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        functools.partial(_QuietHandler, directory=str(PAGE_DIR)),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(
                    ignore_default_args=["--hide-scrollbars"],
                )
            except Exception as exc:  # 已安装 Playwright 但缺少浏览器时跳过
                pytest.skip(f"Chromium 不可用：{type(exc).__name__}")
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            context.add_init_script(BRIDGE_SCRIPT)
            page = context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            try:
                page.goto(
                    f"http://127.0.0.1:{server.server_address[1]}/index.html",
                    wait_until="networkidle",
                )
                page.click('.nav-tabs [data-view="settings"]')
                page.wait_for_selector("#settingsGroups .config-item")
                yield page
                assert errors == []
            finally:
                context.close()
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _disabled(page, selector: str) -> bool:
    return page.eval_on_selector(selector, "element => element.disabled")


def _open_checkin_group(page) -> None:
    page.click('#settingsNav [data-group="checkin_basic"]')
    page.wait_for_selector("#setting-checkin_ai_greeting_prompt")


def _start_pending_update(page) -> None:
    page.evaluate(
        """() => {
          window.__postHandlers["settings/update"] = () => new Promise((resolve) => {
            window.__resolveUpdate = resolve;
          });
        }"""
    )


def _start_pending_settings_read(page) -> None:
    page.evaluate(
        """() => {
          window.__getHandlers["settings"] = () => new Promise((resolve) => {
            window.__resolveSettings = resolve;
          });
        }"""
    )


def test_save_freezes_draft_controls_and_restores_original_disabled_state(page):
    _open_checkin_group(page)
    page.fill("#setting-checkin_ai_greeting_prompt", "新的问候")
    page.click('#settingsNav [data-group="pixiv_source"]')
    page.wait_for_selector("#setting-max_count")
    _start_pending_update(page)
    page.click("#settingsSaveBtn")
    page.wait_for_function(
        "document.querySelector('#settingsStatus').textContent.includes('正在保存')"
    )

    for selector in (
        "#setting-max_count",
        "#setting-preferred_image_source .dd-trigger",
        "#setting-pixiv_refresh_token",
        '[name="clear-pixiv_refresh_token"]',
        "#settingsSearch",
        "#settingsGroupPicker .dd-trigger",
        "#settingsDiscardBtn",
        "#settingsReloadBtn",
        "#settingsSaveBtn",
    ):
        assert _disabled(page, selector), selector
    assert page.eval_on_selector_all(
        "#settingsNav [data-group]", "nodes => nodes.every((node) => node.disabled)"
    )

    page.evaluate("window.__resolveUpdate({ success: true })")
    page.wait_for_function(
        "document.querySelector('#settingsStatus').textContent.includes('设置已保存')"
    )
    assert not _disabled(page, "#setting-max_count")
    assert not _disabled(page, "#setting-preferred_image_source .dd-trigger")
    assert not _disabled(page, "#setting-pixiv_refresh_token")
    assert not _disabled(page, "#settingsSearch")
    assert not _disabled(page, "#settingsReloadBtn")
    assert not _disabled(page, "#settingsDiscardBtn")
    assert _disabled(page, "#settingsSaveBtn")
    assert page.eval_on_selector_all(
        "#settingsNav [data-group]", "nodes => nodes.every((node) => !node.disabled)"
    )
    page.click('#settingsNav [data-group="checkin_basic"]')
    assert page.input_value("#setting-checkin_ai_greeting_prompt") == "新的问候"


def test_failed_save_keeps_draft_and_restores_controls(page):
    page.click('#settingsNav [data-group="pixiv_source"]')
    page.wait_for_selector("#setting-max_count")
    page.fill("#setting-max_count", "5")
    _start_pending_update(page)
    page.click("#settingsSaveBtn")
    page.wait_for_function(
        "document.querySelector('#settingsStatus').textContent.includes('正在保存')"
    )
    assert _disabled(page, "#setting-max_count")

    page.evaluate(
        """window.__resolveUpdate({ success: false, error: "模拟保存失败" })"""
    )
    page.wait_for_function(
        "document.querySelector('#settingsStatus').textContent.includes('模拟保存失败')"
    )
    status = page.text_content("#settingsStatus")
    assert "修改仍保留" in status
    assert page.input_value("#setting-max_count") == "5"
    assert not _disabled(page, "#setting-max_count")
    assert not _disabled(page, "#settingsSaveBtn")


def test_failed_reload_keeps_secret_clear_disabled_state(page):
    page.click('#settingsNav [data-group="pixiv_source"]')
    page.wait_for_selector('[name="clear-pixiv_refresh_token"]')
    page.check('[name="clear-pixiv_refresh_token"]')
    assert _disabled(page, "#setting-pixiv_refresh_token")
    page.evaluate(
        """() => {
          window.__getHandlers["settings"] = () => Promise.resolve({
            success: false, error: "模拟读取失败",
          });
        }"""
    )
    page.click("#settingsReloadBtn")
    page.wait_for_selector("#confirmDialog[open]")
    page.click("#dialogConfirm")
    page.wait_for_function(
        "document.querySelector('#settingsStatus').textContent.includes('模拟读取失败')"
    )
    assert page.is_checked('[name="clear-pixiv_refresh_token"]')
    assert _disabled(page, "#setting-pixiv_refresh_token")
    assert not _disabled(page, '[name="clear-pixiv_refresh_token"]')
    page.uncheck('[name="clear-pixiv_refresh_token"]')
    assert not _disabled(page, "#setting-pixiv_refresh_token")


def test_topbar_refresh_keeps_unsaved_settings_draft(page):
    _open_checkin_group(page)
    page.fill("#setting-checkin_ai_greeting_prompt", "刷新后保留")
    page.click("#refreshBtn")
    page.wait_for_function(
        "document.querySelector('#refreshBtn').disabled === false"
        " && document.querySelector('#refreshBtn').textContent.includes('刷新数据')"
    )
    assert page.input_value("#setting-checkin_ai_greeting_prompt") == "刷新后保留"
    assert "未保存的修改" in page.text_content("#settingsStatus")


def test_background_refresh_keeps_safety_group_dialog_draft(page):
    page.evaluate(
        """() => {
          const original = window.__responses["content-safety"];
          window.__getHandlers["content-safety"] = () => new Promise((resolve) => {
            window.__resolveSafety = () => resolve(original);
          });
        }"""
    )
    page.click("#refreshBtn")
    page.click('.nav-tabs [data-view="policies"]')
    page.click('#policyList [data-edit-group="g1"]')
    page.wait_for_selector("#safetyGroupDialog[open]")
    page.fill("#safetyGroupName", "草稿名称")
    page.evaluate("window.__resolveSafety()")
    page.wait_for_function(
        "document.querySelector('#refreshBtn').disabled === false"
        " && document.querySelector('#refreshBtn').textContent.includes('刷新数据')"
    )
    assert page.eval_on_selector("#safetyGroupDialog", "dialog => dialog.open")
    assert page.input_value("#safetyGroupName") == "草稿名称"


def test_secret_clear_stays_disabled_across_rerender(page):
    page.click('#settingsNav [data-group="pixiv_source"]')
    page.wait_for_selector('[name="clear-pixiv_refresh_token"]')
    page.check('[name="clear-pixiv_refresh_token"]')
    assert _disabled(page, "#setting-pixiv_refresh_token")

    page.click('#settingsNav [data-group="checkin_basic"]')
    page.wait_for_selector("#setting-checkin_ai_greeting_prompt")
    page.click('#settingsNav [data-group="pixiv_source"]')
    page.wait_for_selector('[name="clear-pixiv_refresh_token"]')
    assert page.is_checked('[name="clear-pixiv_refresh_token"]')
    assert _disabled(page, "#setting-pixiv_refresh_token")

    page.fill("#settingsSearch", "留空")
    page.wait_for_selector('[name="clear-pixiv_refresh_token"]')
    assert page.is_checked('[name="clear-pixiv_refresh_token"]')
    assert _disabled(page, "#setting-pixiv_refresh_token")
    page.fill("#settingsSearch", "")

    page.uncheck('[name="clear-pixiv_refresh_token"]')
    assert not _disabled(page, "#setting-pixiv_refresh_token")


def test_settings_controls_expose_field_accessible_names(page):
    page.click('#settingsNav [data-group="pixiv_source"]')
    page.wait_for_selector("#setting-max_count")
    assert page.get_by_role("spinbutton", name="单次最大数量").count() == 1
    assert page.get_by_label("Pixiv refresh token").count() == 1
    assert page.eval_on_selector(
        "#setting-max_count",
        "node => node.getAttribute('aria-describedby')",
    ).startswith("setting-error-max_count")
    assert page.get_by_role("checkbox", name="清除已保存的令牌").count() == 1

    page.click('#settingsNav [data-group="checkin_basic"]')
    page.wait_for_selector("#setting-checkin_enabled")
    assert page.get_by_role("checkbox", name="签到总开关").count() == 1
    assert page.get_by_label("AI 问候提示词").count() == 1
    assert (
        page.eval_on_selector(
            "#settingsGroupPicker .dd-trigger",
            "node => node.getAttribute('aria-labelledby')",
        )
        == "settingsGroupPickerLabel"
    )


def test_invalid_number_counts_as_unsaved_edit_for_discard_and_reload(page):
    page.click('#settingsNav [data-group="pixiv_source"]')
    page.wait_for_selector("#setting-max_count")
    page.fill("#setting-max_count", "")
    page.wait_for_function(
        "document.querySelector('#settingsStatus').textContent.includes('需要修正')"
    )
    assert _disabled(page, "#settingsSaveBtn")

    page.click("#settingsDiscardBtn")
    page.wait_for_selector("#confirmDialog[open]")
    page.click('#confirmDialog button[value="cancel"]')
    page.wait_for_function("!document.querySelector('#confirmDialog').open")
    assert page.input_value("#setting-max_count") == ""

    page.click("#settingsDiscardBtn")
    page.wait_for_selector("#confirmDialog[open]")
    page.click("#dialogConfirm")
    page.wait_for_function("document.querySelector('#setting-max_count').value === '3'")
    assert page.get_attribute("#setting-max_count", "aria-invalid") is None

    page.fill("#setting-max_count", "")
    page.wait_for_function(
        "document.querySelector('#settingsStatus').textContent.includes('需要修正')"
    )
    page.click("#settingsReloadBtn")
    page.wait_for_selector("#confirmDialog[open]")
    page.click('#confirmDialog button[value="cancel"]')
    page.wait_for_function("!document.querySelector('#confirmDialog').open")
    assert page.input_value("#setting-max_count") == ""


def test_number_step_error_blocks_save_with_inline_message(page):
    page.click('#settingsNav [data-group="pixiv_source"]')
    page.wait_for_selector("#setting-p_coin_cost")
    page.fill("#setting-p_coin_cost", "22")
    page.wait_for_function(
        "document.querySelector('#setting-p_coin_cost').getAttribute('aria-invalid') === 'true'"
    )
    assert "5" in page.text_content('[data-field-key="p_coin_cost"] [data-field-error]')
    assert _disabled(page, "#settingsSaveBtn")

    page.fill("#setting-p_coin_cost", "25")
    page.wait_for_function(
        "document.querySelector('#setting-p_coin_cost').getAttribute('aria-invalid') === null"
    )
    assert not _disabled(page, "#settingsSaveBtn")
    page.click("#settingsSaveBtn")
    page.wait_for_function(
        "document.querySelector('#settingsStatus').textContent.includes('设置已保存')"
    )
    assert (
        page.evaluate("window.__posts.at(-1).body.groups.pixiv_source.p_coin_cost")
        == 25
    )


def test_tabs_use_roving_focus_and_arrow_keys(page):
    page.focus("#tab-ranking")
    page.keyboard.press("ArrowRight")
    assert page.evaluate("document.activeElement.id") == "tab-members"
    assert page.get_attribute("#tab-members", "aria-selected") == "true"
    assert page.get_attribute("#tab-members", "tabindex") == "0"
    assert page.get_attribute("#tab-ranking", "tabindex") == "-1"
    assert page.get_attribute("#tab-members", "aria-controls") == "membersView"
    assert page.eval_on_selector("#membersView", "node => node.hidden === false")
    assert page.eval_on_selector("#rankingView", "node => node.hidden === true")

    page.keyboard.press("End")
    assert page.evaluate("document.activeElement.id") == "tab-settings"
    page.keyboard.press("Home")
    assert page.evaluate("document.activeElement.id") == "tab-ranking"
    page.keyboard.press("ArrowLeft")
    assert page.evaluate("document.activeElement.id") == "tab-settings"

    page.click("#tab-settings")
    assert page.get_attribute("#tab-settings", "aria-selected") == "true"
    assert page.eval_on_selector("#settingsView", "node => node.hidden === false")


def test_skip_link_focuses_main_content(page):
    page.locator(".skip-link").focus()
    page.keyboard.press("Enter")
    assert page.evaluate("document.activeElement.id") == "mainContent"


def test_list_options_expose_group_name_and_keep_legacy_values(page):
    _open_checkin_group(page)
    assert page.get_by_role("group", name="一言类型").count() == 1
    assert page.get_by_role("checkbox", name="全部").count() == 1
    assert page.get_by_role("checkbox", name="动画").count() == 1
    assert page.get_by_role("checkbox", name="旧分类（已保存的旧值）").count() == 1
    described = page.eval_on_selector(
        '[data-field-key="checkin_hitokoto_categories"] .config-options',
        "node => node.getAttribute('aria-describedby')",
    ).split()
    assert described[0] == "setting-error-checkin_hitokoto_categories"
    assert "setting-hint-checkin_hitokoto_categories" in described
    assert (
        page.text_content("#setting-hint-checkin_hitokoto_categories")
        == "测试用多选说明"
    )
    assert (
        page.eval_on_selector(
            "#setting-checkin_hitokoto_categories-0",
            "node => node.getAttribute('aria-describedby')",
        )
        is None
    )

    page.check("#setting-checkin_hitokoto_categories-1")
    page.click("#settingsSaveBtn")
    page.wait_for_function(
        "document.querySelector('#settingsStatus').textContent.includes('设置已保存')"
    )
    assert page.evaluate(
        "window.__posts.at(-1).body.groups.checkin_basic.checkin_hitokoto_categories"
    ) == ["全部", "动画", "旧分类"]


def test_field_text_is_rendered_as_text_without_native_dialogs(page):
    native_dialogs: list[str] = []
    page.on(
        "dialog",
        lambda dialog: (native_dialogs.append(dialog.message), dialog.dismiss()),
    )
    _open_checkin_group(page)
    page.fill("#setting-checkin_ai_greeting_prompt", "草稿")
    payload = json.loads(json.dumps(SETTINGS))
    payload["groups"][1]["fields"][1]["label"] = (
        '<img src="x" onerror="window.__xss=1">'
    )
    payload["groups"][1]["fields"][1]["hint"] = "<script>window.__xss=2</script>"
    page.evaluate(
        "payload => { window.__getHandlers['settings'] = () => Promise.resolve(payload); }",
        payload,
    )
    page.click("#settingsReloadBtn")
    page.wait_for_selector("#confirmDialog[open]")
    page.click("#dialogConfirm")
    page.wait_for_function(
        "() => document.querySelector("
        "'[data-field-key=\"checkin_ai_greeting_prompt\"] .config-item-label'"
        ")?.textContent.includes('onerror')"
    )

    assert page.evaluate("window.__xss") is None
    assert (
        page.eval_on_selector(
            '[data-field-key="checkin_ai_greeting_prompt"] .config-item-label',
            "node => node.textContent",
        )
        == '<img src="x" onerror="window.__xss=1">'
    )
    assert (
        page.eval_on_selector(
            '[data-field-key="checkin_ai_greeting_prompt"] .config-hint',
            "node => node.textContent",
        )
        == "<script>window.__xss=2</script>"
    )
    assert (
        page.eval_on_selector_all(
            "#settingsView img, #settingsView script", "nodes => nodes.length"
        )
        == 0
    )
    assert not native_dialogs, f"页面不应弹出原生对话框: {native_dialogs}"


def test_opaque_iframe_keeps_settings_working_without_storage(page):
    base = page.url.rsplit("/", 1)[0]
    page.goto(f"{base}/sandbox.html", wait_until="networkidle")
    assert (
        page.evaluate("document.querySelector('iframe').getAttribute('sandbox')")
        == "allow-scripts"
    )
    frame = next(item for item in page.frames if "/index.html" in item.url)
    assert (
        frame.evaluate(
            "() => { try { localStorage.length; return 'ok'; }"
            " catch (error) { return error.name; } }"
        )
        == "SecurityError"
    )
    assert frame.evaluate("document.styleSheets.length") == 2
    assert (
        frame.evaluate("getComputedStyle(document.body).backgroundColor")
        != "rgba(0, 0, 0, 0)"
    )

    frame.locator('.nav-tabs [data-view="settings"]').click()
    frame.locator("#settingsGroups .config-item").first.wait_for()
    assert frame.locator("#settingsGroups .config-item").count() == 4
    frame.locator('#settingsNav [data-group="checkin_basic"]').click()
    frame.locator("#setting-checkin_ai_greeting_prompt").wait_for()
    assert frame.locator("#settingsGroups .config-item").count() == 3


@pytest.mark.parametrize(
    ("token", "accepted"),
    [
        ("application/json", True),
        (".json", True),
        ("image/png", False),
        ("text/plain", False),
    ],
)
def test_backup_file_input_only_accepts_json(page, token, accepted):
    tokens = page.eval_on_selector(
        "#importFile", "node => node.accept.split(',').map((item) => item.trim())"
    )
    assert (token in tokens) is accepted


def test_settings_heading_and_group_remain_visible_while_scrolling(page):
    page.set_viewport_size({"width": 1100, "height": 430})
    page.evaluate("""() => {
      document.querySelector('#settingsGroups').style.minHeight = '1500px';
    }""")
    page.wait_for_function("""() => {
      const nav = document.querySelector('.config-nav');
      return nav.scrollHeight > nav.clientHeight;
    }""")
    page.locator("#settingsView").evaluate(
        "el => Promise.all(el.getAnimations().map(animation => animation.finished))"
    )
    sidebar = page.locator(".config-nav")
    sticky_positions = []
    for scroll_y in (700, 10_000):
        page.evaluate("(y) => window.scrollTo(0, y)", scroll_y)
        toolbar = page.locator(".config-actionbar").bounding_box()
        shell = page.locator(".nav-shell").bounding_box()
        nav = sidebar.bounding_box()
        assert toolbar["y"] >= shell["y"] + shell["height"]
        assert nav["y"] >= toolbar["y"] + toolbar["height"]
        assert nav["y"] + nav["height"] <= 430
        sticky_positions.append(nav["y"])
        for selector in ("#settingsTitle", "#settingsGroupTitle", "#settingsSaveBtn"):
            expect(page.locator(selector)).to_be_in_viewport(ratio=1)
    assert sticky_positions[0] == pytest.approx(sticky_positions[1], abs=1)
    scroll_before = page.evaluate("scrollY")
    sidebar.evaluate("el => { el.scrollTop = el.scrollHeight; }")
    assert sidebar.evaluate("el => el.scrollTop") > 0
    assert page.evaluate("scrollY") == scroll_before


def test_settings_panel_keeps_width_across_long_and_short_groups(page):
    page.set_viewport_size({"width": 1100, "height": 900})
    page.add_style_tag(
        content="#settingsGroups:has(#setting-max_count) { min-height: 1400px; }"
    )
    panel = page.locator(".config-main")
    long_group = panel.bounding_box()
    assert page.evaluate("document.documentElement.scrollHeight > innerHeight")
    page.click('#settingsNav [data-group="runtime"]')
    page.wait_for_selector("#setting-request_timeout")
    assert page.evaluate("document.documentElement.scrollHeight <= innerHeight")
    short_group = panel.bounding_box()
    for key in ("x", "width"):
        assert short_group[key] == pytest.approx(long_group[key], abs=1)


@pytest.mark.parametrize("width", [1100, 390])
def test_settings_toolbar_does_not_move_with_scroll_or_status(page, width):
    page.set_viewport_size({"width": width, "height": 700})
    page.add_style_tag(content="#settingsGroups { min-height: 1400px; }")
    toolbar = page.locator(".config-actionbar")
    initial = toolbar.bounding_box()
    for scroll_y in (4, 500, 0):
        page.evaluate("(y) => scrollTo(0, y)", scroll_y)
        assert toolbar.bounding_box()["y"] == pytest.approx(initial["y"], abs=1)
    page.fill("#setting-max_count", "5")
    expect(page.locator("#settingsStatus")).to_contain_text("未保存")
    for key in ("y", "height"):
        assert toolbar.bounding_box()[key] == pytest.approx(initial[key], abs=1)
    page.click("#settingsSaveBtn")
    expect(page.locator("#settingsStatus")).to_contain_text("已保存")
    for key in ("y", "height"):
        assert toolbar.bounding_box()[key] == pytest.approx(initial[key], abs=1)


def test_ranking_switch_keeps_current_content_and_ignores_late_response(page):
    page.evaluate("""() => {
      window.__responses['checkin-groups'] = {success: true, groups:
        ['a', 'b', 'c'].map(group_id => ({
          group_id, group_name: '群 ' + group_id, platform: 'fixture'
        }))
      };
      window.__rankingWaiters = {};
      for (const endpoint of ['checkin-ranking', 'checkin-trend']) {
        window.__getHandlers[endpoint] = ({group_id}) =>
          new Promise(resolve => { window.__rankingWaiters[group_id + endpoint] = resolve; });
      }
      window.__finishRanking = (id, count) => {
        window.__rankingWaiters[id + 'checkin-ranking']({
          success: true, entries: [{rank: 1, username: '成员 ' + id, user_id: id, value: '00:00:01'}]
        });
        window.__rankingWaiters[id + 'checkin-trend']({
          success: true, trend: [{date: '2026-08-24', count}]
        });
      };
    }""")
    page.click("#tab-ranking")
    page.click("#refreshBtn")
    page.wait_for_function("Boolean(window.__rankingWaiters['acheckin-trend'])")
    page.evaluate("window.__finishRanking('a', 1)")
    heading = page.locator("#selectedGroupName")
    data = page.locator("#rankingData")
    chart = page.locator("#activityTrack .track-day")
    expect(heading).to_have_text("群 a")
    expect(data.get_by_text("成员 a")).to_be_visible()
    height = data.bounding_box()["height"]

    first_choice = page.locator('#groupList [data-group="b"]')
    first_choice.click()
    expect(first_choice).to_be_focused()
    expect(first_choice).to_have_attribute("aria-pressed", "true")
    expect(heading).to_have_text("群 a")
    expect(data.get_by_text("成员 a")).to_be_visible()
    expect(chart).to_have_attribute("aria-label", "2026-08-24，签到人数 1 人")
    expect(data).to_have_attribute("aria-busy", "true")
    assert data.bounding_box()["height"] == pytest.approx(height, abs=1)

    page.click('#groupList [data-group="c"]')
    page.evaluate("window.__finishRanking('c', 3)")
    expect(heading).to_have_text("群 c")
    expect(data.get_by_text("成员 c")).to_be_visible()
    expect(chart).to_have_attribute("aria-label", "2026-08-24，签到人数 3 人")
    page.evaluate("""async () => {
      window.__finishRanking('b', 2);
      await new Promise(requestAnimationFrame);
    }""")
    expect(heading).to_have_text("群 c")
    expect(data.get_by_text("成员 c")).to_be_visible()
    expect(chart).to_have_attribute("aria-label", "2026-08-24，签到人数 3 人")
    expect(data).to_have_attribute("aria-busy", "false")


def test_group_save_uses_authoritative_response_without_waiting_for_reload(page):
    page.click('.nav-tabs [data-view="policies"]')
    page.click('#policyList [data-edit-group="g1"]')
    page.fill("#safetyGroupName", "已保存名称")
    page.evaluate("""() => {
      const old = window.__responses['content-safety'];
      window.__getHandlers['content-safety'] = () => new Promise(resolve => {
        window.__resolveStaleSafety = () => resolve(old);
      });
      window.__postHandlers['content-safety/groups/save'] = body => new Promise(resolve => {
        window.__resolveGroupSave = () => resolve({ success: true, group: { ...body, name: '已保存名称' } });
      });
    }""")
    # 模拟已打开弹窗时到达的后台刷新，不穿过原生 modal 点击背景。
    page.evaluate("document.querySelector('#refreshBtn').click()")
    page.wait_for_function("typeof window.__resolveStaleSafety === 'function'")
    page.locator('#safetyGroupForm button[type="submit"]').click()
    assert page.get_attribute("#safetyGroupForm", "aria-busy") == "true"
    assert page.locator("#safetyGroupName").is_disabled()
    page.evaluate("window.__resolveGroupSave()")
    page.wait_for_function("!document.querySelector('#safetyGroupDialog').open")
    assert (
        page.locator('#policyList [data-edit-group="g1"] strong').inner_text()
        == "已保存名称"
    )
    assert "已保存" in page.locator("#toast").inner_text()
    assert page.locator('#policyList [data-edit-group="g1"]').evaluate(
        "element => element === document.activeElement"
    )
    page.evaluate("window.__resolveStaleSafety()")
    page.wait_for_function("!document.querySelector('#refreshBtn').disabled")
    assert (
        page.locator('#policyList [data-edit-group="g1"] strong').inner_text()
        == "已保存名称"
    )

    page.evaluate("""() => {
      window.__postHandlers['content-safety/groups/save'] = body => Promise.resolve({
        success: true, group: { ...body, group_id: 'g2' },
      });
      window.__postHandlers['content-safety/groups/remove'] = () => Promise.resolve({ success: true });
    }""")
    page.click("#policyAddBtn")
    page.fill("#safetyGroupName", "新增分组")
    page.locator('#safetyGroupForm button[type="submit"]').click()
    page.wait_for_function("!document.querySelector('#safetyGroupDialog').open")
    assert page.locator('#policyList [data-edit-group="g2"]').evaluate(
        "element => element === document.activeElement"
    )
    page.click('#policyList [data-edit-group="g2"]')
    page.click("#safetyGroupDelete")
    page.click("#dialogConfirm")
    page.wait_for_function("!document.querySelector('#safetyGroupDialog').open")
    assert page.locator('#policyList [data-edit-group="g2"]').count() == 0
    assert page.locator("#policyAddBtn").evaluate(
        "element => element === document.activeElement"
    )


def test_group_save_failure_keeps_editable_draft(page):
    page.click('.nav-tabs [data-view="policies"]')
    page.click('#policyList [data-edit-group="g1"]')
    page.fill("#safetyGroupName", "未提交的草稿")
    page.evaluate("""() => {
      window.__postHandlers['content-safety/groups/save'] = () => Promise.resolve({ success: false, error: '存储不可用' });
    }""")
    page.locator('#safetyGroupForm button[type="submit"]').click()
    page.wait_for_function(
        "document.querySelector('#safetyGroupError').textContent.length > 0"
    )
    assert page.locator("#safetyGroupDialog").is_visible()
    assert page.input_value("#safetyGroupName") == "未提交的草稿"
    assert page.locator("#safetyGroupName").is_enabled()


def test_number_buttons_follow_step_limits_and_restore_after_failed_save(page):
    count = page.locator("#setting-max_count")
    count_control = count.locator("..")
    minus, plus = count_control.locator("button").all()
    plus.click()
    assert count.input_value() == "4"
    page.fill("#setting-max_count", "1")
    assert minus.is_disabled()
    page.fill("#setting-max_count", "20")
    assert plus.is_disabled()
    assert not minus.is_disabled()

    fee = page.locator("#setting-p_coin_cost")
    fee.fill("0")
    fee.locator("..").locator("button").last.click()
    assert fee.input_value() == "5"

    _start_pending_update(page)
    page.click("#settingsSaveBtn")
    assert minus.is_disabled()
    assert plus.is_disabled()
    page.evaluate("window.__resolveUpdate({success:false,error:'存储不可用'})")
    page.wait_for_function(
        "document.querySelector('#settingsStatus').textContent.includes('存储不可用')"
    )
    assert not minus.is_disabled()
    assert plus.is_disabled()
    assert count.input_value() == "20"

    count.fill("")
    assert minus.is_disabled()
    assert plus.is_disabled()
    assert page.locator("#settingsSaveBtn").is_disabled()


def test_member_number_buttons_preserve_decimal_step_and_minimum(page):
    page.evaluate("""() => {
      window.__responses['checkin-members'] = { success: true, total: 1, members: [{
        user_id: '10001', username: '示例成员', coins: 0, affection: 1.23,
        total_days: 10, streak_days: 2
      }] };
    }""")
    page.click('.nav-tabs [data-view="members"]')
    page.locator('[data-edit-member="10001"]').click()
    assert page.get_by_role("spinbutton", name="金币", exact=True).count() == 1
    assert page.get_by_role("spinbutton", name="好感度", exact=True).count() == 1
    assert (
        page.locator("#memberCoins").locator("..").locator("button").first.is_disabled()
    )
    affection = page.locator("#memberAffection")
    affection.locator("..").locator("button").last.click()
    assert float(affection.input_value()) == 1.24
    affection.fill("-10")
    assert affection.locator("..").locator("button").first.is_disabled()
    page.click("#memberCancelBtn")
    page.wait_for_function("!document.querySelector('#memberDialog').open")
    assert page.evaluate("window.__posts.length") == 0


def test_member_pager_preserves_page_on_edit_and_resets_on_search(page):
    page.evaluate("""() => {
      const members = Array.from({length: 39}, (_, index) => ({
        user_id: String(index + 1), username: '成员 ' + (index + 1),
        coins: 10, affection: 0, total_days: 3, streak_days: 1
      }));
      window.__memberRequests = [];
      window.__getHandlers['checkin-members'] = ({query, limit, offset}) => {
        window.__memberRequests.push([query, limit, offset]);
        const matching = members.filter(member => member.username.includes(query));
        return {success: true, members: matching.slice(offset, offset + limit), total: matching.length};
      };
      window.__postHandlers['checkin-members/update'] = values => {
        const member = members.find(item => item.user_id === values.user_id);
        Object.assign(member, values);
        return {success: true, member};
      };
    }""")
    page.click("#tab-members")
    rows = page.locator("#memberList .member-row")
    status = page.locator("#memberPageStatus")
    prev = page.locator("#memberPrevPage")
    next_page = page.locator("#memberNextPage")
    expect(rows).to_have_count(20)
    expect(status).to_have_text("第 1 / 2 页")
    expect(prev).to_be_disabled()
    next_page.click()
    expect(rows).to_have_count(19)
    expect(status).to_have_text("第 2 / 2 页")
    expect(next_page).to_be_disabled()

    edit = page.locator('[data-edit-member="21"]')
    edit.click()
    page.fill("#memberCoins", "99")
    page.click("#memberSaveBtn")
    expect(edit).to_be_focused()
    expect(rows.first.locator(".member-metric strong").first).to_have_text("99")
    expect(status).to_have_text("第 2 / 2 页")
    prev.click()
    expect(rows).to_have_count(20)
    next_page.click()
    expect(rows).to_have_count(19)

    page.fill("#memberSearch", "成员 21")
    page.locator("#memberSearch").press("Enter")
    expect(rows).to_have_count(1)
    expect(page.locator("#memberPager")).to_be_hidden()
    page.click("#memberClearBtn")
    expect(rows).to_have_count(20)
    expect(status).to_have_text("第 1 / 2 页")
    assert page.evaluate("window.__memberRequests") == [
        ["", 20, 0],
        ["", 20, 20],
        ["", 20, 0],
        ["", 20, 20],
        ["成员 21", 20, 0],
        ["", 20, 0],
    ]


def test_pages_use_system_fonts_even_with_legacy_font_response(page):
    font_requests = []

    def track_font(request):
        if request.resource_type == "font" or request.url.startswith(
            "https://font.test/"
        ):
            font_requests.append(request.url)

    page.route("https://font.test/**", lambda route: route.abort())
    page.on("request", track_font)
    page.reload(wait_until="networkidle")
    page.click('#settingsNav [data-group="runtime"]')
    expect(page.locator("#setting-request_timeout")).to_be_visible()
    expect(page.get_by_text("界面字体线路")).to_have_count(0)
    assert page.evaluate("document.fonts.size") == 0
    assert font_requests == []
