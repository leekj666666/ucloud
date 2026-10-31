# D6 同主机清洁部署复现

状态：独立源码已准备；依赖安装、独立服务部署和真实故障闭环尚未完成。本项不算第二台机器测试，也不算从零安装 Windows、WSL 或 Docker。

## 现有准备

独立目录为 WSL `/home/lee/sre-d6-cleanroom-c39e242`，位于 D 盘承载的 WSL 磁盘。主源码固定到 `c39e2429b83d4ad4a2f702a6c17e3315d59ceb3d`；HolmesGPT 固定到 `4efc18f0cd1fc3e65229bb27d236ed057c6f2424`。

源码来自本机已有 Git 对象缓存的独立浅检出，并记录了 257 个主项目受控文件的 SHA-256；未复制旧 `.venv`、`node_modules`、私有环境、健康基线、注入答案或运行证据。详细凭据无关清单为 `D:/uclaude/D6_CLEANROOM_SOURCE.json`。使用本机源码/下载缓存会如实标注，不宣称重新从互联网下载全部内容。

## 安装步骤与验证目标

在正式评测释放资源后串行执行，使用独立目录和全新虚拟环境。以下是待执行的依赖安装命令，不表示本项已经通过：

```bash
cd /home/lee/sre-d6-cleanroom-c39e242
/home/lee/sre-tools/bin/uv sync --frozen --all-groups
cd frontend
PATH=/home/lee/sre-tools/node-v24.22.1-linux-x64/bin:$PATH npm ci --no-audit --no-fund
PATH=/home/lee/sre-tools/node-v24.22.1-linux-x64/bin:$PATH npm run build
cd ..
```

主应用与 HolmesGPT 需要分开的 Python 环境，两者上游依赖范围不同。清洁部署资料另外保存 `docs/HOLMESGPT-REQUIREMENTS.lock.txt`，它逐项对照现有 Holmes 独立环境，仅移除了指向旧项目绝对路径的 Holmes 源码引用。该文档型锁定清单在正式产品冻结后补充，不改变正在评测的镜像。安装前将已审计的清单单独复制到清洁部署资料目录，并记录复制文件的哈希；不悄悄修改原冻结源码树。

```bash
# 从清洁源码根目录执行；锁定清单路径以实际交付资料为准
/home/lee/sre-tools/bin/uv venv .venv-holmes --python 3.12
/home/lee/sre-tools/bin/uv pip install --python .venv-holmes/bin/python --no-deps -r /absolute/path/HOLMESGPT-REQUIREMENTS.lock.txt
/home/lee/sre-tools/bin/uv pip install --python .venv-holmes/bin/python --no-deps ./vendor/holmesgpt
```

验证新环境的实际分发包/版本、主依赖锁文件、上游提交与记录一致，运行迁移、前端构建与镜像构建，保存命令、退出码及镜像 ID。若使用缓存，仍须新建虚拟环境并重新安装，不能把旧环境目录复制过来算复现。

## 部署隔离

新 Compose 项目、数据库与卷、kind 集群、namespace、证据目录、健康基线、观察器及本机端口全部独立命名。当前部分早期部署脚本固定 `sre-dev`、`sre-infra`、端口与规范项目路径，不能在清洁目录直接运行它们，以免修改原环境。独立部署适配文件应保存在清洁部署资料目录，明示命名和地址变换，保留原源码哈希。

禁止为了演示方便把原数据库、事件记录、业务数据、健康基线或成功报告复制进新环境。模型凭据通过私有环境单独提供，不写入源码、构建上下文或归档。新环境可复用明确记录的本机下载缓存，但产品镜像应重新构建并记录 provenance。

## 必须完成的真实验收

1. 从空数据库执行真实迁移，启动业务三服务、监控、日志、事件入口与独立验证器，采集合格健康基线。
2. 建立独立受限身份，启动默认逐次人工审批的生产链路。
3. 对一个明确标记的真实演练故障完成调查、证据/方案审阅、批准、实际修复和读回。
4. 按同样标准执行完整 120 秒、120 请求、12 次完整业务检查、监控/严重告警/资源/P95 门槛；保存原始样本。
5. 核对报告与现场一致、原环境未被改写，最后归档独立环境清单与哈希。失败原样保留，清理不能算 Agent 修复。

以上全部完成后，才把本项标为“同主机清洁部署复现通过”。
