# 来源与自主工作

HolmesGPT 是上游诊断引擎，未将其源码视为自主开发；版本固定于 vendor 中的 commit。上游许可证为 Apache-2.0，分发前保留 LICENSE/NOTICE 并检查依赖。

自主工作包括事件 API 与幂等持久化、只读工具与 HolmesGPT 进程适配、证据和可信基线约束、受限执行与读回、持久化租约/动作意图、独立业务验证、人工审批/拒绝/停止控制台，以及隔离的真实对照评测。状态编排借鉴 STRATUS 的思路，不将通用工作流思想或上游诊断引擎声称为原创。

D5 的具体增益与负面结果见 [D5 来源和贡献](docs/D5_CONTRIBUTIONS.md)、[H2b 最终结论](docs/H2B_FINAL_DECISION.md)。可选 H2b/H5 均默认关闭；D6 的当前状态和能力边界见 [D6 进度](docs/D6_PROGRESS.md) 与 [架构说明](docs/D6_ARCHITECTURE.md)。尚未完成的实机验证不表述为已证明的创新效果。
