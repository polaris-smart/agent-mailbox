# Beta 3 接入、独立工作区与交付验收

Human 已批准按评估继续迭代，本轮实现三个已选优先项，不公开发版，不部署HK/US，不扩展ERP。

## 接入验证

GET /employees/{id}/onboarding?project_id=... 返回 {employee_id,project_id,checks:[{id,status:passed|blocked|unknown,message:{zh,en},action:{zh,en}}],probe:null|{task_id,status,context_seen,note_seen,verified},shared_native_identity:true}；仅查看，不后台调用LLM。原生登录和任务执行验证分开；App未有适配器明确blocked，远端原生状态unknown。POST /employees/{id}/verify {project_id} 为已入组、本机受支持CLI创建显式只读验证任务，会消耗原生模型额度和按现有权限确认。任务需调用绑定项目context和写入随机marker项目note；成功以工具实际调用及任务正常结束证明，不凭模型文本“成功”。重复active验证返回同一task。workspace_mcp在有效task/run边界记录context和note步骤；取消/退役/离组不认为验证成功。

## 修改任务隔离

本机 workspace-write 使用独立Git worktree，记录仓库根路径及起始HEAD；原仓库需clean（含未跟踪文件）且有commit，非Git/subdir不默默回退原目录。只读任务保持原目录。工作区不是OS安全沙箱，同设备原生登录默认仍共享。

准备路径home/task-workspaces/{taskid}，保留成果，不自动删除。workspaces.prepare_workspace(store,task,project)->project copy with isolated path；capture_delivery(store,task,project,result)->persist delivery，不能捕获失败时假装完成；task_delivery(store,taskid)->{workspace:null|{path,base_commit,source_path},files:[...],diff:string,summary:string,verification:{status,notes},applied:bool,can_apply:bool,...}。验证结果明确区分employee reported与系统事实。apply_delivery仅owner HTTP且task已human accept/done，显式confirm，原仓库HEAD/状态校验、固定patchhash、gitapply--check后应用；不自动commit/push。验收不等于合入。失败/取消保留工作区检查，绝不自动合入。Git与文件访问有超时、大小边界、路径/符号链接检查、错误不泄露环境。远端修改任务暂不提供自动工作区/合入，不允许沿用原目录修改作为隔离任务；远端只读/邮件协议保留。

## 交付与退回

GET /tasks/{id}/delivery 展示固定结果、文件diff、工作区/基线、捕获错误与可合入状态。POST /tasks/{id}/apply {confirm:true} 显式合入已验收的本机文本patch。POST /tasks/{id}/follow-up {note} 对review任务退回并事务创建后续task，保留原记录，prompt带原任务ID/交付与人类要求，新增task_links表relation=follow_up用于详情关联。若workspace-write，后续worktree继承原任务捕获patch但原仓库基线必须匹配；不能让员工在缺少上一轮修改的空白目录盲改。POST现有review reject仅保存退回仍支持历史客户端；新UI使用follow-up。任务详情增加关联任务；可显示实际事件与交付来源，未检测的测试不冒充已通过。

## 存储和API契约

Schema9新增workspace模块提供WORKSPACE_SCHEMA，由store迁移执行；父任务新增 employee_probes 与 task_links。接口实现者只操作自身新表，不修改其他实体定义。

UI拥有assets及浏览器验证；工作区专才拥有workbench_workspaces.py与独立测试；父任务拥有store/engine/http/onboarding/MCP与接缝测试。独立工作区先验证真实Git仓库并发写、退回继承、合入拒绝旧基线，再验证真实原生CLI。Beta3不宣称所有18个入口均可执行、Windows/HKUS均验收；发布包和升级保留现有业务行。
