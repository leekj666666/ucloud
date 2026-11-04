# Tm11GPT 运行与复现

## 已验收演示环境
Windows+Ubuntu24.04 WSL2、Docker Desktop、16GB内存，真实DeepSeek官方API；不需要GPU。开发机当前入口localhost:28080，仅本机可访问。访问码与API Key均不随包分发。可查看已恢复c2955251、已拒绝a323f0e4、已停止29468b5f等真实事件。当前业务已清理健康，界面创建任务不等于注入故障。

## 从源码开始
克隆本仓库并使用最终提交版本。需要Python3.12+、uv0.13.5、Node24.22.1及npm、Docker、kubectl、kind。执行uv sync --frozen、在frontend执行npm ci及npm run build。工具调用运行组件需按docs/HOLMESGPT-COMMIT.txt固定commit安装到独立.venv-holmes；不要使用未固定的最新版本。

现有部署脚本的默认工作目录是~/sre-project，部分本地模型配置默认在/mnt/d/uclaude/model.env。首次运行前应核对这些路径，不宣称任意机器一键安装。模型配置仅包含本机OPENAI_BASE_URL、DEEPSEEK_API_KEY、MODEL_NAME；禁止把真实Key填入仓库。

环境部署分层入口：scripts/deploy_d1.py部署业务与观测；scripts/provision_d3_rbac.py配置受限身份；scripts/prepare_d4_baseline.py独立验证健康基线；scripts/deploy_d4_runtime.py --no-workers构建Tm11GPT控制台与任务进程。启动前核对数据库、两组namespace、模型端点、受限身份、Holmes路径、验证器及观察器。基线必须针对当前实际业务镜像重新通过完整业务验证，不能复制他人基线冒充验证。

设置CLOUDSRE_ROUTING_MODE=h5（默认）或b3；旧诊断回退入口由CLOUDSRE_DIAGNOSTIC_RUNNER控制。额外反馈重规划分支关闭，保留逐次人工审批。生成凭证仅留infra/.generated；Kubernetes短期令牌到期须由部署维护流程续签。

## 检查
配置隔离SRE_TEST_DATABASE_URL后运行uv run ruff check backend tests scripts diagnosis benchmarks，以及uv run pytest tests -q -ra。不要把因未配置PostgreSQL而跳过的测试记成完整通过。完整GitHub工作流还包括镜像、12项浏览器契约和告警规则检查。

## 真实操作
准备受控故障后登录，发起调查，阅读证据及目标版本，在审批栏批准或拒绝；调查期间可停止。批准后等完整120秒业务窗口，再从报告页导出JSON/Markdown。不得把部分采样或提前停止标记为恢复。评测注入脚本只用于隔离演练环境，不面向生产集群。

## 本轮复现边界
旧D6做过同主机清洁部署；Tm11GPT新版完成当前开发机真实部署及浏览器/进程验收。当前尚未声明从最终GitHub提交开始在完全空白另一台主机复现通过。上述先决条件和人工核对保留，不以文档中的命令本身代替实测。
