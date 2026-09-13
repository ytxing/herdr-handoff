# Herdr Handoff Spec

## 目标

提供一个独立的本地后台服务，可靠地把 Source Agent 发给 Target Agent 的任务和结果收回来。服务不解析 Agent 的自然语言，只根据 Herdr 生命周期、handoff 命令和本地任务记录推进状态。

Herdr 负责 Agent、pane、状态查询和 Prompt 投递；handoff 负责任务记录、明确命令、计时和重试。

## 身份与任务记录

`send` 必须显式提供 Source 和 Target。服务不从调用上下文自动推导 Source。

每个 Agent 保存：

- `agent_name`：可选；
- `workspace_id`、`tab_id`、`pane_id`；
- Herdr terminal identity（用于确认当前对象）。

任务至少包含：

- `task_id`；
- `description`：必填，可多行，建议简短；
- 完整 `prompt` 或其文件路径；
- `source`、`target` Agent 信息；
- `state`、`required_action`；
- `state_since`、`last_prompt_at`、`next_prompt_at`、`retry_count`；
- `result_file`；
- `last_action`、`last_action_at`、`error`。

任务在 `send` 成功登记并成功投递后直接进入 `published`。不单独保存 `created`。

## 状态

任务状态：

```text
published
active
result_ready
reviewing
finished
changes_requested
rejected
timeout
stopped
cancelled
source_absent
target_absent
```

`required_action` 为：

```text
take | done | claim | accept | reply | none
```

Herdr 观测状态单独保存：

```text
presence: present | absent
lifecycle: working | idle | blocked | unknown | exited
```

`idle` 不表示任务完成。只有 `handoff done` 或 `handoff done --implicit-take` 产生正式结果。

## 命令

```text
handoff send
handoff take <task-id>
handoff progress <task-id>
handoff done <task-id> --result-file <path>
handoff done <task-id> --implicit-take --result-file <path>
handoff blocked <task-id> --reason <text>
handoff claim <task-id>
handoff accept <task-id>
handoff request-changes <task-id> --description <text>
handoff reject <task-id> --reason <text>
handoff stop <task-id>
handoff cancel <task-id>
handoff delete <task-id>
```

`progress` 不改变业务状态，只刷新执行计时。`reject` 只能由 Agent 明确执行，后台不能自行推断拒绝。`blocked` 保持任务为 `active`，并将 `required_action` 设为 `reply`；后台 Prompt Source 回复 B。
`delete` 永久删除任务记录及其已保存的结果文件。
`delete --state <state>` 删除指定状态的任务；`delete --all` 删除全部任务。

## 正常流程

```text
A: handoff send
服务: herdr agent prompt B
B: handoff take
B: 执行任务
B: handoff done
服务: 保存结果并 Prompt A
A: handoff claim
A: 检查结果
A: handoff accept
```

第一版只支持一个未结束的 A→B 任务，暂不支持批次或并发任务协调。

## Prompt 模板

### 发布给 B

```text
[HANDOFF TASK]

Task ID: <task-id>
Description: <description>
Source: <source-agent> / <source-pane>
Target: <target-agent> / <target-pane>

开始任何实际工作前，必须执行：
handoff take <task-id>

任务内容：
<prompt>

完成后执行：
handoff done <task-id> --result-file <path>

仍在处理时执行：
handoff progress <task-id>

明确不执行时才执行：
handoff reject <task-id> --reason "<reason>"
```

### B 忘记 take 且回到 idle

```text
[HANDOFF CHECK]

Task ID: <task-id>
Description: <description>

你曾经处于 working，但没有登记 handoff take。

如果已经执行并完成，请执行：
handoff done <task-id> --implicit-take --result-file <path>

如果还要继续执行，请执行：
handoff take <task-id>

只有明确不执行时，才执行：
handoff reject <task-id> --reason "<reason>"
```

### B 回到 idle 但没有 done

```text
[HANDOFF COMPLETION CHECK]

Task ID: <task-id>
Description: <description>

你已领取任务，但尚未提交完成结果。

如果已经完成，请执行：
handoff done <task-id> --result-file <path>

如果还在处理，请执行：
handoff progress <task-id>

如果遇到阻塞，请执行：
handoff blocked <task-id> --reason "<reason>"
```

### 通知 A 领取和验收

```text
[HANDOFF RESULT READY]

Task ID: <task-id>
Description: <description>
Result file: <result-file>

B 已提交任务结果。请执行：
handoff claim <task-id>

领取并检查后必须执行：
handoff accept <task-id>

需要修改时执行：
handoff request-changes <task-id> --description "<要求>"
```

## 计时与重试

协议确认统一使用：

```toml
protocol_ack_timeout = 30
protocol_ack_retries = 3
```

用于等待 `take`、`done`、`claim`、`accept` 或阻塞问题回复。

执行和验收使用指数退避：

```toml
execution_backoff_initial = 120
execution_backoff_max = 28800
review_backoff_initial = 120
review_backoff_max = 28800
```

默认序列为 2 分钟、4 分钟、8 分钟，最大 8 小时；所有值可配置。

## Working Agent 的等待规则

当后台期待某个 Agent 执行 `take`、`done`、`claim`、`accept` 或其他 handoff 命令时，先查询 Herdr 生命周期：

```text
Agent=working：
  执行 herdr agent wait <agent> --until idle
  等待期间不计算 execution/review backoff
  wait 返回后再发送对应 Prompt

Agent=idle：
  直接发送对应 Prompt

Agent=absent：
  标记 source_absent 或 target_absent
```

`agent wait` 只负责等待 Herdr 生命周期变化，不负责判断任务结果。wait 返回后仍必须等待 Agent 执行明确的 handoff 命令。

B 忘记 `take` 时：

- B 为 `working`：继续等待，不判定失败；
- B 回到 `idle`：发送检查 Prompt；
- B 执行 `done --implicit-take`：直接进入 `result_ready`；
- B 执行 `reject`：进入 `rejected`；
- 多次无响应：进入 `timeout`。

A 忘记 `claim` 或 `accept` 时，使用 `protocol_ack_timeout` 提醒；结果持续保存在本地。

B 执行 `blocked` 后，任务保持 `active`，`required_action = reply`。Source 使用 `handoff reply <task-id> --message ...` 回复；后台不解析回复内容。

## Herdr 适配

后台仅依赖：

```text
herdr agent get
herdr agent list
herdr agent prompt
```

第一版不使用 `herdr agent read`，不解析终端自然语言。

Target pane 移动或原 terminal identity 不再解析时，标记 `target_absent`；不自动迁移到其他 Agent。用户重新选择 Target 后重新 `send`。

## 后台与看板

后台手动运行：

```text
handoff daemon start
handoff daemon stop
handoff daemon status
```

不通过 Herdr startup hook 自动启动。

任务控制：

```text
handoff stop <task-id>    # 可恢复暂停
handoff cancel <task-id>  # 永久取消
```

看板 `handoff ui` 读本地状态，可作为普通终端程序，也可由 Herdr pane 启动。看板不只是展示，也可以直接操作任务：勾选后删除、或向 Target 重发已存 prompt，并可开关 daemon。至少显示：

```text
Task ID
Description
Source Agent / Pane
Target Agent / Pane
Current State
Required Action
State Since
State Duration
Herdr Presence / Lifecycle
Next Retry
Retry Count
Last Error
```

`State Duration = now - state_since`，不需要持久化。

## 第一条验收路径

```text
send → take → done → claim → accept
```

完成后再验证：B 忘记 take、B working 后忘记 done、A 忘记 claim、Agent absent、协议超时、执行退避、stop 和 cancel。
