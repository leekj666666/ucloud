# D3 运行与验收说明

本地项目根目录为 `/home/lee/sre-project`，位于 D 盘的 WSL 虚拟磁盘中。以下命令均在该目录运行。验收状态以 `D3_PROGRESS.md` 和各次独立 `evaluation/runs/d3-*` 记录为准；本文不代表全部验收已经完成。

## 服务与身份

- `cloudsre-diagnosis-d3`：只读 Kubernetes 身份、模型 API、固定调查工具、独立诊断任务目录。没有执行身份、数据库管理员凭据、Docker socket 或评测脚本。
- 两个 `cloudsre-worker-d3-*`：受限数据库角色、限定 Kubernetes 执行身份、只读诊断结果和健康基线。没有模型 Key，也没有 Docker socket。
- 事件 API、业务验证器及资源观察器属于控制面或独立评测设施。资源观察器只把采样 JSON 目录以只读方式挂载给 worker。
- 同一 namespace 的业务依赖图使用保守互斥。跨 namespace 的独立业务图可以并发；不能把有依赖关系的两次修复当作独立任务。

数据库连接持有 PostgreSQL session advisory locks。连接失效后必须重新获取租约和更高代次；不得在自动重连的旧对象上继续提交。外部写入使用不可变动作 ID、UID、resourceVersion 和完整 spec 前置条件。未知结果先读回确认，不能凭超时判断未执行。

## 构建与正常启动

```bash
export PYTHONPATH=backend
.venv/bin/python scripts/build_d3_manifest.py
docker build -t cloudsre-agent:d3 -f deploy/Dockerfile.d3 .
docker compose --env-file infra/.env -f infra/compose.yml up -d --no-deps incident-api
.venv/bin/python scripts/provision_d3_rbac.py
```

身份令牌当前请求 24 小时有效期；重新签发后需要重新创建 D3 容器。凭据只保存在被 Git 忽略的 `infra/.generated/d3/` 私有文件中，不应打印或提交。

资源观察器需要独立运行；不把 Docker socket 交给 Agent：

```bash
mkdir -p evaluation/d3-runtime/observer
CLOUDSRE_OBSERVER_OUTPUT=evaluation/d3-runtime/observer/resources.json \
  .venv/bin/python scripts/observe_d3_resources.py
```

在另一终端完成健康基线和启动。正式基线固定为 120 秒；每次使用独立目录，保留以前的结果。`CLOUDSRE_INCIDENT_NOT_BEFORE` 用于隔离测试批次，不应无说明地跳过生产事件。

```bash
export PYTHONPATH=backend
export CLOUDSRE_INCIDENT_NOT_BEFORE="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
unset CLOUDSRE_TEST_PAUSE_AT CLOUDSRE_DIAG_TIMEOUT_SECONDS
.venv/bin/python scripts/prepare_d3_baseline.py
.venv/bin/python scripts/deploy_d3_runtime.py
```

D3 数据库迁移为 `0004`。升级后不能继续用只包含 `0003` 的旧事件 API 镜像启动。Docker Desktop 重启导致网络地址变化时，`scripts/refresh_local_network.py` 是环境维护工具；其重启业务 Pod 的操作不能记为 Agent 自主恢复。

## 实机验收入口

```bash
.venv/bin/python scripts/verify_d3_scenarios.py --case config
.venv/bin/python scripts/verify_d3_scenarios.py --case route
.venv/bin/python scripts/verify_d3_scenarios.py --case dependency --unrelated-release
.venv/bin/python scripts/verify_d3_scenarios.py --case resources
```

资源案例会导致新 Pod OOM、滚动发布受阻。旧 Pod 可能仍能正常提供服务，因此使用真实资源告警触发调查；不把它描述为业务全面中断。

五个接管测试分别设置 `CLOUDSRE_TEST_PAUSE_AT` 为 `before_diagnosis`、`after_diagnosis`、`before_write`、`after_write`、`verification`，重新部署 worker 后运行：

```bash
.venv/bin/python scripts/verify_d3_scenarios.py --case config --crash-at "$CLOUDSRE_TEST_PAUSE_AT"
```

测试标记仅用于在确定的进程边界执行真实 SIGKILL；正常部署不设置该变量。验证阶段重启必须重新采集完整窗口，不能拼接之前的局部采样。每轮重新设置事件截止时间，成功和失败均使用唯一记录目录。

其他验收入口：

