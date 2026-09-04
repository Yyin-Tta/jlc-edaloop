# Issue: 连接器假死后 ≥5 分钟不自愈 —— 负载下 socket 关闭后 register() 疑似被静默忽略(soak 2026-08-04 的 wedge 形态真机再现)+ 请求 daemon 侧可见性

> 状态:**已提交 2026-08-24 —— [zhoushoujianwork/easyeda-agent#185](https://github.com/zhoushoujianwork/easyeda-agent/issues/185)**(正文与本文一致,少了内部状态行)

## 环境

- easyeda-agent CLI/daemon **v1.1.1**(Windows 10,`connectorVersionOk: true`)
- EasyEDA Pro **3.2.175**,连接器 1.1.1
- 场景:长时间自动化回归(单窗口、单工程、单 daemon;死亡窗口存活 84 分钟、服务 16196 个动作)

## 现象

回归进行中连接器死亡:所有请求 `NO_CONNECTOR`,**5 分钟内零再注册**,transport 的无限重连(0.5s→8s 退避 + 每 4 次失败换 wsId)全部无效。唯一恢复手段 = 杀掉 EasyEDA Pro 进程重启(重启后新窗口立即注册成功 —— App 活着,是复活路径死了)。

## 证据时间线(daemon 侧审计 `~/.easyeda-agent/audit/2026-08-24.jsonl` 还原)

| 时刻(UTC) | 事件 | 证据 |
|---|---|---|
| 06:09:27 | 窗口 `e23df8a0…` 注册 | windowId 生命周期首条 |
| 06:09→07:32 | 正常服务,**16196 个动作** | 同 id 审计行 |
| 07:25:42→07:26:00 | **第一次卡顿**:`schematic.pages.list` `DISPATCH_FAILED` 18s("connector did not respond" / "context deadline exceeded") | `dur=18000ms` 行 |
| ~07:31 | **自愈**,同 windowId 恢复(`document.current` 3ms) | 同 id 后续 ok 行 |
| 07:32:10 | 最后健康动作 | 同 id 最后一组 ok |
| 07:32:10→07:32:28 | socket 消失,hub 清空 | 下一条请求报 `no window is connected at all` |
| 07:32:28→07:37:34 | **零再注册**(期间 CLI 侧 `document.current`/`library.search`/`pages.list` 反复 NO_CONNECTOR,全部落空) | 审计全为无 windowId 拒绝行 |
| 07:37:34 | 杀 App 重启后,新窗口立即注册(停 home 标签) | 新 windowId 生命周期首条 |

审计节选(已脱敏;失败行 `durationMs=0` 即路由层快拒):

```json
{"ts":"07:32:10.74","windowId":"e23df8a0-…","action":"document.current","ok":true,"durationMs":3}
{"ts":"07:32:28.84","windowId":"e23df8a0-…","action":"document.current","ok":false,"durationMs":0,
 "errorCode":"NO_CONNECTOR","errorDetail":"no connector registered for window \"e23df8a0-…\", and no window is connected at all"}
```

## 分析(与你们自己的代码注释逐条对上)

1. **触发器 = webview 主线程饿死**。重画布/保存风暴冻结 JS 事件循环 —— 即 `docs/optimization-loop.md` A4("后台/被遮挡窗口重画布计算永不完成,客户端重试会在 webview 堆积任务恶化")与 `action-queue.ts` 头注("一次卡死的重调用让接下来 4.5 分钟的 place/delete/document.open 全部静默消失")记录的形态。我们 07:25:42 的 18s DISPATCH_FAILED + 同窗口自愈,是"先卡顿、后死亡"两段式的直接实证。
2. **死亡 = socket 被关**。心跳 ping 在主线程发(Worker 只发 tick,`eda.*` 全部主线程),饿死即停跳;`transport.ts` 注释明载 EasyEDA `sys_WebSocket` **~5s 空闲即关**,叠加 `MAX_MISSED_PONGS=3` 自判失联 —— 两条路都通向 daemon 读循环摘窗(`connect.go` read error → `hub.remove`)。
3. **不复活 = 已知 wedge,但触发向量是新的**。`transport.ts` 的 WS_ID_ROTATE 注释记录了 2026-08-04 的 soak:**停 daemon 45s/60s 两轮,第二轮 210s 没能自愈**,60832 上持续报 "closed before the connection is established",`register()` 在 id 被判 active 后被静默忽略;换 id 是逃生口但 soak 证明不可靠。本例是**第三个独立实例、持续最久(5 分钟)**,且触发向量不同:**daemon 全程活着**,socket 死于负载下饿死 —— 即不存在"daemon 不在"窗口,复活失败发生在对活 daemon 的重连风暴里。这可能把排查面从"id 状态机 vs daemon 重启时序"收窄到 **EasyEDA 侧 socket 槽位/id 表在异常关闭后的状态残留**。
4. **观测黑洞是主要障碍**:wedge 期间 daemon 侧**看不到任何东西** —— register 被忽略时 daemon 收不到连接,`diag()` 走的 `sys_Log` 在编辑器日志面板里,窗口不动就没人读得到;daemon 审计只记动作,不记 register/unregister/重连尝试(本时间线是从 windowId 生命周期反推的)。另外心跳是**单向**的(连接器→daemon,daemon 从不主动 ping),webview"冻着但 socket 未断"与"彻底死了"在 daemon 侧不可区分。

## 请求

1. **复活路径**(核心):针对"异常关闭后 register 被静默忽略"再攻一轮 —— 本例提供了新复现向量(**负载饿死 + 后台窗口 + daemon 全程在场**,比停 daemon 更贴近真实事故);若能给出 wedge 后从编辑器日志面板取证 `sys_Log` 的指引,我们可以在下次复现时第一时间取回连接器侧重连日志。
2. **daemon 侧可见性**:
   - 审计里记连接器生命周期事件(register / unregister(read error 原因) / retire),让"死没死、死因、有没有试图回来"不再需要反推;
   - daemon 对静默窗口主动 ping(连接器已会回 pong),把"冻着"与"走了"区分开,`health` 里可标注 `silent-since`。
3. (关联,另有独立草稿)CLI 无 `project.open`:App 重启后云端工程不自动恢复,自动化恢复链的最后一公里只能人工 —— 我们的外置看门狗重启 App 后只能弹 toast 求人工开工程。

## 我们目前的兜底(供参考)

外置看门狗:真实探针(`sch pages --project` 往返 + 前台 documentType 校验)→ 轻恢复(`doc open` 重绑前台)→ 重恢复(优雅关 App → 重启 → 180s 窗口内每 10s 重探 + toast 求人工)。今天整条链正是靠它拉回来的 —— 但"杀 App"是当前唯一有效杠杆,代价大且依赖人工收尾,这也是本 issue 想推动解决的。
