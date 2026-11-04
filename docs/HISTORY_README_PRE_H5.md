# 云巡 SRE Agent

面向线上故障处理的云原生 Agent：从真实告警出发，收集日志、指标、运行状态和发布证据，生成受限修复动作，通过独立业务验证判断是否恢复。

D1、D2、D3、D4 已完成对应阶段验收。阶段状态与证据见 [D1 报告](docs/D1_COMPLETION_REPORT.md)、[D2 报告](docs/D2_COMPLETION_REPORT.md)、[D3 报告](docs/D3_COMPLETION_REPORT.md)。未通过的案例不得计入自主恢复。

D3 使用持久化工作流、PostgreSQL 租约、不可变动作意图及写后读回；诊断与执行使用独立身份。仅允许经过健康基线约束的四种动作，不执行模型生成的任意 Shell 命令。业务恢复必须通过完整 120 秒窗口、120 次请求、12 次业务不变量校验及延迟门槛。

运行与验收命令见 [D3 操作说明](docs/D3_OPERATIONS.md)，冻结判据见 [D3 验收计划](docs/D3_ACCEPTANCE_PLAN.md)。完整本地检查：

```bash
PYTHONPATH=backend .venv/bin/python scripts/quality_d3.py
```

本地与远端 CI 要求至少 92 项测试、零跳过，包含真实 PostgreSQL 集成测试、镜像构建和告警规则验证。实机故障、并发与进程接管另行验收，不能用单元测试代替。远端状态以 [GitHub Actions](https://github.com/leekj666666/ucloud/actions) 的对应提交记录为准。

原计划保存在 `docs/plans`，依赖由 `uv.lock` 固定；HolmesGPT 版本及许可证见 `docs/HOLMESGPT-COMMIT.txt` 和 `docs/HOLMESGPT-LICENSE`。凭据、模型调用原始记录和故障评测数据不上传源码仓库。D5 原思考模式对照与 H2b/H5 补强已完成相应范围验证，结果及限制见 [D5 结果](docs/D5_RESULTS.md) 和 [H2b 最终结论](docs/H2B_FINAL_DECISION.md)。H2b、H5 保留可选、默认关闭。D6 既定范围验收已完成，包括三轮真实用户操作，结果与限制见[D6结项报告](docs/D6_COMPLETION_REPORT.md)。

D4 故障指挥台与阶段验收已完成：[D4 完成报告](docs/D4_COMPLETION_REPORT.md)、[进度与失败披露](docs/D4_PROGRESS.md)、[冻结判据](docs/D4_ACCEPTANCE_PLAN.md)、[启动和演示说明](docs/D4_OPERATIONS.md)。默认逐次人工审批；停止请求与实际确认分开记录。页面使用实际 API/数据库/证据，不提前宣称业务恢复。新 CI 要求至少 113 项后端测试零跳过、5 项真实 PG 控制竞争、3 项真实进程取消、桌面/手机共 12 项浏览器契约与既有构建/告警检查。完整本地检查运行 `PYTHONPATH=backend .venv/bin/python scripts/quality_d4.py`。

## D6 正式验证与交付

冻结产品/评测提交 `c39e242` 的本地与远端后端测试均为 256 项通过、零跳过；远端另有 12 项浏览器契约通过。对应 [CI 证据](https://github.com/leekj666666/ucloud/actions/runs/41144665880) 不代替真实业务恢复与用户试用。

正式对照为 B0 规则、B2 基础 HolmesGPT、B3 当前默认系统，各 20 个留出配置 × 3 次，共 180 项现场试验；B1 另做 60 项快照诊断。已知故障家族内的新干扰上下文不等于全新故障泛化。正式调度已结束，失败、环境中断和未知用量均保留。B0规则恢复54/60，B3当前默认45/60，B2基础包装对照0/60；不能声称全面超过规则或上游产品，详见[结果与限制](docs/D6_RESULTS_REVIEW.md)。

- [D6 进度与失败披露](docs/D6_PROGRESS.md)、[冻结执行计划](docs/D6_EXECUTION_PLAN.md)
- [正式运行与中断恢复](docs/D6_OPERATIONS.md)、[架构、权限与价值边界](docs/D6_ARCHITECTURE.md)
- [同主机清洁部署说明](docs/D6_CLEANROOM.md)
- [真实用户试用清单](docs/D6_USER_ACCEPTANCE.md)、[三分钟真实演示脚本](docs/D6_DEMO_SCRIPT.md)

同主机清洁部署、七类真实浏览器自动化场景、九项权限检查和八项控制竞争/进程测试已通过，真实三分钟字幕演示已生成。见[部署与验收报告](docs/D6_CLEAN_DEPLOYMENT_REPORT.md)。真实用户试用和最终归档仍有独立完成判据，自动化不能替代用户操作。