- `verify_d3_false_recovery.py`：健康端点仍返回 200，但业务验证失败，必须转人工并保留工单。
- `verify_d3_runtime_guards.py --mode database_disconnect`：执行前切断持锁的数据库连接，验证接管和单次写入。
- `verify_d3_runtime_guards.py --mode execution_denied`：执行前撤销 patch 权限，验证拒绝和转人工。以上两项需部署 `before_write` 测试暂停点。
- `verify_d3_runtime_guards.py --mode diagnostic_timeout`：部署 `CLOUDSRE_DIAG_TIMEOUT_SECONDS=1` 后验证真实诊断进程超时、有界重试与零变更。该低预算仅用于测试；正常值为 180 秒。
- `verify_d3_compensation.py`：在 worker 已停止时验证受控扩容、验证设施失联及安全补偿。它属于执行器合同测试，不计入模型自主恢复率。
- `prepare_d3_parallel.py` 和 `verify_d3_parallel.py`：准备两个独立寻址的业务图，先采集同时负载的健康基线，再验证两个恢复窗口确实重叠。

不得同时运行独立故障脚本或重型 CI 与正式性能窗口。受控故障脚本的清理会明确记录 `evaluator_cleanup_used`；清理不能计为自主恢复。

## 完整检查与证据

```bash
PYTHONPATH=backend .venv/bin/python scripts/quality_d3.py
PYTHONPATH=backend .venv/bin/python scripts/verify_d3_permissions.py
```

完整检查使用隔离 PostgreSQL，要求零跳过，并构建应用和 D3 镜像、验证模块导入及告警规则。Promtool 的延迟用例是规则测试，不能冒充实机延迟故障实验。

数据库中的工作流和审计是状态依据；文件为派生证据。每轮保留基线、原始采样、模型请求账本、全部诊断尝试、动作请求记录、版本信息和工单。模型接口失联时用量可能未知，不得填写为零。事故报告在终态提交前生成，最终提交时间以数据库审计为准。

受控补偿只允许撤销自身动作，并绑定独立验证过的健康前态。四类故障恢复动作的前态已知不健康，不能为了“回滚”重新恢复故障。需要进一步处理时停止写入并转人工。

D4 的用户审批、取消、界面和导出，D5 的统计比较与消融，以及 D6 的新环境复现仍按原阶段执行。


## 模型预算调整记录

2026-10-24 的一次真实失败中，最后一次请求使用了 2048 个 completion tokens，其中 reasoning_tokens 也是 2048，最终结果为空。之后将单次输出上限从 2048 提高到 8192，保留每次调查 180 秒、最多 8 步、每事件最多 2 次调查和 10 秒持久化重试退避。新诊断任务状态记录有效模型与预算；旧记录及实际用量保留，不能据此把配置不同的开发轮次当作受控性能对照。

DeepSeek 官方亦提示 JSON 模式可能出现空内容，并要求给输出留足 token：[JSON Output](https://api-docs.deepseek.com/guides/json_mode/)。实际推理与可见输出的 token 仍按请求账本记录；不会把 SDK 未定价时给出的 0 当成真实费用。

## 2026-10-25 验收修正记录

- 并发测试在正式基线前分别验证业务与监控就绪，保存每次前置检查；正式窗口的长度、采样数量及零错误门槛不变。旧失败记录 `d3-parallel-e4704faac363` 保留。
- 只读证据加入 ReplicaSet 发布修订历史，记录创建时间、所属 Deployment UID、修订号、镜像和公开配置。创建时间表示 Kubernetes 滚动发布对象的创建，不能冒充 CI 发布时间。事件查询显式标记分页截断；摘要保留此标记，避免把局部事件当作完整历史。
- 发布证据脱敏与摘要保留新增两项测试，完整 CI 最少测试数提高至 91。

### 发布历史工具拆分

回归案例 `d3-config-8ced86126f7b` 暴露新发布历史与运行状态合并后超过 Holmes 单工具 4800 tokens 上限，策略因缺少运行反证正确拒绝执行。修复为第六个固定只读工具 `get_release_history`，运行状态工具仍返回 Pod、端点和事件。历史按所属 Deployment 分组，对重复配置建立索引；保留全部已取回修订、时间、UID 和配置值。三项发布证据测试覆盖脱敏、摘要标记与无损重建，完整 CI 最少 92 项。

使用原失败证据（33 个 ReplicaSet）离线复测：运行状态约 1892 tokens，发布历史约 3667 tokens，各低于原 4800 上限，未扩大上下文或减少证据。实际调用另按账本记录。测试中断及环境停止均保留原始记录；维护清理不能计为自动修复。
