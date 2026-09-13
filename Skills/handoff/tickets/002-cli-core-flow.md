# 002 CLI 核心闭环

实现 `send`、`take`、`done`、`done --implicit-take`、`claim`、`accept`。

依赖：001。

验收：完整执行 `send → take → done → claim`；命令幂等；`reject` 只能由显式命令触发。
