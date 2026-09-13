# 008 终端看板

实现 `handoff ui` 表格，显示任务描述、Source/Target、状态、required action、状态进入时间、持续时间、Herdr 状态、下次重试和错误。

依赖：001、004、005。

验收：持续刷新；多客户端可同时读取；不解析 Agent 自然语言。

## 后续扩展：看板可写

原实现是只读的。后续在看板上加了勾选框和动作，使 `ui` 成为除 CLI 之外的第二条写路径。约束：

- 重发 prompt 只对 Herdr 状态为 `idle` 或 `done` 的 Target 生效，其余跳过并在页脚报告。Herdr 不会拒绝向 `working` 的 agent 投递，因此这个判断必须由看板自己做。
- 删除必须二次确认，一次确认删除全部勾选项。
- Source/Target 两列的状态直接向 herdr 查询（`herdr agent list` 一次调用覆盖全部 agent），不依赖 daemon 写入的 `*_lifecycle` 列——daemon 可能没在运行。
- 终端宽度不足时按阶梯收缩：SRC/DST 带状态 → 只留 agent 名 → 合并为单列 ROUTE → 丢 ACTION → 连路由也丢。再窄会溢出。实际下限是 43 列左右——由 `fixed()`（约 39 列）加上 DESCRIPTION 的 4 列保底宽度决定，并随状态标签与 AGE 的显示宽度浮动，不是固定值。
- SRC/DST 单元格格式为 `<agent 名（补白到该列最宽）> <状态词>`。名字补白是为了让状态词在列内对齐成一条竖线；只有状态词着色，名字不上色，让视线落在状态上。Herdr 查不到的 agent 显示为 `absent`。
- 看板底部两行常驻：倒数第二行是按键后的提示（无提示时留空占位），最后一行是快捷键条。留空占位是为了让看板不随按键上下跳动。
- **全板不使用粗体**，只靠颜色承载信息。`_CODES` 里不含 bold 系列码，写错的名字会静默不上色；`test_nothing_on_the_board_is_bold` 直接断言渲染出的转义码，防止将来重新引入。
- 状态配色：`published` 蓝 / `active` 青 / `result ready` 品红 / `finished` 绿 / `rejected`·`timeout`·`*_absent` 红 / `cancelled` 灰。agent 状态：`idle`·`done` 绿 / `working` 黄 / `blocked`·`absent` 红 / `unknown` 灰。
- 已终结的行整体压暗，但 SRC/DST 的状态词**保留颜色**，与 STATE 列行为一致。否则一条 `finished` 任务会呈现「STATE 带颜色、agent 状态却是灰」的观感，看起来像没上色。
