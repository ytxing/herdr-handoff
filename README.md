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

后台不会解析终端自然语言。B 必须显式执行 `take` 和 `done`；A 必须显式执行 `claim`。有 handoff task 时，B 通过 `handoff done` 回报，服务会通知 A。

## 看板操作

`handoff ui` 不只是展示，可以直接操作任务：

| 按键 | 作用 |
|---|---|
| `↓` / `↑`、`j` / `k` | 移动光标 |
| `空格` | 勾选/取消光标所在行 |
| `a` | 全选 / 全不选 |
| `r` | 向勾选任务的 Target 重发已存 prompt |
| `d` | 删除勾选任务（先弹确认） |
| `y` / `Enter` | 确认删除；其他任意键取消 |
| `t` | 启动 / 停止 daemon（先二次确认） |
| `q` / `Ctrl-C` | 退出 |

`r` 只重发仍处于 `published` 或 `active` 的任务，且只投给 Herdr 状态为 `idle` 或 `done` 的 Target。其余一律跳过，原因列在提示条**上方单独一行**——提示不会盖住快捷键条。Herdr 本身不会拒绝向忙碌的 agent 投递 prompt，所以这道判断由看板负责，没有它就会打断正在进行的任务。重发的文本头部是 `[HANDOFF TASK — RE-SENT]` 并写明任务当前状态，让接收端能区分「催办」和「新任务」，不必自己去查记录、也不会把已交的活重做一遍。

SRC/DST 两列的状态是向 Herdr 实时查询的（一次 `herdr agent list` 覆盖全部 agent），不依赖 daemon 是否在运行。看板还显示 `START` 和 `PREV` 两列，分别表示任务登记时间和当前状态前一个节点的开始时间。时间使用 `MM-DD HH:MM` 本地格式。终端过窄时各列会按阶梯收缩，始终保证表头和任务行不超出终端宽度。

`ACTION` 列是「下一步该谁敲哪条命令」：`take · h2` 表示等 h2 执行 `take`，`▶ accept` 表示轮到你执行 `accept`（`▶` 是拿你所在 pane 与任务的 source/target pane 比对得出的），`—` 表示任务已终结。这是读看板时最该先看的一列——handoff 的状态机只在有人执行显式命令时才前进。

## 完成态的任务不可再推进

任何会把**已终结**任务（`finished`、`rejected`、`cancelled`、`timeout`、`*_absent`）拉回活动状态的命令都会被拒绝，返回非零退出码并说明当前状态。覆盖 `take`、`progress`、`done`、`done-implicit`、`reject`。

没有这道守卫时，一个老实照提醒词敲命令的 agent 会把已验收的任务复活成 `active`，重做一遍，`done` 覆盖掉已存结果，并再通知 Source 一次。

`claim` / `accept` 刻意不在守卫范围内——它们的落点就是 `finished`，重复执行是幂等重试，不是复活。

`handoff cancel` **只改本地记录，不通知 Target、也不打断它**。范围调整时的做法是「取消旧任务 + 发一条收敛后的新任务」，不引入额外的取消协议。需要让 Target 立刻停下，由操作者手动执行 `herdr agent send-keys <target> esc`。

## daemon 的单实例保证

靠一把文件锁（`daemon.lock`），不是靠 pid 文件。**pid 文件在持有者被 `kill -9` 后会留下一个指向已消失进程的记录，而用户态无法分辨它和活着的 daemon 有什么区别**——运行中的 daemon 就是这样被显示成 stopped 的。锁由内核在持有者死亡时释放，所以不存在这个问题；pid 文件保留，但只作给人看的便利信息。

- `daemon start` 在已有 daemon 持锁时**拒绝启动**并报错，不会再出现两个 daemon 同时跑、互相重复投递
- `daemon status` 与看板用**同一套判定**，不会再一个说 running 一个说 off
- `daemon stop` 会等锁真正释放再回报（最多 6 秒），不是「信号已发出」就算完；没有 daemon 时明说 `no daemon is running`
- 看板上按 `t` 会**先问一次**再执行
- **目标 pane 正处于用户焦点时不发提醒**：提醒会唤醒 agent 起一个回合，而这正是按 Esc 要撤销的东西；用户就在那个窗口里，待办状态他自己看得见。跳过时**不消耗重试次数**，所以任务不会在你坐着的时候就老化成 `timeout`；焦点移开后下一轮扫描即恢复

## 配置

当前默认值写在 Spec 中：协议提醒 30 秒一次；执行和验收退避从 2 分钟开始，最大 8 小时。第一版运行参数仍使用环境变量 `HANDOFF_STATE_DIR` 选择本地状态目录。

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
