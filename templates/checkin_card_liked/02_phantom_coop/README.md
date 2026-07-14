# 02 · 红黑

正式上线模板。直接查看 `preview.png`，或运行构建脚本生成 `preview.html`。

## 视觉方向

- 主题名：**红黑**
- 参考气质：《女神异闻录 5》经典红黑白撞色朋克 UI、斜切卡块、拼贴勒索标题、撕裂立绘大边窗。
- 画布：`960 × 540`
- 功能分区：
  - 左侧红黑信息面板 — 斜切日期条、动态拼贴标题、契约人卡片、斜切白色对话框、红白黑斜切报酬模块。
  - 右侧撕裂立绘大边窗 — `clip-path` 不规则多边形分割，悬浮斜切署名条。

## 预览

```text
templates/checkin_card_liked/02_phantom_coop/preview.html
```

静态样例，不依赖 AstrBot / T2I。修改 `style.css` 后可运行：

```text
python templates/checkin_card_liked/02_phantom_coop/_build_preview.py
```

## 运行时使用

主题编号为 `02`。购买后使用 `/切换主题 02` 切换。
