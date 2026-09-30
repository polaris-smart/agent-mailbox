# Beta 2 更新准备验证

版本0.8.0b2，2026-10-01；本地发行产物，未公开 push/发版，未访问 HK/US。

产物和证据：本机 `/Users/interia/tools/agent-mailbox-builds/v080-beta2/`。最终各项结果以该目录 JSON、JUnit、SHA256SUMS 为准，不含凭据。

- `tests.xml`：最终 Mac 全量回归；涵盖 schema7→8私有迁移备份、身份保留、领取暂停事务竞态、准备失败不退出、不确定领取、已结束回执清理、协议兼容和现有资料/邮件能力。
- `update-ui-real.json`：8项真实HTTP/SQLite流程，无API路由拦截；只有公开发行信息provider使用测试替身。运行任务进入等待、不取消；结束后实际私有备份；模拟符号链接备份失败后仍可恢复接单；渠道与暂停重载保留；手机中文/英文深色无溢出，零页面异常。
- `update-ui-contract.json`：9项独立UI契约场景，含发行说明文本展示、未知/不兼容节点与恢复入口；不是远端服务器实测。
- `public-release-check.json`：显式访问真实固定GitHub公开release，当前公开最新0.7.6，本地0.8.0b2不会被当作应降级。GitHub公开信息不等于本地Beta已公开发布。
- `package-verification.json`：最终wheel/App独立启动、退出、版本/协议原文和员工/项目基本入口。
- `frozen-mcp-verification.json`：最终App内项目MCP工具、固定资料读取及员工文本提案。
- `live-upgrade-verification.json`：本机原home升级和原有业务行校验；`live-updates-verification.json`：安装方式、私有备份、暂停/恢复、业务行保留。
- `ui-verification.json`：本机运行App的中英文、协议与手机检查，配更新页截图。

备份测试实际重建隔离home，验证数据库与项目员工凭据一致。目录0700、文件0600，manifest逐文件SHA-256；运行时、缓存、日志、旧迁移备份、临时owner token/lock和外部项目源码不包含。数据库备份不是供应商登录或项目代码的完整备份。备份是生成时刻的快照，后续记录不属于它。没有自动还原程序/数据库，也未完成自动下载替换。

节点报告为已认证设备自述。不同app版本且v1协议可继续；未报告旧节点保持unknown；不兼容协议停止新领取，但已有回执保留。不确定网络领取先记录持久marker，重连确认已有执行再清除，不能把未确认状态当作空闲。

Beta 1的真实Codex→Claude及Ubuntu ARM64完整回归作为历史基线，不宣称本轮重新执行真实LLM或Ubuntu全量。Windows和真实HK/US仍未验证。AOCI rules/overview/最终maintain实际返回已知index_invalid/path映射错误，未伪造维护成功。
