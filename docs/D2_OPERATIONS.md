# D2 自主修复与严格恢复验收

## 能力和范围
真实 Alertmanager 告警进入事件 API，由 PostgreSQL 持久化。worker 调用 HolmesGPT，通过四个固定只读工具调查配置、发布历史、指标、日志和依赖端点。模型提出结构化方案，策略核验证据、可信目标及实际资源，再由独立执行器修改配置。

当前自动动作仅限 kind-sre-dev / sre-demo / order 的 INVENTORY_URL，RELEASE_ID 由执行器生成中性关联标识。范围与原 D2 单故障闭环一致。诊断工具无修改能力。多事件并行、进程崩溃接管、细粒度 RBAC、更多故障及物理隔离保留 D3。

## 执行保护
策略批准快照绑定 UID、generation、resourceVersion、镜像内容标识、完整 spec 摘要及旧配置值。执行前重新读取；任何变化均拒绝。实际 JSON Patch 同时包含原子前置测试。写入超时/未知结果只读回核对，不盲重发；无法确认则转人工。发布后和质量窗口结束再次核对版本。

NEW → INVESTIGATING → PLAN_READY → EXECUTING → VERIFYING → RESOLVED。诊断、策略、执行或验证失败进入 ESCALATED。告警恢复通知和 /health 正常均不等于业务修复。

## 正式运行与验收
在 /home/lee/sre-project：
```bash
PYTHONPATH=backend .venv/bin/python scripts/verify_d2_complete.py
```
旧入口 scripts/verify_d2.py 转到同一严格验收，不再允许 30 秒替代。

验收先创建中性健康发布，采集 120 秒健康基线，再启动临时 worker，注入中性错误依赖地址。真实告警触发动态调查，Agent 自行恢复。评测器只在失败后清理，清理永不算 Agent 成功。临时 worker、故障负载在 finally 停止。

健康基线与恢复窗口使用相同负载：每秒一次下单，120 次全部保存；每 10 秒一次完整业务不变量检查，至少 12 次；监控每 5 秒查询；容器资源每 10 秒采样。资源结果是采样峰值，非连续监测的绝对峰值。独立 Docker 验证器经 NodePort → Service → gateway 访问业务，不用端口转发判定恢复。

恢复窗口至少 120 秒；下单 5xx、超时、不变量错误均为零；监控可用、无新增相关严重告警；P95 ≤ max(健康基线 P95×2, 1秒)。该阈值是冻结的本地开发标准，不是官方比赛门槛或生产 SLO。故障期间的失败单独保留，不从总时间线消失。

手动启动 worker 需要先提供本环境的合格基线：
```bash
CLOUDSRE_HEALTHY_BASELINE=/absolute/path/to/healthy-baseline/result.json PYTHONPATH=backend .venv/bin/python -m cloudsre.worker
```
环境或负载变化应重新采集基线。正式脚本自动设置基线与告警时间边界。诊断最多 8 步、180 秒；验收的 worker 处理上限为两次，以便仍运行时检验重复告警。第二 worker 必须被数据库执行锁拒绝。

## 证据与费用
evaluation/runs/d2-complete-*/ 保存冻结配置、健康基线原始样本、故障流量、worker 日志和完整验收结果；d2-*/ 保存诊断、证据、动作、恢复样本、workflow 和 incident-report.json。失败尝试保留，不覆盖为成功。

calls.jsonl 在每次实际 HTTP 请求发起前 fsync 落盘，记录尝试 ID、响应请求 ID、模型、耗时、usage 和缓存字段；网络重试分别计数。中断/超时缺失 usage 标为未知，不能按零计算；已知 tokens 是已知调用的小计。未核对官方价目时金额、币种及价目版本保留 null，不沿用上游 0.0 估值。

结构化报告包含影响、时间线、假设与证据引用、动作、验证、未解决问题、长期建议、软件及配置版本、阶段耗时和调用账本。无法确定的受影响请求总数如实标未知。

## 本地环境维护
Docker 重启可能改变基础服务 IP。业务因旧 Secret 地址无法连接数据库或 Alloy 无法连接 Loki 时：
```bash
PYTHONPATH=backend .venv/bin/python scripts/refresh_local_network.py
```
此脚本更新本地地址并等待滚动发布，属于环境维护，不计作 Agent 修复能力。不要在正在运行的正式窗口执行。

## 结果解释
一次单故障闭环通过仅支持该案例与环境的能力判断，不代表通用成功率或创新增益。D3 的崩溃恢复/多事件与 D5 的公平基线、消融、留出故障测试仍须单独完成；D6 负责新环境从零复现。


诊断侧 Kubernetes 读取改为锁定依赖中的 Python 客户端，固定上下文、命名空间和资源白名单；原 800 MB 工具虚拟地址空间限制保留。修复前 Go kubectl 在该限制下存在随机分配失败；修复后部署及端点十次真实读取通过。
