# Beta 4 发行准备证据

目标 0.8.0b4，尚未发布。当前本机用户仍运行已验证的 Beta 3，不把 source target 当作 live upgrade。

## 已完成

- README 中英文、quickstart 与渠道说明已更新；品牌与包名保留 agent-mailbox。GitHub description/topics增加实际用途关键词，公开默认分支仍是旧版介绍。
- 原始 CI 36827062285：Ubuntu 两组通过，macOS反向DNS启动超时及Windows权限/停止路径失败；卡住的旧运行已取消，保留失败证据，不宣称旧 CI 成功。
- Mac 本地完整回归返回0；私有权限、fleet/node和新接缝定向回归192项通过。Windows ACL仅由Windows实际检查证明。
- v0.8 tag只构建，registry上传需显式dispatch指定；现有CI和未来发布共用六组回归及三平台原生包门禁。安装包检查从实际压缩包解压后运行，验证owner认证、私有权限、版本、11工具和context/note回执，无provider调用。

## 待完成

- 新commit GitHub六组回归全通过。
- 三平台原生包实际构建/解压/HTTP/MCP验证全通过。
- 最终wheel、真实模型协作与本机保留数据升级。
- GitHub Beta / PyPI同一commit同版实际上传与下载核验。

Gatekeeper/SmartScreen首次下载人工体验必须单独说明；源码和CI测试不冒充所有实体桌面已验证。AOCI仍有原布局错误，不在产品运行包里伪造工具成功。
