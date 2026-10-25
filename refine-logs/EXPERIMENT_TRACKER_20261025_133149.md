# D5 实验跟踪

| Run ID | Milestone | Purpose | System / Variant | Split | Metrics | Priority | Status | Notes |
|--------|-----------|---------|------------------|-------|---------|----------|--------|-------|
| M0-budget | M0 | 实际上游调用硬上限及重启/并发/错误账本 | 公共预算代理 | 无模型控制 | 真实 HTTP 数/预算拒绝/未知 usage | MUST | TODO | 本批上限 100 |
| M0-controls | M0 | 无操作/标准修复/表面恢复控制 | 公共外部验收 | 独立控制 | 120秒/120请求/12检查及假阳性 | MUST | TODO | 不消耗模型 |
| M1-data | M1 | 12开发/8验证现场快照及留出隔离 | 同一观测协议 | dev/validation | 真实故障、去密与答案隔离 | MUST | TODO | 不运行 D6 留出 |
| M1-pilot | M1 | 固定四类基线可运行性 | B0/B1/B2/B3 | dev | 输出有效/调用/usage | MUST | TODO | 最多12次模型HTTP |
| M2-paired | M2 | H1/H4 配对诊断及 H2 安全对照 | 单因素 | validation | 配对胜负、真实usage、假成功 | MUST | TODO | 顺序随机化 |
| M3-selection | M3 | 有效组合/逐项移除与 H1-H9 采纳登记 | 候选最小组合 | validation | 效果、错误处置、成本 | MUST | TODO | 无证据不采纳 |
| M4-delivery | M4 | 回归、结果与证据封版 | 最终配置 | 已批准范围 | CI/账本/哈希 | MUST | TODO | 未通过不能标D5完成 |
