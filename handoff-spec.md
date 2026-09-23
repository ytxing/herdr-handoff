# Herdr Handoff Spec

## 目标

提供一个独立的本地后台服务，可靠地把 Source Agent 发给 Target Agent 的任务和结果收回来。**状态推进只根据 Herdr 生命周期、handoff 命令和本地任务记录**；Jev（TypeSafe System One）的语义判断只用于两处观测层细化：判断到期的提醒是否该压住不发，以及按看板的显式请求对所有未结束任务给出完成度打分。Jev 不驱动任何状态迁移。

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
- `task_started_at`、`state_since`、`previous_node_started_at`、`last_prompt_at`、`next_prompt_at`、`retry_count`；
- `result_file`；
- `last_action`、`last_action_at`、`error`。

任务在 `send` 成功登记并成功投递后直接进入 `published`。`task_started_at` 在登记时固定；每次进入新状态时，
将进入当前状态前的 `state_since` 保存为 `previous_node_started_at`。旧数据库没有任务开始时间时，迁移使用其
现有的 `state_since` 作为回填值。

## 状态

任务状态：

```text
published
active
result_ready
finished
rejected
timeout
cancelled
source_absent
target_absent
```

`required_action` 为：

```text
take | done | claim | accept | none
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
handoff done <task-id> --result-file <path>
handoff done <task-id> --implicit-take --result-file <path>
handoff claim <task-id>
handoff reject <task-id> --reason <text>
handoff cancel <task-id>
handoff delete <task-id>
handoff clean invalid
handoff clean old [--days N]
```

`reject` 只能由 Agent 明确执行，后台不能自行推断拒绝。
需要 Source 回答时，不单设 `blocked`/`reply` 一轮往返：B 用 `done` 把已有结果交回、问题写进结果文件，Source 看完再发新任务。
因此 `active` 只有一个含义——Target 已领取、正在执行。
`delete` 永久删除任务记录及其已保存的结果文件。
`delete --state <state>` 删除指定状态的任务；`delete --all` 删除全部任务。

`clean` 是回收记录的命令，两个子命令都只作用于**终态任务**，未结束的任务一律不动：
`clean invalid` 删除无结果结束的任务（`target_absent`、`source_absent`、`timeout`、`cancelled`）；
`clean old` 删除终态且 `state_since` 已超过 N 天（默认 7，`--days` 可改）的任务，包含 `finished` 和 `rejected`。
终态不会再迁移，所以这些行的 `state_since` 就是任务结束的时刻。删除未结束的任务会让 Target 手里的 task id
在服务端消失，它随后的 `done` 会被当作未知任务拒绝，因此不在回收范围内。

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

同一个 Target 同一时间只能持有一个未结束任务（按 `target_pane` 判定）。不同 Target 之间互不阻塞；
暂不支持把一个批次拆给多个 Target 协调。

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

如果遇到阻塞，请执行：
```

### 通知 A 领取和验收

```text
[HANDOFF RESULT READY]

Task ID: <task-id>
Description: <description>
Result file: <result-file>

B 已提交任务结果。请检查结果后执行：
handoff claim <task-id> --pane <your-pane>

`claim` 完成 Source 的检查并结束任务；需要修改时重新发布一个任务。
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

**焦点抑制**：若待办一方所在的 pane 正处于用户焦点（Herdr `agent get` 的 `focused` 字段），本轮不发提醒也不消耗重试次数，等焦点移开后恢复。

理由：Herdr 只按终端标题推断状态（`| Working` / `| Ready`），**用户按 Esc 打断后与正常结束回合无法区分**。daemon 额外读取当前 `herdr agent read <pane> --source detection` 快照来判断是否该发提醒：

- 先查内置精确子串标记：命中 `Conversation interrupted`（Codex）或 `Request interrupted by user`（Claude）则本轮不发、不耗重试。已知标记是确定的免费答案，不问模型。
- 未命中标记且配置 `TYPESAFE_API_KEY` 时由 Jev 判断 `hold_off`（noul，概率 ≥ 0.5 则本轮不发、不耗重试）：用户主动结束了当前回合，**或快照显示 agent 正在推进这个任务**（包括它发起的后台命令/仿真仍在运行、它正在等待结果——这正是 Herdr 生命周期在回合结束后看不到的「还在干活」）。两种情形都不该催。发出的提醒永远是那条固定文案（待办动作 + 确切命令），Jev 只决定发不发。
- 未配置 key 或 Jev 调用失败时按普通提醒流程发送标准提醒（标记已经在前面检查过）。

使用 detection buffer 而不是完整 scrollback，避免旧中断记录一直压制后续提醒。读取失败时按普通提醒流程继续。

**状态词与完成比例（按需）**：看板按 `s` 触发一次全量评审。每个未结束任务问两道，答案互相独立、都不取整：

- `choice` 的 `state`：从二十一个状态词里挑一个，写入 `source_stage` / `target_stage`（与待办方一致）。**十二个「路上」的词**按先后排：unstarted / reading / exploring / planning / groundwork / output / working / first-pass / refining / verifying / concluding / done；**九个「不在路上」的词**：waiting / restarting / workaround / diagnosing / fixing / error / unreported / stuck / elsewhere——麻烦与停摆可以在任意位置出现，排进刻度就破坏了有序性。`choice` 不受 API 十级上限约束，词表按需要加。Herdr 自己就能报 `blocked`（识别各家的批准框提示），因此这个词不进 Jev 的词表，避免两个来源给出同一个词。
- `score` 的 `phase`：0–9 的位置，**按原样保存**。十级描述与状态词表**不共用文字**：状态说 agent 此刻在干什么，分数说这件事占整件事的比例，两者允许不一致（一个做完八成正卡在坑里的任务仍是八成）。

