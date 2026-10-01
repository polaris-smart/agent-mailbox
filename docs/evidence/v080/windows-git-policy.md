# Windows 独立工作区 Git 行尾语义修复

2026-10-01。Windows CI runtime 阶段已走过，下一失败为 seam 修改任务进入 failed 而非 review。系统 Git 在 Windows 常用 core.autocrlf=true；原工作区助手强制 GIT_CONFIG_NOSYSTEM=1，会在同一仓库忽略系统行尾规则，误判 CRLF 工作树与 LF 索引不同。

已在 Mac 的隔离临时 Git 仓库复现：通过测试 wrapper 为原生 Git 指定合成 system 配置 core.autocrlf=true，并隔离 global 配置，不写用户或系统 Git 配置。原生 git status 实际为空；修复前 prepare_workspace 抛 workspace_dirty。移除强制 NOSYSTEM 后，同一测试完整通过原生 clean 检查、CRLF worktree、固定 diff、Human 验收后显式 apply，最终源文件保留 CRLF 的精确 approved 内容。

产品仍剥离继承的 GIT_DIR、GIT_WORK_TREE、GIT_CONFIG_SYSTEM 等调用者覆盖，任务不能注入配置位置；测试合成配置仅由测试 subprocess wrapper 控制。命令仍禁用 hooks、fsmonitor、external diff、textconv，保留时间/大小边界与基线/路径校验。未删除或放宽业务断言，也没有跳过 Windows 工作区功能。

首 seam 断言另加安全 error/capture_error/执行阶段数诊断。最终真实 Windows CI 结果由主任务收口，Mac 合成配置复现不当作 Windows 全量通过。

本轮仍禁止全局 Git 配置（GIT_CONFIG_GLOBAL 指向空设备），所以不承诺用户 global core.autocrlf/eol/safecrlf 的自定义规则均被保持；仓库本地配置或 .gitattributes 应明确非系统行尾策略。后续可仅有界读取这三个键、严格白名单后以命令参数注入，不能为保留行尾规则而加载任意全局 filter/hooks。当前目标是修复已真实复现的系统默认策略冲突。Mac workspace+seam 17 项完整通过、无跳过。
