# D3 验收结果索引

2026-10-25T07:01:52.019733+08:00。D3 判据已通过；完整结论与边界见 D3_COMPLETION_REPORT.md。

| 运行 | 通过 | 类别 | 评测清理 | 错误类别 |
|---|---|---|---|---|
| d3-baseline-366bd63841c0 | True |   | None |  |
| d3-ci-961911e6a4eb | True |   | None |  |
| d3-ci-d09b44cb927e | True |   | None |  |
| d3-ci-f549c397d15f | True |   | None |  |
| d3-ci-fbceba9e602d | True |   | None |  |
| d3-compensation-43972aea0265 | True |   | False |  |
| d3-config-017874a4635c | True | config after_write | False |  |
| d3-config-11c3abc43c68 | False | config  | True | AssertionError |
| d3-config-2c2b5900d34f | True | config before_diagnosis | False |  |
| d3-config-3f0a37f50564 | True | config  | False |  |
| d3-config-6258bd28ca68 | True | config before_write | False |  |
| d3-config-6acdcd2355d1 | True | config  | False |  |
| d3-config-749b6bc25d61 | True | config verification | False |  |
| d3-config-8ced86126f7b | False | config  | True | AssertionError |
| d3-config-9ee080a46e9f | False | config  | True | AssertionError |
| d3-config-e9c2b4531c50 | True | config after_diagnosis | False |  |
| d3-database_disconnect-7827e3bd4ec5 | True | database_disconnect  | False |  |
| d3-database_disconnect-873b602ffda1 | False | database_disconnect  | True | AssertionError |
| d3-database_disconnect-deaac1c695fb | False | database_disconnect  | True | AssertionError |
| d3-dependency-5ab0db08fe51 | True | dependency  | False |  |
| d3-dependency-98c7fb4cc1b9 | True | dependency  | False |  |
| d3-dependency-fe193ab085e2 | False | dependency  | True | AssertionError |
| d3-diagnostic_timeout-3e1d51a08da5 | True | diagnostic_timeout  | True |  |
| d3-execution_denied-a38a359725ee | True | execution_denied  | True |  |
| d3-execution_denied-d7a6a6369504 | False | execution_denied  | True | AssertionError |
| d3-false-recovery-821285e5ead0 | True |   | True |  |
| d3-parallel-9acc0f72d511 | True |   | False |  |
| d3-parallel-e4704faac363 | False |   | False | CalledProcessError |
| d3-resources-251427451096 | False | resources  | True | AssertionError |
| d3-resources-8f02f8d51d4f | True | resources  | False |  |
| d3-resources-ad603b1a1d42 | False | resources  | True | AssertionError |
| d3-resources-caa9048190d3 | True | resources  | False |  |
| d3-route-06c7b0729036 | True | route  | False |  |
| d3-route-22e0f798f295 | False | route  | True | EvaluatorInterrupted |
| d3-route-234886cfc312 | True | route  | False |  |
| d3-route-542bf85a0703 | False | route  | True | EnvironmentInterrupted |

无结果的中断目录保持原样，不推定通过。开发迭代版本不同，不能用此表计算泛化成功率；补偿属于执行器合同测试。