看板 PROCESS 列并排显示两者（如 `verifying 82.22%`），任一缺失时只显示另一个；这一格是读数不是命令，故不重复 SRC/DST 已有的 agent 名。主路状态按段位着色（蓝→青→黄→绿），岔路状态固定洋红，`stuck` 与 `error` 红色——红色的语义是「要人」：`error` 是屏幕上明摆着失败且无人处理，`stuck` 是无报错但也没有下一步。长程后台任务没有单独的 词，`waiting` 覆盖它（快照分不出一个命令要跑多久）。分数按原样保留，列上显示的是它占整条刻度的比例，保留两位。

全部任务在**同一次请求**里并发提问（`partial`：个别任务未作答不影响其余）；请求整体失败则不写任何分数。state 含每个任务的完整时间线（登记时间、当前节点与前一节点开始时间、上次动作、重试次数、两侧 lifecycle/presence、上一轮分数）加上该任务待办方 pane 的终端快照。

两个答案都不随 daemon 自动刷新：屏幕上的字来自一次显式按键，直到下一次评审或任务迁移状态为止；daemon 每轮只问 `hold_off`。这是观测信息，不参与状态推进。

任务需要 Source 回答时不走单独的协议状态：B 以 `done` 交回、把问题写进结果文件，Source 决定是发新任务还是接受。

## Herdr 适配

后台仅依赖：

```text
herdr agent get
herdr agent read
herdr agent list
herdr agent prompt
```

`herdr agent read --source detection` 只用于匹配当前快照中的中断标记：Codex 使用 `Conversation interrupted`，Claude 使用 `Request interrupted by user`。读取失败时按普通提醒流程继续。

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
handoff cancel <task-id>  # 永久取消（仅改本地记录）
```

`cancel` **只标记本地任务记录，不通知 Target、也不打断它**。正在执行的 Target 会继续做完当前这一轮，做完后若去执行 `done` 会被终态守卫拒绝（任务已是 `cancelled`）。
需要让 Target **立刻停下**时，由操作者手动打断：

```sh
herdr agent send-keys <target> esc      # esc 为规范键名，ctrl+c 亦可
```

## 范围变更的常规做法：cancel + 重发

范围调整时不必引入「取消中」这类中间态，直接取消旧任务、发一条收敛后的新任务即可——新提示词天然覆盖旧意图，Target 无需理解任何取消协议。

看板 `handoff ui` 读本地状态，可作为普通终端程序，也可由 Herdr pane 启动。看板不只是展示，也可以直接操作任务：勾选后删除、或向 Target 重发已存 prompt，并可开关 daemon。按键都是单次生效：`↑` `↓` 移动，`空格` 勾选，`a` 全选，`r` 重发，`s` 触发 Jev 完成度评审，`d` 删除勾选项，`t` 开关 daemon。`d` 作用于勾选的行；**批量回收是命令行的 `handoff clean`**，两套入口各管一种意图。删除类动作都先弹 `[y/N]` 确认。至少显示：

```text
Task ID
Description
Current State
Source Agent / Pane
Target Agent / Pane
Required Action
Task Start
Previous Node Start
State Since
State Duration
Herdr Presence / Lifecycle
Next Retry
Retry Count
Last Error
```

`State Duration = now - state_since`，不需要持久化。

欠着下一步的那一头的 agent 名做波效果：逐字符取 256 色灰阶（`PULSE_FLOOR`=244 到 `PULSE_TOP`=255 的三角波，再开平方，使名字大部分时间处在亮端、走过的是一道窄暗谷），后一个字符比前一个滞后 `HANDOFF_PULSE_LAG`（默认 0.1 个周期）；周期 `HANDOFF_PULSE_SECONDS` 默认 2.4 秒，看板重绘间隔 `HANDOFF_FRAME_SECONDS` 默认 0.15 秒。非 TTY 或 `NO_COLOR` 时不渲染。SRC/DST 的**名字按 agent 种类着色**：`AGENT_RGB` 以 RGB 给常见工具逐个指定（codex 为 `rgb(130,139,251)`，claude / gemini / cursor / kimi …），未知种类按名字做稳定散列落到 `AGENT_RGB_FALLBACK` 的同一组颜色，保证同名同色、跨进程不变。操作方那条名字在此基础上跑波：三角波取平方，使高光成为一条窄带（其余字符保持本色），逐级向白色混合（0 到 0.55 的白量，`PULSE_LEVELS` 级），并叠加下划线。**状态词保持 `STATUS_STYLE` 的原色**（idle/done 绿、working 黄、blocked 红）。**终态整行统一用 `38;5;240`**，名字、状态词、STATE 词一视同仁，比任何活跃行都暗；`rejected` / `timeout` / `*_absent` 等红色告警词保持原色，不参与压暗。行内其余部分只用颜色，不用粗体。
看板中的 `START` 是任务登记时间，`PREV` 是进入当前状态前一个节点的开始时间；首个节点没有 Prev 时，
`PREV` 回退显示 `START`。时间列使用紧凑的 `MM-DD HH:MM` 本地时间格式；任务按活跃态优先，再按当前节点
开始时间倒序排列。任务超出可视高度时，任务行区域最右侧显示低对比度轨道与滑块，窗口变窄时看板会依次收起路由、操作和时间列，保证表头、任务行与滚动条不超出终端宽度。

## 第一条验收路径

```text
send → take → done → claim
```

完成后再验证：B 忘记 take、B working 后忘记 done、A 忘记 claim、Agent absent、协议超时、执行退避和 cancel。
