# 签到卡片 V2 运行时模板

本目录是插件实际使用的 Jinja 模板。`checkin_card.py` 启动时读取 `index.html`，并将 `style.css` 注入 `/*__CHECKIN_CARD_CSS__*/`，因此渲染结果不依赖外部样式或网络资源。

## 视觉方向

- 固定 `960 × 540` 的“H · 丰富信息纸张画册”。
- 左侧约 `48%` 为签到信息，右侧约 `45%` 为固定竖向作品相框。
- 暖色纸张、轻微印刷纹理和克制的编辑排版；不使用全屏作品背景、紫色渐变或玻璃卡片。
- 作品使用 `object-fit: contain`，无作品时保留同尺寸纸张占位图。

## 数据区域

- `.card-header`: 日期、标题、最多两个徽标。
- `.identity` / `.greeting`: 用户身份、主次问候和角色署名。
- `.rewards`: 今日金币、好感度和连续签到。
- `.account-summary`: 累计签到、金币余额和关系等级。
- `.affection-progress`: 好感度进度、下一等级、下一纪念和可选加持状态。
- `.artwork-frame` / `.artwork-credit`: 竖向作品或占位图及 Pixiv 署名。

所有长文本都由 ViewModel 截断，并由 CSS 使用固定行数或省略号二次保护。
