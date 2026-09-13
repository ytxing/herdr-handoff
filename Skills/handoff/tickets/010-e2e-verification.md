# 010 端到端验证

验证单个 A→B 正常闭环及所有已定义异常路径：忘记 take、working 后忘记 done、A 忘记 claim/accept、blocked、reject、Agent absent、先 wait until idle、超时、退避、stop/cancel。

依赖：002、003、005、006、007、008、009。

验收：每条路径有可复现命令和状态/看板证据。
