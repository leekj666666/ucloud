# D4 故障指挥台操作说明

本阶段在 D3 的受限执行器上增加人工审批、停止和中文界面。入口为 http://localhost:18080 。访问码由启动脚本写入 `D:/uclaude/console-access.txt`，与模型 API Key 分开；不得提交、截图或归档访问码。

## 本机启动

以下命令在 Ubuntu 的项目目录运行。保留既有 Docker、kind、业务图、验证器、监控和独立健康基线；不得在存在活动任务时重建基线。

```bash
cd /home/lee/sre-project
PYTHONPATH=backend .venv/bin/python scripts/deploy_d4_runtime.py --no-workers
# 确认页面、私有环境和既有任务后启动工作器
docker start cloudsre-worker-d4-1 cloudsre-worker-d4-2
```

`--no-workers` 创建但不启动工作器。默认启动仅处理已显式登记的控制台任务，不把历史 webhook 积压当成自动执行授权。部署会替换 D4 容器；活动任务应先停止或等待完成。诊断、证据和检查点保留在 `evaluation/d4-runtime`，D3 原始归档不变。

访问码是单机操作员会话，不代表企业 SSO 或多租户身份系统。会话使用 HttpOnly、SameSite=Strict Cookie，重启 API 后需要重新登录。仅在本机回环地址开放；上线到云环境前需另行配置 HTTPS、身份系统及网络边界。

## 演示步骤

1. 登录后选择事件，或点击“发起调查”填写观察到的症状。服务与 namespace 必须正确，不填写已知根因或修复答案。界面发起调查不会注入故障。
2. 保持默认“逐次人工审批”。工作器离线时任务显示等待；调查中可查看真实检查点和证据，未批准前不能执行变更。
3. 阅读方案、完整模型判断、证据和变更差异。右栏核对目标、参数、资源版本与方案哈希，勾选后批准，或填写原因拒绝。
4. 批准只对当前方案有效；UID、resourceVersion、完整 spec、健康基线或证据冲突仍会拒绝执行并转人工。
5. 在业务验收页观察实际采样。只有完整 120 秒、每秒一次共 120 请求、至少 12 次完整业务不变量检查、零错误与既定 P95/监控门槛通过，才能显示已验证恢复。
6. 在报告页查看用量、影响和未解决项，下载 JSON 或 Markdown。未知费用和未知 usage 不填零。

“停止任务”先记录停止请求，再由工作器确认。在途 Kubernetes 请求不能撤回，必须读回确认结果；结果不明时转人工。停止后不开始新的外部写入。已应用变更保留，停止不等于回滚或业务恢复。验证中停止只保留部分窗口，不能算恢复成功。

## 验收命令与隔离

```bash
PYTHONPATH=backend .venv/bin/python scripts/quality_d4.py
PYTHONPATH=backend .venv/bin/python scripts/verify_d4_permissions.py
cd frontend
npm run test:browser
```

完整检查使用独立 PostgreSQL 容器；要求至少 113 项后端测试且零跳过。浏览器契约测试使用独立 SQLite 和明确标记的测试夹具，无模型、Kubernetes 或业务库凭据，不证明真实业务恢复。GitHub CI 使用 Chromium；Windows 可设置 `CLOUDSRE_CHROME_CHANNEL=chrome` 使用已安装 Chrome。

实机浏览器脚本 `frontend/scripts/live_acceptance.cjs` 从 Windows 调用既有 WSL 环境，支持 `approve`、`reject`、`stop_investigation`、`stop_verification`、`stale_plan`。原始结果落在 `D:/uclaude/d4-browser-runs`。只可逐例运行，避免同一业务图故障互相干扰。脚本会真实注入故障、调用官方模型并产生费用。故障注入和清理仅在评测器中；负向案例的评测清理不得算 Agent 自主恢复。

```powershell
cd D:\uclaude\dev-stage\frontend
D:\node\node.exe scripts\live_acceptance.cjs --case=approve
```

Windows `dev-stage` 仅供浏览器驱动使用；规范源码位于 WSL `/home/lee/sre-project`。更新脚本时只同步指定文件，禁止用旧快照覆盖规范源码。

## 保留范围

D4 演示的是本地两组 namespace、三服务依赖图及四种受限动作；不宣称任意生产故障都可自治恢复。诊断与执行分离，worker 无模型 Key，诊断无执行身份。数据库权限允许 worker 锁定控制行，但不允许改写模式、清除停止或创建审批。D5 的增益比较、消融和 D6 的留出/新环境复现仍需另行完成。
