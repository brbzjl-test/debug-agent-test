# 现场调试助手：P0 实现计划

版本：v1.1｜日期：2026-09-16｜状态：P0 已实现并完成 macOS 验证

## 1. 实施边界

P0 交付一个与业务程序独立部署的现场调试助手。应用不修改 `src/` 下的启动、驱动、遥操、控制路由和日志代码；业务仓库仅作为只读 Snapshot 与 Codex 分析数据源。

现场初始配置只有：业务 Git 仓库地址、本地代码路径，以及可选的预期 ROS 拓扑。系统、进程、日志、Git 状态、硬件和当前 ROS 图均自动发现。Codex 禁止修改代码是不可配置的内置安全策略。

P0 的 Solution 只强制填写实际解决方案；验证方法为可选补充。CI/CD、hotfix 分支和提交门禁不在本工具内实现。

## 2. 目标架构

```text
开机常驻 field-support-core
  ├── SQLite：问题、消息、事件、Solution、Outbox
  ├── 仅监听 127.0.0.1 的会话令牌 API
  └── 事件调度器

图形登录后常驻 field-support-float
  └── 点击后加载 Web UI：Init / History / Chat

按事件运行
  ├── snapshot-worker：建单及首条现象后只读采集
  ├── codex-runner：有现象且联网后只读分析
  ├── handoff-client：选择人工后连接服务端
  └── sync-worker：只处理已有 Outbox，不扫描 Base

服务端 field-support-gateway
  ├── 飞书问题话题和 Solution 卡片
  ├── 人工事件与 Solution 持久化
  └── 事件触发的多维表格投影
```

本机 Core 是离线期间的权威记录。服务端 Gateway 是人工处理事件与 Solution 的权威记录。Base 仅用于查询、分派和统计，不参与业务状态判定。

## 3. 代码边界

```text
field_support_app/
  contracts/                 跨模块配置、事件和 API 契约
  src/field_support_agent/
    domain/                  ID、sub-ID、状态机、Solution 版本
    storage/                 SQLite、事件、Evidence、Outbox
    api/                     本机 API
    collectors/              系统、进程、日志、Git、ROS 只读采集器
    analysis/                Codex 只读执行和结果解析
    integrations/            Gateway 客户端
    service/                 按需任务调度
    ui/                      Web UI 与桌面浮窗壳
  deploy/                    systemd 与图形会话启动配置
  tests/

field_support_gateway/
  src/field_support_gateway/ 飞书回调、Solution、同步、Base 投影
  tests/

tests/fixtures/               本仓库内只存测试契约和期望结果
```

macOS 模拟业务程序使用独立仓库 `git@github.com:brbzjl-test/debug-agent-test.git`，本地默认路径 `/Users/brb/项目/debug-agent-test`。

## 4. 实现阶段与分工

| 阶段 | Owner | 交付物 | 依赖 | 完成门槛 |
|-|-|-|-|-|
| M0 契约基线 | 主线 | 配置、状态机、事件格式、目录边界 | 无 | 契约测试可被各模块独立引用 |
| M1 Core | Agent A | ID/sub-ID、SQLite、时间线、Outbox、本地 API | M0 | 并发/重启不重复建单，状态门禁生效 |
| M1 Web UI | Agent B | 浮窗壳、Init/History/Chat、API 客户端 | M0 | 三页跳转正确，收起不抢焦点 |
| M1 模拟业务 | Agent F | macOS Python 模拟程序和故障场景 | M0 | 可产生稳定日志、异常、崩溃和间歇故障 |
| M2 Snapshot | Agent C | 插件式只读采集、证据清单、脱敏与限流 | M0、模拟业务 | 每项有结果或缺失原因，不阻塞建单 |
| M2 Codex | Agent D | 会话隔离、只读工具代理、系统沙箱 | Core、Snapshot | 写文件/Git/控制请求均被技术拒绝 |
| M2 飞书 | Agent E | Gateway、话题绑定、Solution、Base 投影 | Core 契约 | 重试幂等，断网后可按 ID/版本补拉 |
| M3 集成 | 主线 + Agent F | systemd、安装包、故障注入、端到端测试 | M1、M2 | P0 验收矩阵全部通过 |

各 Agent 使用独立写入目录；跨模块修改先更新 `contracts/`，避免并行实现产生隐式耦合。

## 5. Snapshot 策略

Snapshot 不要求业务代码提供新接口，也不改变现有日志系统。采集器只读取当前已有信息：

- 系统时间、OS、内核、uptime、CPU、内存、磁盘和网络。
- 业务进程、父子关系、打开文件、监听端口和服务状态。
- 配置仓库的 remote、分支、HEAD、status、diff 摘要和文件校验值。
- 已有文件日志、journald、ROS 默认日志和仍可读取的终端输出来源。
- USB、串口、CAN、相机和网络设备枚举。
- 当前 ROS node/topic/service/action/diagnostics，以及可选预期拓扑的差异。

每项采集都有超时、输出上限、开始/结束时间、退出码、SHA256 和缺失原因。默认不录 rosbag，不发布 Topic，不调用控制 Service，不修改权限或设备状态。

任意终端已经滚走或关闭的历史不能保证恢复；这类缺口明确记录，允许现场在 Chat 粘贴终端文本。

## 6. Codex 只读门禁

1. 工具代理只暴露列目录、搜索代码、读文件和读证据。
2. 业务仓库与 Snapshot 以只读方式提供；输出只允许写入问题专属临时目录。
3. 独立低权限进程隐藏 Git/SSH 凭据、Docker socket、systemd 控制接口和硬件设备。
4. Codex CLI 同时启用 `--sandbox read-only`、`--ephemeral` 和 `-a never`；保留模型供应商认证配置，但禁用插件、Apps、Skills、MCP、多 Agent、浏览器、计算机控制与 Hooks。
5. 每次分析前后校验业务仓库内容；检测变化立即标记安全失败。

## 7. 已完成验证

- Core、Snapshot、Codex、Web UI、Gateway 与部署资产共 43 个测试通过；Gateway 另有 12 个测试通过。
- macOS 模拟业务的 7 类故障场景可运行，独立仓库已推送到 `main`。
- 真实 Codex 已基于 Snapshot 完成一次只读分析；分析前后业务仓库 HEAD 和工作区均未变化。
- 飞书测试群已成功收到 Solution 交互卡片；“问题台账”已按真实字段写入单条 `open` 记录。
- Gateway 已支持工程师 Solution 回传，以及现场确认后将状态从“待验证”更新为“已解决”。

## 8. P0 验收门槛

- 空闲时只有 Core 和轻量浮窗；无 Codex、飞书连接和 Base 轮询。
- 离线可创建 ID/sub-ID、聊天和 Snapshot，重启后记录不丢。
- 不同 ID 的消息、证据和 Codex 会话不串联。
- 历史问题再次上报创建 sub-ID，并使主问题汇总状态重新为 `open`。
- 选择人工后停用该问题本地输入；发送失败与工程师处理中分开显示。
- 工程师提交 Solution 后进入待验证；只有本次上报人能确认已解决。
- 本机离线期间提交的 Solution 能在恢复后按 ID/版本补拉。
- Codex 无论收到什么指令都不能修改业务代码、Git、服务或设备。
- 模拟业务程序的七类场景均能生成有来源、时间和缺失说明的 Snapshot。
- 目标 Linux 设备完成 24 小时待机资源测试后，再确定周期健康检查和预算。
