# H5 产品候选接入

当前候选仍在独立验收，不能将机器试验称为用户亲自验收。冻结 B2 与历史 H5 数据不变。

标准 Dockerfile.d4 包含 runner_h5、共同接口 runner_common 和原 runner_v3。标准部署脚本默认生成 H5 只读诊断配置；工作进程仍使用原人工逐次审批、版本绑定授权和持久化执行流程。

- `CLOUDSRE_ROUTING_MODE=h5`：六类只读证据快照，规则唯一命中后提交方案；无匹配或采集失败转共同 B3。
- `CLOUDSRE_ROUTING_MODE=b3`：相同快照和共同模型契约，停用规则分支。
- `CLOUDSRE_DIAGNOSTIC_RUNNER=/app/diagnosis/runner_v3.py`：保留旧产品诊断入口；仅在无进行中的诊断任务时切换部署。
- H2b 默认保持关闭。控制台仍默认逐次人工审批，规则命中不等于授权写入。

新增路由记录包含规则/模型、采集错误与快照哈希，写入工作流和导出报告的 diagnostic_routing 字段。该字段中的 execution_authorized=false 描述诊断阶段无执行权限；最终授权以 operator_events、plan_decisions 和真实写入审计为准。

日志能力变更：inventory_rejected 增加准确上游与请求路径；读取该事件；同源路径漂移且真实404、目标与操作完全匹配才进入新增证据条件。409/422或不匹配地址不得据此修复。策略与规则共享判断，原资源身份、可信基线、端点健康与审批门槛保留。旧业务镜像没有这些字段时仍应转人工，不能推造字段。

部署前必须为新的业务镜像重新实测健康基线，不能伪造旧基线的镜像兼容性。完整验收结果另存于 D盘 H5_LOG_SUPPLEMENT_RESULTS.json 和 H5 产品验收记录。
