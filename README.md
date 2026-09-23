# Herdr Handoff

独立的单任务 A→B 交接服务。Herdr 负责 Agent 状态和 Prompt 投递；本项目保存任务状态、结果文件、超时和提醒。第一版支持 macOS 和 Linux，并可供不同 Agent harness 使用。

## 快速使用

```sh
cd herdr-task-handoff
./install-global.sh
handoff --help

./handoff daemon start
./handoff send --source-pane w1:p1 \
  --target-pane w1:p4 \
  --description "修复 footer 窄屏重叠" --prompt "检查并修复布局"
./handoff ui
```

B 按 Prompt 执行：

```sh
./handoff take <task-id>
./handoff done <task-id> --result-file result.md
```

长任务不必占着这一轮：后台跑，过程中用一行话报到。

A 领取并验收：

```sh
./handoff claim <task-id>
./handoff claim <task-id> --pane <your-pane>
```

删除任务及其已保存结果：

```sh
./handoff delete <task-id>
```

按状态删除或清空全部任务：

```sh
./handoff delete --state finished
./handoff delete --all
```

清理可安全丢弃的记录（只动终态任务，不碰未结束的）：

```sh
./handoff clean invalid      # 无结果结束：target_absent / source_absent / timeout / cancelled
./handoff clean old          # 终态且已静止超过 7 天的记录，含 finished / rejected
./handoff clean old --days 1
```

后台不靠终端文本来推进任务状态：B 必须显式执行 `take` 和 `done`；A 必须显式执行 `claim`。有 handoff task 时，B 通过 `handoff done` 回报，服务会通知 A。（配置 `TYPESAFE_API_KEY` 后，daemon 会用 Jev 判断提醒该不该发，看板按 `s` 可以用它给所有未结束任务打分并说明 Target 停着的原因，但这些只影响提醒和观测，不推进状态，见「Jev 语义判断」一节。）

## 看板操作

`handoff ui` 不只是展示，可以直接操作任务：

| 按键 | 作用 |
|---|---|
| `↓` / `↑` | 移动光标 |
| `空格` | 勾选/取消光标所在行 |
| `a` | 全选 / 全不选 |
| `r` | 向勾选任务的 Target 重发已存 prompt |
| `s` | 让 Jev 重新评审所有未结束任务并打分 |
| `d` | 删除勾选任务（先弹确认） |
| `y` / `Enter` | 确认删除；其他任意键取消 |
| `t` | 启动 / 停止 daemon（先二次确认） |
| `q` / `Ctrl-C` | 退出 |

每个动作一次按键，没有先导键，移动只用方向键。`d` 删的是**勾选的那几行**；按状态或时间批量回收是命令行的 `handoff clean`（见上），不看勾选、也不区分你是从哪一行按的。

`r` 只重发仍处于 `published` 或 `active` 的任务，且只投给 Herdr 状态为 `idle` 或 `done` 的 Target。其余一律跳过，原因列在提示条**上方单独一行**——提示不会盖住快捷键条。Herdr 本身不会拒绝向忙碌的 agent 投递 prompt，所以这道判断由看板负责，没有它就会打断正在进行的任务。重发的文本头部是 `[HANDOFF TASK — RE-SENT]` 并写明任务当前状态，让接收端能区分「催办」和「新任务」，不必自己去查记录、也不会把已交的活重做一遍。

SRC/DST 两列是**向 Herdr 实时查询的事实**（一次 `herdr agent list` 覆盖全部 agent），不依赖 daemon 是否在运行。值就是 Herdr 自己的词：`working` / `idle` / `blocked` / `done` / `unknown`，或者找不到 pane 时的 `absent` / `?:pane`（红字）。

**名字的颜色按 agent 种类走**：`codex` 是给定的 `rgb(130,139,251)`，`claude` 橙、`gemini` 蓝、`cursor` 近白、`kimi` 紫……常见的那十几种在 `AGENT_RGB` 里逐个指定，其余按名字稳定地落到同一组备用色——同一个工具永远同一个颜色，不用认名字就知道哪端是谁。**状态词仍用原来的颜色**（`idle`/`done` 绿、`working` 黄、`blocked` 红）。判据来自标题和屏幕内容，`herdr agent explain <pane>` 会把具体是哪条规则、什么证据摊开给你看。

