# Beta 4 发行准备证据

目标 0.8.0b4，尚未发布。当前本机用户仍运行已验证的 Beta 3，不把 source target 当作 live upgrade。

## 已完成

- README 中英文、quickstart 与渠道说明已更新；品牌与包名保留 agent-mailbox。GitHub description/topics增加实际用途关键词，公开默认分支仍是旧版介绍。
- 原始 CI 36827062285：Ubuntu 两组通过，macOS反向DNS启动超时及Windows权限/停止路径失败；卡住的旧运行已取消，保留失败证据，不宣称旧 CI 成功。
- Mac 本地完整回归：Python 3.13：361 passed、2 skipped，147.61s；Python 3.10：361 passed、2 skipped，146.76s，退出0；私有权限、fleet/node和新接缝定向回归192项通过。Windows ACL 修复后真实 CI 的 prepare 已通过，但后续普通 ACP fixture 暴露启动预算不足，整组未通过。
- Mac ARM64 Beta 4 实际压缩包重新解压、启动及退出通过：owner 认证、私有权限、版本、11 个 frozen MCP 工具、context/note 探针回执全部验证，provider 调用0。
- 独立 venv 重新安装 Beta 4 wheel，实际 CLI HTTP 启动/退出、错误 owner 拒绝、私有权限和11个 MCP 工具/探针回执通过。修正验证脚本的安装类型与模块名后通过；没有把脚本误写当作产品缺陷。
- v0.8 tag只构建，registry上传需显式dispatch指定；现有CI和未来发布共用六组回归及三平台原生包门禁。安装包检查从实际压缩包解压后运行，验证owner认证、私有权限、版本、11工具和context/note回执，无provider调用。

- GitHub run 36834003049 的 macOS、Windows、Linux 原生包均成功完成构建、实际压缩包解压、HTTP owner/private/version/quit 和 frozen MCP 11工具/探针回执检查；没有模型调用。该运行完整回归尚未全部通过，不等同可发行。
- 真实 Codex/Claude 四项协作通过：两位员工的实际 context/note 探针、Codex 独立文件修改、Claude 固定补丁审查、Human 验收不自动应用、显式 apply 精确 marker。第一次 Codex 探针未调用工具，被正确判为 PROBE_INCOMPLETE，原始失败日志/结果另存；不是登录失败证据。对照 Beta 3 的真实 exec-wrapper 工具调用后，只澄清 probe 允许客户端发现与传输包装、包装内部仅能调用项目 MCP，再用同一 Luna 模型及 Claude 重测成功，未放宽真实回执判定。

## 待完成

- 新commit GitHub六组回归全通过。
- 三平台原生包实际构建/解压/HTTP/MCP验证全通过。
- 本机保留数据升级；最终提交的产物来源和哈希收口。
- GitHub Beta / PyPI同一commit同版实际上传与下载核验。

Windows fixture 的真实诊断及整改边界见 [Windows runtime fixture](windows-runtime-fixture.md) 及 [Git 行尾语义](windows-git-policy.md)，不通过放宽权限或跳过业务断言换取绿色。

Gatekeeper/SmartScreen首次下载人工体验必须单独说明；源码和CI测试不冒充所有实体桌面已验证。AOCI仍有原布局错误，不在产品运行包里伪造工具成功。
