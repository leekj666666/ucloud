# 云巡 SRE Agent

面向线上故障处理的云原生 Agent：从真实告警出发，收集日志、指标、运行状态和发布证据，生成受限修复动作，通过独立业务验证判断是否恢复。

D1、D2 已完成验收；D3 正在补齐最终实机验收与交付。阶段状态与证据见 [D1 报告](docs/D1_COMPLETION_REPORT.md)、[D2 报告](docs/D2_COMPLETION_REPORT.md)、[D3 进度](docs/D3_PROGRESS.md)。未通过的案例不得计入自主恢复。

D3 使用持久化工作流、PostgreSQL 租约、不可变动作意图及写后读回；诊断与执行使用独立身份。仅允许经过健康基线约束的四种动作，不执行模型生成的任意 Shell 命令。业务恢复必须通过完整 120 秒窗口、120 次请求、12 次业务不变量校验及延迟门槛。

运行与验收命令见 [D3 操作说明](docs/D3_OPERATIONS.md)，冻结判据见 [D3 验收计划](docs/D3_ACCEPTANCE_PLAN.md)。完整本地检查：

```bash
PYTHONPATH=backend .venv/bin/python scripts/quality_d3.py
```

本地与远端 CI 要求至少 92 项测试、零跳过，包含真实 PostgreSQL 集成测试、镜像构建和告警规则验证。实机故障、并发与进程接管另行验收，不能用单元测试代替。远端状态以 [GitHub Actions](https://github.com/leekj666666/ucloud/actions) 的对应提交记录为准。

原计划保存在 `docs/plans`，依赖由 `uv.lock` 固定；HolmesGPT 版本及许可证见 `docs/HOLMESGPT-COMMIT.txt` 和 `docs/HOLMESGPT-LICENSE`。凭据、模型调用原始记录和故障评测数据不上传源码仓库。D4 用户审批与界面、D5 统计对比与消融、D6 新环境复现按原阶段推进。