欠着下一步的那一头**名字上有一道走过的波**——所以格子里不写「等谁」。「欠谁」这件事只在有人执行显式命令时才前进。**该动的那一头的名字有一道高光**：每个字符比前一个慢十分之一个周期，所以高光是沿着名字走过去的，而不是整个名字一起明灭——2.4 秒一轮，颜色从该 agent 的本色逐级提亮到掺入 55% 白，三角波取平方所以**亮带窄、名字大部分时间保持本色**。另一头就是那个本色，不动——靠「在动」区分谁在等。

**已终结的行整行一个灰**（240）：名字、状态、STATE 词全部同色，比活跃行的任何一档都暗，整行退到后面。唯一的例外是红色告警——`rejected`、`*_absent` 这类词保持红色，压暗不能把问题藏起来。

波只在终端支持颜色时出现（`NO_COLOR` 或非 TTY 时不渲染）。

看板还显示 `START` 和 `PREV` 两列，分别表示任务登记时间和当前状态前一个节点的开始时间；首个节点没有 Prev 时回退显示 `START`。时间使用 `MM-DD HH:MM` 本地格式，任务按活跃态优先、再按最近节点时间倒序排列。任务行区域最右侧显示低对比度滚动条，终端过窄时各列会按阶梯收缩，始终保证表头、任务行和滚动条不超出终端宽度。

`PROCESS` 列是 **Jev 对「欠着下一步那个 agent」的判断**，也是看板上唯一会过期的信息——它只在你按 `s` 时刷新。三种形态：

- **还没评审过**：显示待办命令本身（`take` / `done` / `claim` / `accept`），谁的靶子由 SRC/DST 的明暗指出；`—` 表示任务已终结。
- **评审过之后**：状态词 + 完成比例，如 `verifying 82.22%`。两个答案互为独立的问题，允许打架——词说 agent 此刻在干什么，百分比说这件事占整件事的多少。同一个 `working` 可以是 20% 也可以是 70%。
- 只有其中一个答案回来时，就只显示那一个。

## 状态词是怎么来的

Herdr 只给三个词（`working` / `idle` / `blocked`），而 `idle` 盖住了几种需要完全相反处理的情况。所以剩下那部分交给 Jev：评审时拿待办 agent 的终端快照 + 任务时间线，从二十一个词里挑一个。

**十二个「路上」的词**（按先后排）：

```
unstarted · reading · exploring · planning · groundwork · output
working · first-pass · refining · verifying · concluding · done
```

底色按段位走：蓝 → 青 → 黄 → 绿。

**九个「不在路上」的词**——麻烦可以在任何位置出现，把它们排进刻度就等于宣称「第三次改崩的任务比正在写主体更靠后」：

| 词 | 处境 | 底色 |
|---|---|---|
| `waiting` | 在等自己发起的东西跑完：构建、作业、长命令、跑几小时的作业——长程后台任务就是这个词 | 洋红 |
| `restarting` | 放弃原来那条路，换一条重来；已做出的部分丢掉 | 洋红 |
| `workaround` | 撞上任务没预料到的坎，正在绕过去 | 洋红 |
| `diagnosing` | 出了问题但还不知道是什么，正在缩小范围 | 洋红 |
| `fixing` | 已经做出来的东西错了——失败了、检查没过、方案被否——**正在修** | 洋红 |
| `error` | 停在失败上，**没人管**：屏幕上摆着报错、崩溃、跑挂，回合就在那儿结束 | **红** |
| `unreported` | **活看着做完了，但没执行 `handoff done`**——去催它交结果 | 洋红 |
| `stuck` | 手里能试的路都试过，走不通，也看不到下一步——**需要别人拿主意** | **红** |
| `elsewhere` | 回合结束了，pane 转去做别的了 | 洋红 |

