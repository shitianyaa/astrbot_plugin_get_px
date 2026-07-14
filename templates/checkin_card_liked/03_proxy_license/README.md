# 03 · 黄黑

正式上线模板。直接查看 `preview.png`，或运行构建脚本生成 `preview.html`。

## 视觉方向

- 主题名：**黄黑**
- 参考气质：《绝区零》原生「绳网终端+录像媒介」世界观 UI 风格。
- 画布：`960 × 540`
- 特色：
  1. 水泥深灰噪点墙体背景，两边斜切黄黑警示条。
  2. CRT老显示器横向微细扫描线。
  3. 卡片与按钮使用半开口/缺口硬朗边框，体现工业感。
  4. 委托赏金根据功能严格分色（黄-丁尼，青-数据里程，玫红-空洞警告天数）。
  5. 拼排格式采用严格的 `ENGLISH // CHINESE` 双语义排版。
  6. 右侧立绘框重构为 **战术侦测窗口 (Tactical HUD Viewport)**，配以对角瞄准定位十字括号。

## 预览

```text
templates/checkin_card_liked/03_proxy_license/preview.html
```

静态样例，不依赖 AstrBot / T2I。修改 `style.css` 后可运行：

```text
python templates/checkin_card_liked/03_proxy_license/_build_preview.py
```

## 运行时使用

主题编号为 `03`。购买后使用 `/切换主题 03` 切换。
