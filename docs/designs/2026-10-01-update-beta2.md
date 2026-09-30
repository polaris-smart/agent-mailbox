# Beta 2 更新准备设计（human 已同意分步方案）

第一阶段：显式检查 GitHub 固定仓库公开发行版，稳定/Beta 渠道；识别运行中 App/非 editable wheel/源码（无法判断则未知），给出对应操作。没有自动轮询、下载替换或自动回滚。本地 Beta 不冒充公开发行版。

更新准备采用持久 maintenance pause。claim_task 与暂停使用同一个 SQLite 写事务序列，防止检查空闲后新任务抢跑。已有任务继续运行；队列保留。准备按钮先暂停，再检查 active 和远端未确认执行/回执；有阻塞则返回 waiting。重试后私有备份数据库一致性快照与 workbench 内身份配置（排除 runtime、缓存、旧 migration backups、临时 instance/lock）。不复制项目或供应商外部登录。备份完成后仍暂停；明确 resume 才接单，重启不自动取消暂停。失败也不偷偷恢复接单。备份 manifest 含校验值，目录 0700、文件0600；拒绝符号链接，空间/内容大小受限。保存在 home/update-backups，GUI不会通过HTTP下载私钥。

节点版本能力新增可选 v1 标识。不改变 v1 现有协议，不把未报告的旧节点说成已兼容。认证请求报告 app_version/fleet_protocol；UI明确同版本/不同版本但协议兼容/未知/协议不兼容；未来或错误协议拒绝新 claim，已领取任务仍可上传回执。报告是节点自述，不是软件供应链证明。

## UI API

GET /updates/status -> {current_version,channel,installation:{kind:app|wheel|source|unknown,platform,architecture,home,executable,instructions:[{zh,en}]},maintenance:{paused,active_tasks:[{id,title,status}],queued_count},check:null|{status:update_available|current|no_releases|error,checked_at,latest:null|{version,url,notes},error:null|{code,message}},last_backup:null|{path,created_at,files,bytes,verified},nodes:[{id,name,version,protocol,status:matched|compatible|unknown|incompatible,is_local}]}
POST /updates/channel {channel:stable|beta} -> same status (invalidate previous check)
POST /updates/check {} -> same status (network errors reported in check.status=error, prior latest never silently reused)
POST /updates/prepare {} -> {status:waiting|ready,backup:null|metadata,...GET status fields}; errors normal HTTP API error, paused remains true
POST /updates/resume {} -> same status; notify local/fleet engines

Frontend uses direct API with local about-page state and explicit buttons; global bootstrap adds update_maintenance for persistent banner across views, no sensitive backup contents. Network check explicit only, release URL constrained fixed GitHub repo, notes textContent and capped. Version selection compares numeric semantic tuple and a/b/rc prerelease; no naive lexical order. Beta channel includes stable+beta, stable excludes prereleases. Platform guide never executes arbitrary shell release content.

Interfaces: store.update_maintenance(), store.pause_updates(paused:bool), store.update_channel(channel=None). Fleet agent owns version protocol module + fleet fields and remote claim guard; parent owns update service/backups/store/API; UI agent owns workbench.js/api.js/css and browser verification. Parent integrates tests and actual home upgrade. Version0.8.0b2; no publicpush/release/serverdeploy.