红色是「要人」：`stuck`（试过几条路都不通）和 `error`（停在失败上没人管）用红，其余七个是自己能走的，用洋红。两者的区别在证据上——`error` 是屏幕上明摆着一个失败，`stuck` 是没有报错但也没有下一步。`blocked` 不在这个表里——**Herdr 自己就报这个**（它认得出 codex 的 `Action Required`、claude 的 `do you want to proceed?` / `esc to cancel`、cursor 的 `waiting for approval`），所以不必问 Jev，也不会看到两个来源的同一个词。

和分数一样，这二十一个词**只在按 `s` 时更新**，daemon 不碰这一列。

## 完成态的任务不可再推进

任何会把**已终结**任务（`finished`、`rejected`、`cancelled`、`timeout`、`*_absent`）拉回活动状态的命令都会被拒绝，返回非零退出码并说明当前状态。覆盖 `take`、`done`、`done-implicit`、`reject`。

没有这道守卫时，一个老实照提醒词敲命令的 agent 会把已验收的任务复活成 `active`，重做一遍，`done` 覆盖掉已存结果，并再通知 Source 一次。

`claim` / `accept` 刻意不在守卫范围内——它们的落点就是 `finished`，重复执行是幂等重试，不是复活。

`handoff cancel` **只改本地记录，不通知 Target、也不打断它**。范围调整时的做法是「取消旧任务 + 发一条收敛后的新任务」，不引入额外的取消协议。需要让 Target 立刻停下，由操作者手动执行 `herdr agent send-keys <target> esc`。

## daemon 的单实例保证

靠一把文件锁（`daemon.lock`），不是靠 pid 文件。**pid 文件在持有者被 `kill -9` 后会留下一个指向已消失进程的记录，而用户态无法分辨它和活着的 daemon 有什么区别**——运行中的 daemon 就是这样被显示成 stopped 的。锁由内核在持有者死亡时释放，所以不存在这个问题；pid 文件保留，但只作给人看的便利信息。

- `daemon start` 在已有 daemon 持锁时**拒绝启动**并报错，不会再出现两个 daemon 同时跑、互相重复投递
- `daemon status` 与看板用**同一套判定**，不会再一个说 running 一个说 off
- `daemon stop` 会等锁真正释放再回报（最多 6 秒），不是「信号已发出」就算完；没有 daemon 时明说 `no daemon is running`
- 看板上按 `t` 会**先问一次**再执行
- **目标 pane 正处于用户焦点或刚被打断时不发提醒**：提醒会唤醒 agent 起一个回合，而这正是按 Esc 要撤销的东西；用户就在那个窗口里，待办状态他自己看得见。跳过时**不消耗重试次数**，所以任务不会在你坐着或刚打断时老化成 `timeout`；焦点移开且中断迹象消失后下一轮扫描即恢复。是否「刚被打断」在配置了 `TYPESAFE_API_KEY` 时由 Jev 判断（见下节），否则用内置的中断标记子串匹配

## 配置

当前默认值写在 Spec 中：协议提醒 30 秒一次；执行和验收退避从 2 分钟开始，最大 8 小时。第一版运行参数仍使用环境变量 `HANDOFF_STATE_DIR` 选择本地状态目录。

## Jev 语义判断（可选）

配置 `TYPESAFE_API_KEY` 后，Jev 参与两件事（`HANDOFF_JEV=0` 可整体关闭）：

