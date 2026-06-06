"""模拟翻页测试：连续搜索"蔚蓝档案"，验证分页游标推进逻辑。

用法：
    python tests/test_sim_page.py <refresh_token>
    python tests/test_sim_page.py                 # 自动从 data/cmd_config.json 读取
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CONFIG_PATH = ROOT / "data" / "cmd_config.json"
PROXY = "http://127.0.0.1:7897"

# 测试用临时 DB 目录
TEST_DIR = Path(tempfile.mkdtemp(prefix="test_page_cursor_"))
TEST_TOKEN_FILE = TEST_DIR / "token.txt"


def load_token() -> str:
    if len(sys.argv) >= 2:
        token = sys.argv[1]
        TEST_TOKEN_FILE.write_text(token)
        return token

    if CONFIG_PATH.is_file():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            token = cfg.get("pixiv_refresh_token", "")
            if token:
                print(f"[*] 从 {CONFIG_PATH} 读取 refresh_token")
                return token
        except (json.JSONDecodeError, OSError) as e:
            print(f"[!] 读取配置失败: {e}")

    print("[!] 用法: python tests/test_sim_page.py <refresh_token>")
    sys.exit(1)


async def run_simulation(token: str):
    from pixivpy_async import AppPixivAPI
    from image_index import ImageIndexStore, ordered_by_unused

    TAG = "蔚蓝档案"
    SCOPE = "group:test_123"
    SOURCE_KEY = f"search:{TAG.lower()}"

    print(f"测试标签: {TAG!r}")
    print(f"Scope   : {SCOPE}")
    print(f"代理    : {PROXY}")
    print(f"临时DB  : {TEST_DIR}")
    print()

    # —— 初始化 ——
    store = ImageIndexStore(TEST_DIR)

    api = AppPixivAPI(proxy=PROXY)
    await api.login(refresh_token=token)
    print("[✓] 登录成功\n")

    # —— 逐页模拟 ——
    total_fresh_global = set()
    total_used_global = set()

    for round_num in range(1, 6):  # 最多测 5 轮
        offset = await store.get_page_offset(SCOPE, SOURCE_KEY)

        # 模拟搜索
        resp = await api.search_illust(
            TAG, search_target="partial_match_for_tags",
            sort="date_desc", offset=offset,
        )
        illusts = list(resp.get("illusts") or [])
        if not illusts:
            print(f"[第{round_num}轮] offset={offset} → 无结果，分页到底！\n")
            # 重置游标（模拟 advance_page_offset 中 <20 的自动重置）
            if offset > 0:
                _ = await store.advance_page_offset(SCOPE, SOURCE_KEY, 0)
                print("         → 游标自动重置为 0\n")
            break

        ids = [str(i["id"]) for i in illusts]

        # 获取当天已用
        used_ids = await store.get_used_illust_ids(SCOPE, SOURCE_KEY)
        ordered = ordered_by_unused(illusts, used_ids)
        fresh_ids = [str(i["id"]) for i in ordered if str(i["id"]) not in used_ids]

        fresh_count = len(fresh_ids)
        used_count = len(ids) - fresh_count

        print(f"[第{round_num}轮] offset={offset:>4}  "
              f"返回={len(ids):>2}张  fresh={fresh_count:>2}  used={used_count:>2}  "
              f"游标推进={'是' if fresh_count == 0 else '否'}")

        total_fresh_global |= set(fresh_ids)
        total_used_global |= (set(ids) - set(fresh_ids))

        # 如有新鲜图 → 模拟"发送"，写入 image_usage
        if fresh_ids:
            for fid in fresh_ids:
                await store.record_usage(
                    scope=SCOPE, source_key=SOURCE_KEY,
                    illust_id=fid, feature="normal",
                )

        # 页面耗尽检测 → 推进游标
        if fresh_count == 0 and ids:
            new_offset = await store.advance_page_offset(
                SCOPE, SOURCE_KEY, len(ids)
            )
            print(f"         → 推进到 offset={new_offset}")

        # 间隔 3 秒
        if round_num < 5:
            print("         (等待 3s...)")
            await asyncio.sleep(3)

    print(f"\n{'='*50}")
    print(f"累计新鲜图: {len(total_fresh_global)} 张")
    print(f"累计已用过: {len(total_used_global)} 张")
    final_offset = await store.get_page_offset(SCOPE, SOURCE_KEY)
    print(f"最终游标  : {final_offset}")

    # 验证游标持久化
    store2 = ImageIndexStore(TEST_DIR)
    reloaded = await store2.get_page_offset(SCOPE, SOURCE_KEY)
    print(f"持久化验证: 重新读取游标 = {reloaded} {'✓' if reloaded == final_offset else '✗'}")

    # 验证 TTL 过期（模拟 4 天前最后访问）
    import sqlite3 as _sq
    from datetime import datetime as _dt, timedelta as _td
    from zoneinfo import ZoneInfo as _ZI
    _shanghai = _ZI("Asia/Shanghai")
    old_time = (_dt.now(_shanghai) - _td(days=4)).isoformat(timespec="seconds")
    with _sq.connect(store._db_path) as _conn:
        _conn.execute(
            "UPDATE source_page_cursor SET updated_at = ? WHERE scope = ? AND source_key = ?",
            (old_time, SCOPE, SOURCE_KEY),
        )
        _conn.commit()
    expired_offset = await store.get_page_offset(SCOPE, SOURCE_KEY)
    print(f"TTL 过期验证: 4天前游标={final_offset} → 过期后={expired_offset} "
          f"{'✓' if expired_offset == 0 else '✗ (应为0)'}")

    print("\n[✓] 模拟完成")


if __name__ == "__main__":
    token = load_token()
    try:
        asyncio.run(run_simulation(token))
    finally:
        import shutil
        if TEST_DIR.exists():
            shutil.rmtree(TEST_DIR, ignore_errors=True)
