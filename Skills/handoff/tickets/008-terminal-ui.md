# 008 终端看板

实现只读 `handoff ui` 表格，显示任务描述、Source/Target、状态、required action、状态进入时间、持续时间、Herdr 状态、下次重试和错误。

依赖：001、004、005。

验收：持续刷新；多客户端可同时读取；不解析 Agent 自然语言。