- **状态词 + 完成比例 · 按需触发**：在看板按 `s` 触发一次全量评审。Jev 一次读完**所有未结束任务**，每条问两道：`choice` 从二十一个状态词里挑一个（见上节，含十二个「在路上」和九个「不在路上」），`score` 给它在 0–9 上的位置。两个答案各走各的、互不推导——格子并排显示 `verifying 82.22%`：词是模型挑的，百分比是它自己那个数占整条刻度的比例（保留两位）。选择题不像打分题那样受 API 的十级上限约束，所以词表可以按需要加；打分的十级是**另一套描述**，只说「离完成还有多远」（0 什么都没做 → 9 已完成），不重复任何状态词。
  送进去的 state 不只有当前终端快照，还有每个任务的来龙去脉：登记时间、当前节点和前一节点的开始时间、上次动作、已消耗的重试次数、两侧 lifecycle/presence、上一轮评审留下的分数。只看快照分不清「刚开始」和「重试四次了」。全部任务在一次请求里并发提问（`partial` 模式：个别任务没答上不影响其余落分）；请求整体失败则一分不写，旧分数原样保留。**这两列不做后台刷新**：屏幕上的字来自一次显式按键，直到下一次评审或任务迁移状态为止。
- **提醒决策 · daemon 每轮判断**：内置中断标记命中即跳过；未命中时由 Jev 判断本轮是否该压住提醒（`hold_off`）——用户主动结束了回合，或快照显示 agent 正在推进这个任务（含它发起、仍在运行的后台仿真/构建），都压住不发、不耗重试；否则发出那条固定提醒。

两类判断互相独立：daemon 每轮只问 `hold_off`，不写状态词也不写分数。Jev 不可用或调用失败时退回标准提醒（内置中断标记始终先生效），任务状态推进永远只由显式 handoff 命令驱动。所有 Jev 请求共用一个下限：**同一进程里 5 秒最多一次**。撞上这个下限时，看板按 `s` 会提示「问得太频繁，还要等 N 秒」——不然你看到的就是一个空结果，跟调用失败长得一样；daemon 那一轮则退回标准提醒。调用超时或失败同样计入这 5 秒，避免服务不可用时被反复重试。

可调项：`TYPESAFE_API_URL`、`TYPESAFE_MODEL`（默认 `jev-latest`）、`HANDOFF_JEV_TIMEOUT`、`HANDOFF_JEV_MIN_INTERVAL`（默认 5 秒）、`HANDOFF_JEV_SUPPRESS_THRESHOLD`（默认 0.5）。

## daemon 遇到每种 agent 状态怎么办

| Herdr 报的 | 含义 | daemon |
|---|---|---|
| `working` | 回合进行中 | 有界等 3 秒，不提醒、不耗重试 |
| `blocked` | **屏幕上有个等着批准的框** | 同 `working`：它在等人回话，不是在忽略我们 |
| `idle` / `done` / `unknown` | 回合结束 | 提醒流程：焦点 → 中断标记 → Jev `hold_off` → 发 |

每一轮扫描会把任务的**两端**都查一遍（不只是等着被催的那一端）并写进 `*_lifecycle` / `*_presence`。只查一端时，另一半会停在某一轮扫描的旧值上，而那份旧值正是评审送给 Jev 当上下文的东西——Source 早就回去干自己的活了，却还挂着发任务那会儿的状态。
| `absent` | pane 不在了 | 判 `target_absent` / `source_absent` |

`blocked` 那行是有来历的：`herdr agent prompt` 对已经 blocked 的 pane **会在发出任何输入之前拒绝提交**，而拒绝的返回值原来被丢掉了——于是每条提醒都被记成「已投递」，三次协议重试 30 秒一轮，任务就这样被判成 `timeout`，而实际上一条都没送到。现在两条都改了：blocked 与 working 同样对待，且投递失败不计次数。

## Herdr 插件

```sh
herdr plugin link "$PWD"
```

插件提供 Handoff Board pane，但不会自动启动 daemon——可以手动执行 `./handoff daemon start`，也可以在看板里按 `t` 开关。

## 测试

```sh
python3 -m unittest discover -s tests -v
```

## Agent Skill

通用 Agent 指令位于 `herdr-task-handoff/SKILL.md`。将该目录复制到你的 Agent harness 的 skills 目录，并确保该 Agent 能访问 `handoff` 全局命令。Skill 只使用 Herdr 的通用概念和 CLI，不包含特定模型、供应商或 Agent harness 参数。
