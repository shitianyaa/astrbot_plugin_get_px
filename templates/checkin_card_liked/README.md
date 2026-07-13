# 签到卡片模板库

当前签到卡片分为一个运行时默认模板和三套正式上线的付费主题。

## 运行时默认

- [checkin_card_v2](../checkin_card_v2)：便签/暖纸画册风。
- 插件由 `checkin/card.py` 直接读取该目录中的 `index.html`、`style.css` 和本地字体。
- 默认模板的说明与预览见其目录内的 `README.md`。

## 可购买主题

| 编号 | 模板目录 | 名称 | 视觉方向 |
|------|----------|------|----------|
| `01` | [01_stellar_ticket](01_stellar_ticket) | 浅蓝 | 浅色列车票券、金色轨道线与观察窗 |
| `02` | [02_phantom_coop](02_phantom_coop) | 红黑 | 红黑白朋克 UI、斜切卡块与撕裂立绘窗 |
| `03` | [03_proxy_license](03_proxy_license) | 黄黑 | 深灰工业终端、黄黑警示条与战术 HUD |

## 目录约定

每套上线模板应完整包含：

- `index.html`
- `style.css`
- `_build_preview.py`
- `preview.png`
- `README.md`

`preview.html` 是 `_build_preview.py` 生成的本地中间文件，已加入 `.gitignore`，不随插件发布。

这些模板由签到主题目录注册，可通过 `/购买主题 <编号>` 解锁、通过 `/切换主题 <编号>` 使用。每套模板拥有独立的模板版本，切换后会使用新的签到图片缓存。

## 统一预览素材

所有模板使用 [preview_assets/artwork_3x4.jpg](preview_assets/artwork_3x4.jpg) 生成正式预览。该素材是同一张真实 Pixiv `3:4` 竖图，并配合统一的用户、签到奖励、金币余额和好感度数据，用于公平比较不同主题的排版与裁切效果。
