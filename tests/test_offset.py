"""测试 pixivpy_async search_illust / illust_ranking 的 offset 参数是否生效。

用法：
    python tests/test_offset.py <refresh_token>
    python tests/test_offset.py              # 自动从 data/cmd_config.json 读取
"""

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "data" / "cmd_config.json"
PROXY = "http://127.0.0.1:7897"


def load_token() -> str:
    if len(sys.argv) >= 2:
        return sys.argv[1]

    if CONFIG_PATH.is_file():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            token = cfg.get("pixiv_refresh_token", "")
            if token:
                print(f"[*] 从 {CONFIG_PATH} 读取 refresh_token")
                return token
        except (json.JSONDecodeError, OSError) as e:
            print(f"[!] 读取配置文件失败: {e}")

    print("[!] 未提供 refresh_token。用法: python tests/test_offset.py <token>")
    print("[!] 或确保 data/cmd_config.json 中存在 pixiv_refresh_token 字段")
    sys.exit(1)


async def test_offset():
    from pixivpy_async import AppPixivAPI

    token = load_token()
    print(f"[*] 代理: {PROXY}")

    # ── 登录 ──
    api = AppPixivAPI(proxy=PROXY)
    try:
        await api.login(refresh_token=token)
    except Exception as e:
        print(f"[✗] 登录失败: {e}")
        sys.exit(1)
    print("[✓] 登录成功")

    # ── 测试 search_illust offset ──
    TAG = "初音ミク"
    print(f"\n── 测试 search_illust(tag={TAG!r}) ──")

    try:
        resp0 = await api.search_illust(
            TAG,
            search_target="partial_match_for_tags",
            sort="date_desc",
            offset=0,
        )
        ids0 = [str(i["id"]) for i in (resp0.get("illusts") or [])]
        print(f"  offset=0   → {len(ids0)} 张: {ids0[:5]}{'...' if len(ids0) > 5 else ''}")
    except Exception as e:
        print(f"  [✗] offset=0 失败: {e}")
        sys.exit(1)

    try:
        PAGE_SIZE = len(ids0) if ids0 else 30
        resp1 = await api.search_illust(
            TAG,
            search_target="partial_match_for_tags",
            sort="date_desc",
            offset=PAGE_SIZE,
        )
        ids1 = [str(i["id"]) for i in (resp1.get("illusts") or [])]
        print(f"  offset={PAGE_SIZE} → {len(ids1)} 张: {ids1[:5]}{'...' if len(ids1) > 5 else ''}")
    except Exception as e:
        print(f"  [✗] offset={PAGE_SIZE} 失败: {e}")
        sys.exit(1)

    overlap = set(ids0) & set(ids1)
    if overlap:
        print(f"  [✗] 两页有重叠: {overlap}")
        sys.exit(1)
    else:
        print("  [✓] 两页无重叠，offset 参数生效！")

    # ── 测试 illust_ranking offset ──
    print("\n── 测试 illust_ranking(mode='week') ──")

    try:
        resp_r0 = await api.illust_ranking(mode="week", offset=0)
        ids_r0 = [str(i["id"]) for i in (resp_r0.get("illusts") or [])]
        print(f"  offset=0   → {len(ids_r0)} 张: {ids_r0[:5]}{'...' if len(ids_r0) > 5 else ''}")
    except Exception as e:
        print(f"  [✗] offset=0 失败: {e}")
        sys.exit(1)

    try:
        RANK_PAGE = len(ids_r0) if ids_r0 else 30
        resp_r1 = await api.illust_ranking(mode="week", offset=RANK_PAGE)
        ids_r1 = [str(i["id"]) for i in (resp_r1.get("illusts") or [])]
        print(f"  offset={RANK_PAGE} → {len(ids_r1)} 张: {ids_r1[:5]}{'...' if len(ids_r1) > 5 else ''}")
    except Exception as e:
        print(f"  [✗] offset={RANK_PAGE} 失败: {e}")
        sys.exit(1)

    overlap_r = set(ids_r0) & set(ids_r1)
    if overlap_r:
        print(f"  [✗] 两页有重叠: {overlap_r}")
        sys.exit(1)
    else:
        print("  [✓] 两页无重叠，offset 参数生效！")

    print("\n[✓] 全部测试通过！offset 参数可用于分页去重。")


if __name__ == "__main__":
    import asyncio
    asyncio.run(test_offset())
