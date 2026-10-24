# D3 验收进度

更新时间：2026-10-25T01:30:30.292487+08:00。

D3 尚未宣告完成；下表按实际结果生成，保留全部已落盘失败。无 result.json 的中断不推定通过。

已通过五个进程中断点、四类故障历史案例、数据库连接中断、诊断超时、假恢复拒绝、受控补偿。并发案例 `d3-parallel-9acc0f72d511` 通过，验证窗口重叠 118.724957 秒，两组各 120 次请求、12 次业务校验、零错误，无评测器代修。

执行拒绝实机案例 d3-execution_denied-a38a359725ee 已通过；最新本地 CI d3-ci-f549c397d15f 通过 92 项测试、零跳过，9 项权限检查通过。剩余交付判据：最新四类故障回归、远端 CI、完整归档与版本核对。新增发布证据及启动就绪检查见 D3_OPERATIONS.md，不改变正式业务验收门槛。

| 运行 | 通过 | 案例/中断点 | 评测清理 | 错误类别 |
|---|---|---|---|---|
| d3-baseline-366bd63841c0 | True |   | None |  |
| d3-ci-961911e6a4eb | True |   | None |  |
| d3-ci-fbceba9e602d | True |   | None |  |
| d3-compensation-43972aea0265 | True |   | False |  |
| d3-config-017874a4635c | True | config after_write | False |  |
| d3-config-11c3abc43c68 | False | config  | True | AssertionError |
| d3-config-2c2b5900d34f | True | config before_diagnosis | False |  |
| d3-config-3f0a37f50564 | True | config  | False |  |
| d3-config-6258bd28ca68 | True | config before_write | False |  |
| d3-config-749b6bc25d61 | True | config verification | False |  |
| d3-config-9ee080a46e9f | False | config  | True | AssertionError |
| d3-config-e9c2b4531c50 | True | config after_diagnosis | False |  |
| d3-database_disconnect-7827e3bd4ec5 | True | database_disconnect  | False |  |
| d3-database_disconnect-873b602ffda1 | False | database_disconnect  | True | AssertionError |
| d3-database_disconnect-deaac1c695fb | False | database_disconnect  | True | AssertionError |
| d3-dependency-98c7fb4cc1b9 | True | dependency  | False |  |
| d3-dependency-fe193ab085e2 | False | dependency  | True | AssertionError |
| d3-diagnostic_timeout-3e1d51a08da5 | True | diagnostic_timeout  | True |  |
| d3-execution_denied-d7a6a6369504 | False | execution_denied  | True | AssertionError |
| d3-false-recovery-821285e5ead0 | True |   | True |  |
| d3-parallel-9acc0f72d511 | True |   | False |  |
| d3-parallel-e4704faac363 | False |   | False | CalledProcessError |
| d3-resources-251427451096 | False | resources  | True | AssertionError |
| d3-resources-8f02f8d51d4f | True | resources  | False |  |
| d3-resources-ad603b1a1d42 | False | resources  | True | AssertionError |
| d3-route-234886cfc312 | True | route  | False |  |

这些是开发迭代记录，配置与版本并不完全相同，不能作为泛化成功率。诊断断连/中断的未知用量不计为零；补偿案例属于执行器合同测试，不计作模型自主修复。

配置回归 `d3-config-6acdcd2355d1` 已通过。端口回归 `d3-route-542bf85a0703` 因 Docker/WSL 停止被中断，保留失败与清理记录；端口、依赖和资源案例正在新批次重跑。远端 CI 40116572960 在旧提交 5a591a4 成功；工具拆分修复后须检查新提交的 CI。
