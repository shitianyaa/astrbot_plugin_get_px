# 签到卡片 V2 运行时模板

本目录是插件实际使用的 Jinja 模板。`checkin_card.py` 启动时读取 `index.html`，并将 `style.css` 注入 `/*__CHECKIN_CARD_CSS__*/`，因此渲染结果不依赖外部样式或网络资源。

![签到卡片 V2](../../docs/images/checkin-card-v2-template-preview.png)

## 视觉方向

- 固定 `960 × 540` 的“H · 丰富信息纸张画册”。
- 左侧约 `48%` 为签到信息，右侧约 `45%` 为固定竖向作品相框。
- 暖色纸张、轻微印刷纹理和克制的编辑排版；不使用全屏作品背景、紫色渐变或玻璃卡片。
- 中文字体使用本地内嵌的霞鹜文楷轻便版 GB2312 子集，标题、寄语和信息区保持统一，不依赖系统字体或外网字体服务。
- 作品使用 `object-fit: contain`，无作品时保留同尺寸纸张占位图。
- 作品选择固定目标比例为 `3:4`，容差 `20%`，只接受宽高比 `0.60–0.90`；分页耗尽后保持占位图，不切换布局。

## 数据区域

- `.card-header`: 日期、标题、最多两个徽标。
- `.identity` / `.greeting`: 用户身份、主次问候和角色署名。
- `.rewards`: 今日金币、好感度和连续签到。
- `.account-summary`: 累计签到、金币余额和关系等级。
- `.affection-progress`: 好感度进度、下一等级、下一纪念和可选加持状态。
- `.artwork-frame` / `.artwork-credit`: 竖向作品或占位图及 Pixiv 署名。

所有长文本都由 ViewModel 截断，并由 CSS 使用固定行数或省略号二次保护。

## 运行行为

- 首次签到把最终问候与作品信息写入当天记录；重复签到不重新发奖、不扣好感度、不重新选图，也不再次调用问候模型。
- 成品 JPEG 缓存保留一天，同一天重复签到优先重发缓存；缓存缺失、损坏或模板版本变化时按已保存记录重建。
- JPEG 清晰度由 `checkin_card_quality` 控制，范围 `60–100`、默认 `95`；质量会进入缓存键，修改配置后不会继续复用旧质量缓存。
- Pixiv 签到背景固定下载 `medium` 画质，不读取普通发图的 `image_quality`，避免为 `960 × 540` 卡片先下载数 MB 原图再降级。
- 法定节假日数据在首次安装、插件版本变化及距上次成功更新满 180 天时后台更新；网络失败使用旧缓存和内置公历/农历规则，不影响签到。
- 问候来源可选本地文案、一言或 AI，默认使用一言；一言正文最多 24 个字符，一言和 AI 请求失败时均回退本地文案。AI 模式优先使用 `checkin_ai_greeting_provider_id` 指定的文本模型，留空时尝试当前会话文本模型。
- 用户佩戴称号通过 ViewModel 的 `user_title` 注入；生日、全局事件、节假日、里程碑和连签按优先级生成主事件及单行次要备注。
- 对应配置为 `checkin_greeting_mode`、`checkin_hitokoto_timeout`、`checkin_ai_greeting_provider_id`、`checkin_ai_greeting_prompt`、`checkin_ai_greeting_timeout`。

## 字体授权

模板内置字体由霞鹜文楷轻便版 `v1.522` 生成，仅保留 GB2312、ASCII 和常用标点字符，并转换为 WOFF2。原字体采用 SIL Open Font License 1.1，来源与许可文本见 [`fonts/README.md`](fonts/README.md) 和 [`fonts/OFL.txt`](fonts/OFL.txt)。未覆盖字符会回退到渲染环境中的思源宋体或系统宋体。
