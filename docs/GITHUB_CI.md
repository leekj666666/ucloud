# GitHub 仓库与 D1 远端验收

仓库：https://github.com/leekj666666/ucloud ，默认分支 `main`。仓库保持私有。

原本地 D1 验收提交 `071a5bca80a94e3963fb6489c9a8c18efc5b1516` 完整保留。后续仓库接入调整 CI、文档和普通文件的可执行权限，不修改业务实现、测试断言、部署逻辑或依赖锁。

验收判据：锁定依赖安装、Ruff、至少21项测试全部通过且无跳过、两项真实 PostgreSQL/TCP 集成测试必须执行、镜像构建和容器导入成功。并发幂等集成测试仍为20轮、每轮4个同步请求。

Actions 在 checkout 后初始化证据目录；无论后续成功或失败均上传已有证据。执行到测试时，持久保存 JUnit、gateway/order/inventory 原始服务日志和测试业务归档。Artifacts 保留14天；长期证据下载到 D 盘。依赖安装前/初始化前的失败仍从 Actions 原始 job 日志提取，不声称一定已有服务日志。

## 首次远端失败与修正

- 提交：`6d8a847a05ae5b56c25a17abde774ce231322fda`。
- 运行：https://github.com/leekj666666/ucloud/actions/runs/39921295634 。
- 真实结果：Ruff报45项EXE002，测试和镜像步骤未执行；artifact因测试尚未建立目录而未产生。
- 原因：普通Python源文件错误带入Git可执行标记；Ruff此规则不在WSL执行，在GitHub原生Linux环境发现。
- 修正：普通文件统一为0644，不关闭检查规则；在checkout后初始化证据目录。
- 原始job日志已在本地保存；该次失败不计为21项测试失败，也不计为完整验收通过。

规则依据：[Ruff EXE002 官方文档](https://docs.astral.sh/ruff/rules/shebang-missing-executable-file/)。后续实际结果以 [Actions](https://github.com/leekj666666/ucloud/actions) 中与提交对应的完整运行记录为准。

本地原D1的78个文件和原指纹已归档至 `D:/uclaude/d1-evidence/frozen-071a5bc`；最新远端结果在 `D:/uclaude/GITHUB_CI_REPORT.md` 与 `GITHUB_CI_EVIDENCE.json` 保存。原D1报告属于历史本地验收快照，不用新结果改写旧失败。
