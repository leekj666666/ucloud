# D1 完整业务与观测操作说明

主源码 WSL /home/lee/sre-project，物理文件在 D 盘。D:/uclaude/dev-stage 为同步快照。

## 服务与真实网络路径

- 事件 API：http://localhost:18080/docs
- 独立业务验证服务：http://localhost:18084/docs；POST /verify 无需指定目标，目标由部署配置固定。
- Grafana：http://localhost:13000/d/cloudsre-demo，包含6个面板；输入32位真实 Trace ID 查询 span。
- Prometheus：http://localhost:19090；Alertmanager：http://localhost:19093；Loki：http://localhost:13100。
- Kubernetes sre-demo：gateway、order、inventory、probe、alloy。
- 独立 verifier-api 在 Docker 网络经 kind NodePort 30082/30081 访问 Kubernetes Service；probe 在集群内经 Service DNS 访问真实业务。

Grafana admin 随机密码只在 Linux infra/.env，不写入文档。数据库密码、生成的 Secret 和备份只在私有配置目录。接口均为本机开发用途。

## 检查与部署

在主源码根目录执行：

```bash
export PATH="$HOME/sre-tools/bin:$PATH"
uv sync --frozen
PYTHONPATH=backend .venv/bin/python scripts/quality_d1.py
PYTHONPATH=backend .venv/bin/python scripts/deploy_d1.py
.venv/bin/python scripts/provision_dashboard.py
PYTHONPATH=backend .venv/bin/python scripts/verify_d1_complete.py
```

quality_d1.py 创建独立临时 PostgreSQL，运行含真实TCP服务的全部21项测试并构建镜像，最后清理本次容器和匿名数据卷。单独 pytest 而未配置 SRE_TEST_DATABASE_URL 时会跳过2项PG测试，所以不能据此宣布完整CI通过。

verify_d1_complete.py 会在固定 kind-sre-dev/sre-demo 中临时变更配置、缩容 inventory、修改 gateway Service targetPort，并在 finally 恢复；不针对生产环境。旧 scripts/verify_d1.py 已转调完整入口。验收暂停probe进行精确库存核对，之后恢复。保留所有尝试、负载请求、Trace和清理证据，不调用模型。

部署器先校验本地固定摘要的 Alloy 镜像，缺少时才下载；应用镜像内容ID变化会触发滚动更新，避免只复用tag造成旧Pod未更新。迁移保留原数据，物理PG未拆分。标准非敏感资源可用 kubectl kustomize deploy 渲染；应用清单引用的 Secret 由部署器创建。云端地址/凭据参数化和新环境复现按D6另验，不能把本机配置宣称成任意云一键安装。

手动访问业务可使用：

```bash
kubectl --context kind-sre-dev -n sre-demo port-forward service/gateway 18083:8000
```

port-forward 适合本机调试；Service 路由坏时它可能绕过部分错误，恢复验收须使用独立 verifier-api 的真实路径。

## Trace、发布记录与验证数据

OpenTelemetry SDK 为三服务请求生成 SERVER/CLIENT span，携带 W3C traceparent，结构化导出给 Alloy/Loki。x-request-id 是关联请求ID，x-trace-id 是SDK真实32位Trace ID，两者区分。未部署可选Tempo后端，不把请求ID日志冒充真实Trace。

evaluation/releases 保存脱敏版本快照、差异、父哈希和健康验证引用；健康登记由可信验收流程调用并附独立验证结果引用；尚未接入生产审批签名。发布工具可查询ReplicaSet发布时间/镜像，证据包含来源、采集时间和内容哈希。

每次验证创建 verify-UUID 租户和独立SKU；先保存证据，再删除该租户拥有的订单/预留/库存，拒绝清理业务租户。verifier-api 证据持久保存于 evaluation/verifier-api，严格验收结果复制到 evaluation/runs/<run-id>/verification。持续probe不会消耗 demo-sku 库存。

告警 resolved 只是检测规则恢复；D1 事件可以仍是 NEW，不冒充 Agent 已完成修复。任务执行、中断接管和进一步可靠性属于后续阶段。

## 当前外部依赖

用户尚无GitHub仓库；本地完整CI通过，远端Actions待建仓库后实际运行。代码可先通过本地Git提交固定版本，不上传凭据或诊断私有文件。
