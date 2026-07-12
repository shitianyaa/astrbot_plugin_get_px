# 签到卡片字体

- 字体：霞鹜文楷轻便版（LXGW WenKai Lite）Regular
- 上游版本：`v1.522`
- 上游项目：https://github.com/lxgw/LxgwWenKai-Lite
- 授权：SIL Open Font License 1.1，详见 `OFL.txt`
- 卡片文件：`LXGWWenKaiLite-GB2312.woff2`

卡片字体由官方 `LXGWWenKaiLite-Regular.ttf` 生成，保留 GB2312、ASCII 与常用中文标点，并移除 hinting 后转换为 WOFF2。这样可以在 AstrBot Chromium/T2I 渲染中稳定使用，同时控制插件体积。未包含的生僻字、扩展汉字和符号由 CSS 字体栈回退显示。
