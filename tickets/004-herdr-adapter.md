# 004 Herdr 适配层

封装 `herdr agent get/list/prompt`，支持显式 Source/Target 身份校验。

依赖：001、002。

验收：能区分 present/absent 和 working/idle/blocked/unknown；Prompt 失败可报告；pane 移动不自动迁移任务。
