# 签到卡片 V2 初始模板

直接打开 `index.html` 查看静态原型。本目录不参与插件运行。

## 设计方向

- “暮色纪念明信片”，作品与每日问候优先于账户数字。
- 固定输出 `960 × 540`，无外部字体、脚本或网络资源。
- 当前背景使用 CSS 抽象占位图，接入时替换为实际 Pixiv 图片。

## 区域映射

- `.card-header`: `date_label`, `title`, `badges`
- `.identity`: `username`, `avatar_url`, `user_title`
- `.greeting`: `greeting`, `bot_name`, `secondary_note`
- `.rewards`: `coins_reward`, `affection_reward`, `streak_days`
- `.growth`: `affection_value`, `affection_level`, `affection_next_text`, `milestone_next_text`, `coins_total`
- `.artwork-credit`: `artwork_title`, `artwork_author`, `artwork_id`

## 限制

- 当前使用静态示例数据。
- 这里只验证信息层级和视觉方向。
- 接入 Jinja 和签到数据前不得修改现有 `checkin_card.py`。
