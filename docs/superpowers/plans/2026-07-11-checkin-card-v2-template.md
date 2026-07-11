# Check-in Card V2 Template Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create an isolated, directly viewable `960 × 540` check-in card prototype that validates the content hierarchy defined in the approved design.

**Architecture:** Keep the prototype under `templates/checkin_card_v2/` and do not connect it to `checkin.py`, `checkin_card.py`, or `main.py`. `index.html` contains fixed sample content, `style.css` owns all layout and visual rules, and `README.md` maps prototype regions to the future `CardViewModel` fields. A small structural test prevents required regions from disappearing before integration.

**Tech Stack:** HTML5, CSS3, Python `unittest`, browser screenshot rendering.

## Global Constraints

- Canvas size is exactly `960 × 540` with a `16:9` aspect ratio.
- The artwork and daily greeting are visually dominant; account numbers are secondary.
- The card displays at most three visually prominent translucent panels.
- Main greeting is at most two lines and `44` Chinese characters.
- Artwork credit remains readable on bright, dark, and complex backgrounds.
- Prototype files do not change existing runtime rendering behavior.
- No external font, script, image, or stylesheet dependency is required to open the prototype.

---

### Task 1: Standalone Template Contract

**Files:**
- Create: `tests/test_checkin_card_template_v2.py`
- Create: `templates/checkin_card_v2/index.html`
- Create: `templates/checkin_card_v2/style.css`
- Create: `templates/checkin_card_v2/README.md`

**Interfaces:**
- Consumes: `docs/superpowers/specs/2026-07-11-checkin-card-content-layout-design.md`.
- Produces: Standalone HTML regions `card-header`, `identity`, `greeting`, `rewards`, `growth`, and `artwork-credit` for later conversion into a Jinja template.

- [ ] **Step 1: Write the failing structural test**

```python
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "templates" / "checkin_card_v2"


class CheckinCardTemplateV2Test(unittest.TestCase):
    def test_required_regions_and_local_assets_exist(self):
        html = (TEMPLATE_DIR / "index.html").read_text(encoding="utf-8")
        css = (TEMPLATE_DIR / "style.css").read_text(encoding="utf-8")

        for region in (
            "card-header",
            "identity",
            "greeting",
            "rewards",
            "growth",
            "artwork-credit",
        ):
            self.assertIn(f'class="{region}', html)

        self.assertIn('href="./style.css"', html)
        self.assertIn("width: 960px", css)
        self.assertIn("height: 540px", css)
        self.assertIsNone(re.search(r'https?://', html + css))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test and verify the missing template failure**

Run: `python -m pytest tests/test_checkin_card_template_v2.py -q`

Expected: FAIL with `FileNotFoundError` for `templates/checkin_card_v2/index.html`.

- [ ] **Step 3: Create the semantic HTML skeleton**

```html
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>签到卡片 V2 初始模板</title>
  <link rel="stylesheet" href="./style.css" />
</head>
<body>
  <main class="stage">
    <article class="checkin-card">
      <div class="artwork-layer" aria-hidden="true"></div>
      <div class="readability-layer" aria-hidden="true"></div>

      <header class="card-header">
        <div>
          <p class="date">2026-07-11 · 星期六</p>
          <h1>今日签到</h1>
        </div>
        <div class="badges"><span>夏日相遇</span><span>连续 5 天</span></div>
      </header>

      <section class="identity">
        <div class="avatar">S</div>
        <div><strong>Sham1k0</strong><span>初次相遇</span></div>
      </section>

      <section class="greeting">
        <p class="eyebrow">NEKO'S DAILY NOTE</p>
        <blockquote>今天也有好好见面。<br />这张画，就当作我们共同留下的回忆。</blockquote>
        <p class="signature">— neko</p>
      </section>

      <section class="rewards">
        <div><span>金币</span><strong>+100</strong></div>
        <div><span>好感度</span><strong>+1.00</strong></div>
        <div><span>连续签到</span><strong>5 天</strong></div>
      </section>

      <section class="growth">
        <div class="growth-row"><span>好感度 66.60 · 亲近</span><strong>距离“信赖”还需 3.40</strong></div>
        <div class="progress"><i style="width: 83%"></i></div>
        <div class="milestone"><span>下一纪念：累计签到 30 天</span><strong>还差 18 天 · 金币 888</strong></div>
      </section>

      <footer class="artwork-credit">今日作品：Blue Sky / Someone · Pixiv 445566</footer>
    </article>
  </main>
