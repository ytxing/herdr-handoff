# 005 后台轮询与协议超时

实现手动启动的 daemon、单个 A→B 任务轮询和 `protocol_ack_timeout=30s`、提醒次数配置。

依赖：001、002、004。

验收：期待 Agent 动作前若为 working，先执行 `herdr agent wait --until idle`；等待不计入退避；B 忘记 take/done 或 A 忘记 claim/accept 时发送固定 Prompt；达到重试上限后进入 timeout；daemon 不自动启动。
