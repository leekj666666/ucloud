# 云巡 SRE Agent

当前D1本地完整验收通过，D2仍待补齐。详见 docs/D1_COMPLETION_REPORT.md 和 docs/D1_OPERATIONS.md。

完整本地CI：PYTHONPATH=backend .venv/bin/python scripts/quality_d1.py。
完整D1验收：PYTHONPATH=backend .venv/bin/python scripts/verify_d1_complete.py。
验收真实业务路径经过Docker网络、NodePort和Kubernetes Service；保留所有失败和清理记录。

原计划保存在docs/plans，依赖由uv.lock固定；HolmesGPT版本和许可证见docs/HOLMESGPT-COMMIT.txt与docs/HOLMESGPT-LICENSE。凭据不进仓库。
GitHub仓库：[leekj666666/ucloud](https://github.com/leekj666666/ucloud)。远端CI以[Actions实际记录](https://github.com/leekj666666/ucloud/actions)为准，要求至少21项测试通过、无跳过，两项真实PostgreSQL集成测试必须运行；JUnit和三服务日志按运行号归档14天。D1本地报告保留仓库创建前的验收快照，正式留出评测和新环境复现按后续阶段完成。
