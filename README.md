# 云巡 SRE Agent

当前D1本地完整验收通过，D2仍待补齐。详见 docs/D1_COMPLETION_REPORT.md 和 docs/D1_OPERATIONS.md。

完整本地CI：PYTHONPATH=backend .venv/bin/python scripts/quality_d1.py。
完整D1验收：PYTHONPATH=backend .venv/bin/python scripts/verify_d1_complete.py。
验收真实业务路径经过Docker网络、NodePort和Kubernetes Service；保留所有失败和清理记录。

原计划保存在docs/plans，依赖由uv.lock固定；HolmesGPT版本和许可证见docs/HOLMESGPT-COMMIT.txt与docs/HOLMESGPT-LICENSE。凭据不进仓库。
GitHub仓库尚未创建，远端CI待验证；正式留出评测和新环境复现按后续阶段完成。
