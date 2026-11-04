# 云巡 SRE Agent

UCloud 校招 AI 比赛赛道 B：线上故障处理。从真实观测到受限修复，再通过业务事务确认恢复。

当前提交路线为 H5：确定性规则优先，证据不足或规则未覆盖时回退 HolmesGPT / DeepSeek；默认逐次人工审批，H2b关闭。支持恢复依赖地址、Service端口、有限副本数和资源配置，不执行模型生成的任意Shell。

- [参赛技术文档](docs/COMPETITION_TECHNICAL.md)
- [运行、复现与环境边界](docs/SUBMISSION_REPRODUCTION.md)
- [当前验收记录](docs/H5_ACCEPTANCE.md)
- [自主贡献与上游归属](CONTRIBUTIONS.md)

已完成：6次针对性日志补采现场恢复、7项真实控制链路验收、3项真实浏览器验收及JSON/Markdown下载。恢复须通过120秒、120请求、12次业务检查；拒绝/停止不记为恢复。当前H5新版尚未重跑完整120项正式矩阵。

历史共同接口对照：B2恢复81/120，B3为86/120，补强前H5扩大验证95/120。它们不是同步随机盲测；不宣称全面优于完整HolmesGPT。定向6/6不拼接成101/120。历史报告保留并按版本解释，见[旧入口](docs/HISTORY_README_PRE_H5.md)。

依赖用uv.lock和frontend/package-lock.json固定；Python>=3.12，Docker、kind/Kubernetes、PostgreSQL、Prometheus、Loki及独立业务验证器。HolmesGPT固定版本见docs/HOLMESGPT-COMMIT.txt；模型API Key通过本地私密配置提供，禁止提交密钥和kubeconfig。

本仓库提供源码，视频及本地演示交付说明随参赛材料单独提供。简历和身份信息由参赛人自行填写。本项目目前为演练环境验证，未声称企业生产客户验证。
