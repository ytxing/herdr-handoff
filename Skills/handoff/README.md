# Herdr Handoff

独立的单任务 A→B 交接服务。Herdr 负责 Agent 状态和 Prompt 投递；本项目保存任务状态、结果文件、超时和提醒。第一版支持 macOS 和 Linux，并可供不同 Agent harness 使用。

## 快速使用

```sh
cd Skills/handoff
./install-global.sh
handoff --help

./handoff daemon start
./handoff send --source-agent A --source-pane w1:p1 \
  --target-agent B --target-pane w1:p4 \
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
./handoff accept <task-id>
```

后台不会解析终端自然语言。B 必须显式执行 `take` 和 `done`；A 必须显式执行 `claim` 和 `accept`。

## 配置

当前默认值写在 Spec 中：协议提醒 30 秒一次；执行和验收退避从 2 分钟开始，最大 8 小时。第一版运行参数仍使用环境变量 `HANDOFF_STATE_DIR` 选择本地状态目录。

## Herdr 插件

```sh
herdr plugin link "$PWD"
```

插件提供 Handoff Board pane，但不会自动启动 daemon。需要手动执行 `./handoff daemon start`。

## 测试

```sh
python3 -m unittest discover -s tests -v
```

## Agent Skill

通用 Agent 指令位于 `skill/SKILL.md`。将该目录复制到你的 Agent harness 的 skills 目录，并确保该 Agent 能访问 `handoff` 全局命令。Skill 只使用 Herdr 的通用概念和 CLI，不包含特定模型、供应商或 Agent harness 参数。