</body>
</html>
```

- [ ] **Step 4: Create the initial visual system**

Create `style.css` with these exact structural rules, then refine spacing and color without changing the required class names:

```css
* { box-sizing: border-box; }
html, body { width: 100%; min-height: 100%; margin: 0; }
body { display: grid; place-items: center; background: #11151c; font-family: "Microsoft YaHei", "PingFang SC", sans-serif; }
.stage { width: 960px; height: 540px; }
.checkin-card { position: relative; width: 960px; height: 540px; overflow: hidden; color: #f8fafc; background: #172033; }
.artwork-layer { position: absolute; inset: 0; background: radial-gradient(circle at 76% 34%, rgba(255,213,170,.72), transparent 25%), linear-gradient(135deg, #38577d 0%, #8a6e7e 48%, #d6a66d 100%); }
.readability-layer { position: absolute; inset: 0; background: linear-gradient(90deg, rgba(10,16,28,.94) 0%, rgba(10,16,28,.72) 46%, rgba(10,16,28,.12) 76%, rgba(10,16,28,.28) 100%); }
.card-header, .identity, .greeting, .rewards, .growth, .artwork-credit { position: absolute; z-index: 1; }
.card-header { top: 28px; left: 32px; right: 32px; display: flex; justify-content: space-between; align-items: flex-start; }
.identity { top: 128px; left: 32px; display: flex; align-items: center; gap: 12px; }
.greeting { top: 210px; left: 32px; width: 480px; }
.rewards { top: 132px; right: 32px; display: grid; grid-template-columns: repeat(3, 1fr); width: 372px; }
.growth { left: 32px; right: 32px; bottom: 52px; padding: 14px 18px; border: 1px solid rgba(255,255,255,.18); border-radius: 16px; background: rgba(11,18,31,.58); backdrop-filter: blur(12px); }
.artwork-credit { right: 32px; bottom: 22px; max-width: 520px; font-size: 14px; color: rgba(255,255,255,.78); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
```

- [ ] **Step 5: Document the prototype mapping**

Create `README.md` containing:

```markdown
# 签到卡片 V2 初始模板

直接打开 `index.html` 查看静态原型。本目录不参与插件运行。

## 区域映射

- `.card-header`: `date_label`, `title`, `badges`
- `.identity`: `username`, `avatar_url`, `user_title`
- `.greeting`: `greeting`, `bot_name`, `secondary_note`
- `.rewards`: `coins_reward`, `affection_reward`, `streak_days`
- `.growth`: `affection_value`, `affection_level`, `affection_next_text`, `milestone_next_text`, `coins_total`
- `.artwork-credit`: `artwork_title`, `artwork_author`, `artwork_id`

## 限制

- 固定输出 `960 × 540`。
- 当前使用静态示例数据。
- 接入 Jinja 和签到数据前不得修改现有 `checkin_card.py`。
```

- [ ] **Step 6: Run the structural test**

Run: `python -m pytest tests/test_checkin_card_template_v2.py -q`

Expected: `1 passed`.

- [ ] **Step 7: Commit the standalone prototype**

```bash
git add templates/checkin_card_v2 tests/test_checkin_card_template_v2.py
git commit -m "Add check-in card v2 prototype"
```

### Task 2: Rendered Preview Verification

**Files:**
- Create: `docs/images/checkin-card-v2-template-preview.png`
- Modify: `templates/checkin_card_v2/README.md`

**Interfaces:**
- Consumes: `templates/checkin_card_v2/index.html` and `style.css`.
- Produces: A `960 × 540` visual artifact for user review before runtime integration.

- [ ] **Step 1: Open the local HTML in a browser at a `960 × 540` viewport**

Open the absolute `file:///.../templates/checkin_card_v2/index.html` URL. Disable viewport padding and capture only `.checkin-card`.

- [ ] **Step 2: Save the rendered preview**

Save the element screenshot to `docs/images/checkin-card-v2-template-preview.png`.

- [ ] **Step 3: Verify preview dimensions**

Run:

```python
from PIL import Image
image = Image.open("docs/images/checkin-card-v2-template-preview.png")
assert image.size == (960, 540), image.size
print(image.size)
```

Expected: `(960, 540)`.

- [ ] **Step 4: Add the preview link to the template README**

```markdown
## 预览

![签到卡片 V2](../../docs/images/checkin-card-v2-template-preview.png)
```

- [ ] **Step 5: Run verification**

Run: `python -m pytest tests/test_checkin_card_template_v2.py tests/test_checkin_card.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit the verified preview**

```bash
git add docs/images/checkin-card-v2-template-preview.png templates/checkin_card_v2/README.md
git commit -m "Add check-in card v2 preview"
```

