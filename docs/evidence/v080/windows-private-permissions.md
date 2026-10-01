# Windows 私有访问与启动修复（待 CI 平台验证）

2026-10-01。旧 CI run 36827062285 的 Windows 3.13 日志只有进度中的失败标记及随后线程超时，不能从它恢复全部失败断言。已直接定位的跨平台问题：测试要求 POSIX `0600/0700`，而 Windows `chmod/stat` 只表示只读属性，不保证访问控制；节点停止后仍有线程等待领取锁。

新增 `workbench_private.py`：POSIX 继续精确 chmod；Windows 通过标准 Win32 security descriptor API 设置 protected DACL，允许当前用户 SID、SYSTEM 和 Administrators 完全访问，目录提供子项继承。读取真实 DACL 复核，任何权限 API 错误都向上抛出，不退回 chmod 或无保护继续写。

覆盖工作台数据库/侧文件、迁移/手动备份、owner instance/锁、fleet 身份/配对/TLS key、远端回执与更新备份文件；新文件写敏感内容前处于私有目录或先应用 ACL。现有 mode 安全断言在 POSIX 仍精确检查，在 Windows 改为真实 DACL 检查，不跳过身份、配对或备份行为测试。新增 Windows 专属测试先设置 Everyone ACE，再证明 private helper 移除宽权限。

Fleet TLS bind 不再依赖反向 DNS；实际 TLS 配对测试令 getfqdn 抛错仍完成配对与项目查询。领取停止保留实际 socket 引用，避免 HTTPConnection 已释放 sock 引用而阻塞 response 读取；socketpair 实际阻塞读取在停止后结束。节点 close 等待领取线程释放锁。

Mac 目标回归通过；这不等于 Windows 实机/CI 已通过。最终 Windows 3.10/3.13 CI 结果由主任务记录。未修改 v0.7 runtime、未公开发布。
