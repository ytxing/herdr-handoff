# 009 Herdr 插件封装

添加 `herdr-plugin.toml`，注册看板 pane 和手动 daemon action；核心 CLI 可脱离插件运行。

依赖：004、008。

验收：插件可 link/install；不注册自动启动 daemon；pane 能运行 `handoff ui`。
