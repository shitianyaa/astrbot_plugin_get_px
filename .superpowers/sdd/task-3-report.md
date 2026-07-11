# Task 3 报告：H 纸张画册 ViewModel 与运行时模板

## RED

命令：

```powershell
python -m pytest tests/test_checkin_card.py tests/test_checkin_card_template_v2.py -q
```

结果：`4 failed, 3 passed`。

- 缺少 `build_checkin_card_view_model`。
- 旧 `build_checkin_card_data` 读取 profile 当前总量并暴露完整 `user_id`。
- 静态模板缺少 H 纸张画册必需区域。
- 运行时仍使用内嵌旧模板，没有从模板目录读取并注入本地 CSS。

## GREEN

- 新增冻结的 `CheckinCardViewModel`，覆盖设计文档字段契约。
- 快照总量只读取 `CheckinRecord.total_*_after` / `streak_days_after`。
- 保留持久化事件、问候、来源和次要说明；支持可选 `CheckinContent` 显式输入。
- 徽标最多两个；作品标题和作者分别按 18/12 字符安全省略。
- 计算下一好感度等级、下一累计签到里程碑和可选加持状态。
- 数据和模板不展示完整 UID。
- 运行时从 `templates/checkin_card_v2/` 读取 HTML/CSS，并替换 CSS marker；无外链。
- 模板改为固定 `960 × 540` 暖色纸张画册，左 `48fr`、右 `45fr`，24px 外边距，问候 28px，正文 18px，作品 `object-fit: contain`。

## 自审

- Correctness：普通快照、事件问候、徽标上限、截断、进度、里程碑、占位作品均有固定输出；Task 4 负责后续将旧图片编码器改为只读原图 Data URL。
- Readability：ViewModel 构建、展示文本计算、模板加载和图片编码职责分开；无遗留内嵌旧模板。
- Architecture：渲染层只接收显式输入和记录快照，不读取数据库或平台事件；模板只负责展示。
- Security：用户、事件、作品元数据进入模板数据前统一 HTML 转义；模板和 CSS 不含外部 URL；完整 UID 不进入数据字典。
- Performance：模板文件仅在模块导入时读取一次；构建过程为固定长度计算，无无界循环或新依赖。

## 验证

```text
focused: 7 passed, 2 warnings
full:    99 passed, 2 warnings
compile: python -m compileall -q checkin_card.py -> exit 0
diff:    git diff --check -> exit 0（仅 Git 的 LF/CRLF 提示）
```

两条 warning 均来自现有第三方 `jieba/pkg_resources`，本任务未新增 warning。

## 文件

- `checkin_card.py`
- `templates/checkin_card_v2/index.html`
- `templates/checkin_card_v2/style.css`
- `templates/checkin_card_v2/README.md`
- `tests/test_checkin_card.py`
- `tests/test_checkin_card_template_v2.py`
- `.superpowers/sdd/task-3-report.md`

## Commit

- 实现提交：`e7e36e8 Build H check-in card template`
