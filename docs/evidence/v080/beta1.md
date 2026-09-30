# Beta 1 本地验证记录

版本：0.8.0b1；日期：2026-10-01。未公开推送或发行，未部署 HK/US。

可复核产物位于本机 `/Users/interia/tools/agent-mailbox-builds/v080-beta1/`；证据不包含凭据。发布前应另行在目标发行环境复验。

| 证据 | 结果 |
| --- | --- |
| tests.xml | Mac 完整 pytest：309 passed、1 skipped，0 failures/errors |
| ubuntu-full-suite.json | Ubuntu 24.04 ARM64：269 passed、41 skipped；未安装 Node/ACPX 的运行时测试全部明示跳过 |
| node-beta-verification.json | Mac→Ubuntu 7 项：配对/身份、注册映射、执行验收、固定资料版本、断线/不确定领取、取消/结束拒绝访问、撤销 |
| actual-collaboration-resources.json | 真实 Codex/Claude CLI 读取固定资料并邮件交接；两任务 human 验收 |
| sidebar-real-ui-evidence.json | 9 项真实 HTTP UI 流程，无 API 拦截 |
| sidebar-extra-ui-evidence.json / sidebar-empty-ui-evidence.json / sidebar-ui-evidence.json | 20 项语言、手机、无项目和布局检查；部分布局使用独立 fixture |
| package-verification.json / frozen-mcp-verification.json | 最终 wheel/App 与 App 内 MCP 独立验证 |
| live-upgrade-verification.json / ui-verification.json | 原有业务行保留、本机版本及实际 UI 验证 |

代码质量：Ruff、格式检查、JS 语法、git diff 检查。schema 7 自动迁移生成私有备份；资源版本、审批和任务 manifest 不可原地更新/删除。查询日志不改变任务或消息状态。

测试节点容器已删除；没有使用用户远端服务器、模型凭据或 SSH 密钥。实际 LLM 协作仅使用已获授权的本机登录身份和隔离项目。

AOCI-Code 当前索引返回 `index_invalid / code_object_path_unresolved`。已记录工具诊断，不宣称完成其认知治理整合。CodeGraph 为可选 POSIX 适配器，Windows 明示不支持。

最终包校验与本机升级的具体结果以同目录 JSON 和 SHA256SUMS 为准；App 为本地 ad-hoc 签名，未公证。
