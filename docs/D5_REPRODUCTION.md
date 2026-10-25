# D5 对照实验复现与边界

D5 是开发/验证筛查；本说明不代表实验已经完成。验收总表见 D5_ACCEPTANCE_PLAN.md，当前进度见 D5_PROGRESS.md。所有命令在既有 WSL `/home/lee/sre-project` 环境运行，沿用锁定依赖、固定上游 HolmesGPT 版本与已验收 D4 基线。

1. `PYTHONPATH=backend .venv/bin/python scripts/deploy_d5_runtime.py` 构建隔离镜像与预算代理。它保留原预算数据库，绝不通过重建容器清零。真实 Key 只在代理。
2. 依次运行 `PYTHONPATH=backend .venv/bin/python -m benchmarks.d5.capture --split dev` 与 `--split validation`。每次取得独占实验锁、确认业务健康、注入真实故障、确认实际症状和告警、采证并精确清理。失败保留；新时序协议会重采旧的无关发布顺序样本。
3. `PYTHONPATH=backend .venv/bin/python -m benchmarks.d5.freeze` 校验20个样本、完整记录哈希、incident和严格更晚的无关发布。正式留出配置不执行。已有冻结清单不能覆盖。
4. `PYTHONPATH=backend .venv/bin/python scripts/verify_d5_isolation.py` 审核实际镜像及快照中已知凭据。Agent只挂自身结果目录、随机ID公共快照和固定Holmes依赖，不能访问注入器、评分器、标准答案或管理员凭据。
5. `PYTHONPATH=backend .venv/bin/python -m benchmarks.d5.matrix prepare` 冻结源码/基线/数据、顺序种子及76任务；随后 `... matrix run --reserve-attempts 4`。20规则任务无网络，56模型任务有各次上限；调度种子不冒充模型seed。不足预算保留待执行项，失败不自动重跑。
6. `PYTHONPATH=backend .venv/bin/python -m benchmarks.d5.e2e --case dev_01 --variant B0` 示范新现场评测。它重新采证并调用共同的生产授权/有界执行核心，保留根因与完整120/120/12外部评分；不声称覆盖控制台/分布式worker审批全流程。模型variant同样消耗当前批额度。
7. `PYTHONPATH=backend .venv/bin/python -m benchmarks.d5.analyze` 汇总诊断、H1增量、2×2及现场结果。各组分母、缺失和失败均保留。root-action为动作/目标/值代理指标，不是自由文本根因质量的人工判分；升级人工不记恢复。
8. `PYTHONPATH=backend .venv/bin/python scripts/verify_d5_regression.py` 在独立测试数据库运行完整回归。远端CI另含镜像隔离、12项浏览器契约与告警规则检查。
9. 停止实验写入后，`PYTHONPATH=backend .venv/bin/python scripts/archive_d5_evidence.py` 生成D盘全新目录、脱敏副本、原始/归档哈希、预算公开摘要和回读验证。预算数据库、原始凭据和kubeconfig不复制。部分结果明确标记未完成。

前8次模型预跑用于开发校准，发生在逐HTTP完整轨迹持久化之前：保留现有SDK/Holmes结果与调用账本，但不伪称它们拥有后续正式矩阵同等的逐HTTP请求/响应覆盖。一次缺少core依赖的0调用启动失败、旧时序快照和发布格式校验失败均单列，不替换或隐去。

目前不将候选默认投入D4。H2a支持假成功检测，H2b反馈再规划尚未实现；H6–H9是否继续取决于实际瓶颈和证据。没有可重复收益可保留D4。D6每例3次、每端到端方案至少60次及新环境复现仍需独立完成。
