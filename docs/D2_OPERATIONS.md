# D2 首个自动修复闭环

## 实际能力

告警进入 API 后，worker 从 PostgreSQL 选择仍处于 firing 的新订单事件；通过 HolmesGPT 查询配置/发布历史、指标、依赖错误日志和库存服务端点，得到严格结构化方案。代码策略核对证据 ID、当前 Deployment UID/代次、预期旧值、可信基线和库存端点，再用带 resourceVersion 前置条件的 JSON Patch 恢复配置。执行后读回并等待发布，进行独立业务验证。

状态：NEW → INVESTIGATING → PLAN_READY → EXECUTING → VERIFYING → RESOLVED；无法诊断、证据不足、配置变化或验证失败则 ESCALATED。告警 resolved 通知不会独立触发 RESOLVED。

当前自动动作仅限 kind-sre-dev 集群、sre-demo 命名空间、order Deployment 的 INVENTORY_URL；RELEASE_ID 由执行器生成以关联修复发布。没有任意 shell、任意资源编辑或生产平台自动操作能力。可信目标来自 D1 已验证部署清单，不来自故障注入脚本。诊断引擎不具备执行工具。

## 运行

在 /home/lee/sre-project 执行：

```bash
PYTHONPATH=backend .venv/bin/python -m cloudsre.worker
```

默认持续验证窗口 120 秒，允许 30–300 秒；本次验收设置 CLOUDSRE_VERIFY_SECONDS=30，在持续业务负载下四次验证真实下单、读回、库存、幂等与冲突处理。该采样验收不能证明所有业务请求在窗口内都成功。

worker 使用 PostgreSQL advisory lock 限制并发执行。每次修复前再次验证锁；执行意图先记录。变更结果不确定时读回确认，不自动重发；任务异常转人工并保留现场。worker 运行在 WSL，事件 API 在 Docker，业务在 kind；后续再容器化 worker 和细化 RBAC。

验收入口 scripts/verify_d2.py。它会启动临时 worker、产生负载、注入随机错误地址；只在 Agent 未恢复时做清理，且清理不会算作 Agent 成功。验收结束停止临时 worker 和端口转发，不继续产生模型调用。

## 持久化和轨迹

- API GET /incidents/{id}/runs 提供阶段、方案、动作、验证和模型用量。
- evaluation/runs/d2-<run-id>/ 保存 incident、脱敏 HolmesGPT 结果、证据文件及 workflow。
- evaluation/runs/d2-acceptance-<id>/ 保存评测报告和 worker 日志。
- 诊断进程超时 180 秒，最大 8 步；工具仅四个固定只读命令。

## 真实验收

首次：库存端点查询退出，模型转人工，评测清理恢复。完整失败记录保留；未将该次算成功。原始 kubectl stderr 在首版未保留，因此不能断言首错具体原因。

修正：保留只读 kubectl 错误信息、限制 Go 工具并行度和堆预算；在上游相同子进程限制下离线查询通过；允许模型对瞬时只读失败重试一次。

第二次：实际告警自动触发；模型提出正确方案；Agent 完成配置恢复；30 秒四次业务验证通过；无评测清理；重复告警不重复处理；第二个 worker 被锁拒绝。成功调查两次模型调用、7,383 tokens，失败调查 8,474 tokens，本轮两次调查合计 15,857 tokens。未将上游未知模型价格估计为真实费用。

这是一个故障类型的受控集成验收，不能换算为通用成功率，也尚未证明创新增益。

## 尚待 D3/D5 完成

当前保存了阶段检查点，但尚不自动接管崩溃后的中间任务；状态与审计更新还需统一事务及恢复协议。API/数据库中断、执行器故障、多事件、复合故障的完整覆盖待 D3。额外回滚/补偿策略不得简单重设已知坏配置，需结合业务效果设计。通用根因分解、证据预算优化及公平消融待 D5。

当前可信基线是本地已验证清单，后续应版本化签名/审批及健康版本登记；不能把现有文件当作任意应用的生产发布治理。
