# 自主贡献与开源归属

HolmesGPT为上游诊断引擎，不属于本项目原创；固定commit见docs/HOLMESGPT-COMMIT.txt，许可证副本见docs/HOLMESGPT-LICENSE。Python/npm依赖按锁文件安装；分发依赖本体时遵循各自许可证并保留适用声明。

自主工程工作：事件API与幂等状态；持久化工作流、数据库租约与重启接管；精确方案审批/拒绝/停止；UID、资源版本与完整spec前置校验；未知写入结果读回重建；独立订单/库存事务验证；证据ID和哈希绑定；H5规则与模型回退集成；结构化路径故障证据、有上限的日志补采；中文控制台、审计导出与故障评测设施。

H5规则来自既有B0规则，不包装为新算法。借鉴成熟项目的编排/验证思想，不宣称这些机制均为首次提出。当前工程增益及限制见docs/COMPETITION_TECHNICAL.md及H5验收记录，原始失败保留。

Prometheus、Alertmanager、Loki、Alloy、Kubernetes/kind、PostgreSQL、FastAPI、React等为第三方组件；依赖和版本以锁文件、镜像摘要和上游版本记录为准。未将第三方代码改名作为自主成果。
