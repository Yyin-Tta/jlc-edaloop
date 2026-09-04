# P5 目检审计协议(14 金标冻结批)

> 版本:v1(2026-09-01)|负责人:人工目检(Claude 出协议+判读,用户执机)
> 上游:DEVELOPMENT.md §5.4.5 产品梳理批第 3 件;搭载 14c845c 布局治本批真机复验。
> 产物落点:本文件 §7 缺陷台账 + `runs/`(截图/audit 原件,私有仓)。

## 1. 目的

1. **终结 QA 发现倒挂**:目前布局缺陷靠用户肉眼发现、agent 的门禁看不见。本批对 14 金标需求全量目检,把人眼发现的一切缺陷按分类法登记,再逐类回答「能否机械化」——能的回填 checks.py/controller 判据,不能的写明为何留给人工。
2. **冻结金标基线**:req-01~14 从此为冻结集,验收口径 = 本批登记的缺陷清零或降级;改需求集须独立 PR。
3. **搭载验证 14c845c**(扩档 270/330、盲退几何关 `_guarded_autoconnect`、末轮 reseat 后终态复探 post-reseat3):看 audit 事件统计,不单独跑。

## 2. 冻结范围与环境

| 项 | 值 |
|---|---|
| 需求集 | `evals/requirements/req-01` ~ `req-14`(14 条,含 .bak 的 req-24 不算) |
| 代码 | main @ **1fe5f00**(v0.6.17,362 测绿;d040c49 C9 双治 + req-08 复跑两修——标记侧位引脚聚类+place-only 入 pack,详 §7 复跑判读) |
| easyeda-agent | **1.2.10**(adapter 启动时强校验,不符拒跑) |
| EasyEDA Pro | 前台运行,工程路由 `EDALOOP_PROJECT` 同跑批惯例 |
| 模式 | `EDALOOP_LAYOUT_FREEZE=pack`(单遍冻结:试放→量测→装箱→重放+closeout→画框→net 修复→冻结,**不跑 gate 迭代**) |

冻结模式选择理由:目检对象是**布局终态**,gate 迭代只会引入轮次噪声;freeze=pack 与生产共享同一 closeout 序(rotate→reseat→closeout→compact→reprobe→reseat2→wrong_side→reprobe→**reseat3**),14c845c 的全部新代码都在路径上。

## 3. 跑批命令序

按块数从小到大(小需求先热身、确认协议可执行,再上大页数需求):

```powershell
# 建议序:req-08(最小)→req-14 →req-03 →req-04 →req-05 →req-09 →req-06 →req-11
#        →req-02 →req-13 →req-12 →req-01 →req-10 →req-07(最大,37 块)
$env:EDALOOP_LAYOUT_FREEZE="pack"
uv run edaloop run evals/requirements/req-08-liion-protection-freeform.md 2>&1 | Tee-Object log-audit-req08.log
# 逐条替换需求文件名;单需求 5~15min(37 块的 req-07 约 13min+)
```

- 每条跑完**当场目检再跑下一条**(冻结页会被下一轮清页,不攒批)。
- 中断续跑:直接重跑同命令,验证式清页幂等;audit 落 `runs/run-<id>/audit.jsonl`,新 run 新 id,不覆盖。
- 目检时窗口布局建议:每页放大到能读 netport 文字为准,逐页过 §4 清单。

## 4. 逐页目检清单(每页过一遍)

| # | 看什么 | 判劣标准 |
|---|---|---|
| C1 | 器件本体相交 | 任意两器件 body 框有重叠面积(擦边不算) |
| C2 | 翼擦 | A 块的 netport 文字/netflag/桩线墨迹压到或擦到 B 块**本体或 B 的墨迹**,但本体不相交 |
| C3 | 标记压体 | netport/netflag 文字压在任意器件本体上 |
| C4 | 框不住墨迹 | 虚线体积框外还有本块墨迹(连线/文字);框交叠(跨块线)如实呈现,不算缺陷 |
| C5 | 出带 | 任何墨迹越出 BAND(30,30,1140,795)±5 |
| C6 | 图签遮挡 | 墨迹压图签区(486,30,1140,180);oversize 独占页的翼展压角**如实保留**,登记但不判劣 |
| C7 | 页残骸 | 非本轮内容的残留器件/标注/旧框 |
| C8 | 连线并轨 | 直连线中途挂到别的网(看 netport 网名与所连引脚是否一致) |
| C9 | 引脚同点 | 两个引脚落在同一坐标 |
| C10 | 空白率 | 单页墨迹占比 < 20% 且非末页(装箱浪费) |
| C11 | 文字可读 | designator/netport 文字互相重叠到不可读 |
| C12 | 页数合理 | 总页数明显多于块数所需(经验:37 块 ≤ 7 页) |

## 5. 缺陷分类法与机械化映射

登记格式见 §6。每类缺陷回答:现有判据在哪、缺什么、能否机械化、回填落点。

| 类别 | 对应清单 | 现有机械化 | 缺口 → 候选判据 | 落点 |
|---|---|---|---|---|
| 本体相交 | C1 | `sch clusters --strict` 逐对复核(controller closeout 硬门禁) | 应为 0;出现即 gate 漏报 → P0 | controller(已有,复核口径) |
| 翼擦 | C2 | 无(软指标,§10 定为记数不追修) | ink-rect vs foreign body ±2 相交计数,入报告不阻断 | controller `_layout_warnings` 新 code `WING_GRAZE` |
| 标记压体 | C3 | `_connect_stub` 落点避让(事前);无事后复核 | 复用 blind-guard 的 mrect vs body 判定做事后扫描 | controller `MARKER_ON_BODY` |
| 框不住墨迹 | C4 | 框口径=volume∪自画线(已有) | 框外墨迹 bbox 检测需读文字实体,成本高 → **留人工** | —(登记即可) |
| 出带 | C5 | blind-guard 的 BAND±5(仅盲退标记);重放锚带内 | 全页墨迹 bbox vs BAND 事后扫描 | controller `INK_OUT_OF_BAND` |
| 图签遮挡 | C6 | 图签让位默认(装箱预留) | 同 C5,对图签区矩形单独扫 | controller `TITLEBLOCK_OCCLUDE`(仅非 oversize 页) |
| 页残骸 | C7 | 验证式清页(两趟告警 `PAGE_CLEAR_FAILED`) | 已覆盖,目检只验证告警真实性 | 已有 |
| 连线并轨 | C8 | net-presence 终检+修复(`_net_presence`/`_repair_missing_nets`);引脚同点/错网硬门禁 | 错网已拦;并轨**形态**检测(线中段挂网)难 → 留人工+audit 对照 | —(登记即可) |
| 引脚同点 | C9 | `_fix_marker_coincidences`+硬门禁 | 应为 0;出现即漏报 → P0 | controller(已有);**同 pin 同网重复载体已补双治(d040c49):盲退幂等清理+`_dedupe_pin_markers` 终态去重** |
| 空白率 | C10 | packer waste(装箱口径,非墨迹口径) | 每页墨迹 bbox 并集/页面积,阈值 20% | controller `PAGE_INK_SPARSE`(弱告警) |
| 文字可读 | C11 | 无 | 文字实体级重叠检测成本高 → **留人工** | —(登记即可) |
| 页数合理 | C12 | packer 页数(waste 驱动) | 已有数据,目检只确认 | 已有 |

映射结论先行:**C4/C8/C11 三类本批留人工**(文字/线段实体级检测的工程成本与收益不匹配);其余类别的候选判据在下一批(p5-2)按台账频次排序回填——出现 ≥3 次的类别才立项,偶发的不做。

## 6. 缺陷台账格式

登记到本文件 §7,一行一缺陷:

```
| 需求 | 页 | 清单项 | 描述 | 截图 | audit 对照 | 处置 |
| req-08 | P2 | C2 | U3 的 NETX netport 压到 R2 本体上沿 | runs/audit-p5/req08-p2.png | run-<id>:reseat-escape U3:1 fallback | 回填 MARKER_ON_BODY / 已修 / 留人工 |
```

- 截图统一放 `runs/audit-p5/`(私有仓),命名 `req<NN>-<页>-<序>.png`。
- 「audit 对照」必填:找到 audit.jsonl 里最接近该缺陷的事件(reseat fallback/unguarded/warning 等),没有就写「无对应事件」——**无对应事件 = 门禁盲区,是本批最有价值的产出**。

## 7. 缺陷台账(跑批回填)

登记方式:目检者只记 需求/页/清单项/描述(截图可选);audit 对照与判读由 Claude 补。

| 需求 | 页 | 项 | 描述 | audit 对照(run-746b24879342) | 判读/处置 |
|---|---|---|---|---|---|
| req-08 | P1 | C3 | DW01A 2脚连线压在本体上 | PROTDW01:3 OC `reseat-blind-guard→unguarded`;PROTDW01:2 CSI 两轮 reseat(90,300→190,300 right/60) | 盲退标记落在自体边缘,桩线穿体;候选弱判据 `MARKER_OWN_BODY` |
| req-08 | P1 | C10 | 整页只放了一个模块 | `repack-fallback reason=no-upstream-blocks` → req-08 走 freeform 路径,分页非 packer 所为 | freeform 分页浪费;记频次,p5-2 视次数立项 |
| req-08 | P2 | C3 | FS8205A 2脚连线和网格压在本体 | PROTFS:2 `reseat-blind-guard→unguarded` ×2 | 同 P1 C3,同根 |
| req-08 | P2 | C9 | **FET_MID 两个都挂在 FS8205A 5脚** | `freeze-pack-reseat`:pin2 两枚(110,300+160,300)、pin5 两枚(250,300+260,300),全 `failed:fallback`;而 `round-validate gate=pass` | **P0:同 pin 同网重复载体**。net-presence 只查「网存在」不查「重复」,盲区实锤。**已修(d040c49)**:盲退链三处幂等清理+`_dedupe_pin_markers` 终态去重(freeze 画框前+生产尾轮 reseat 后,审计 marker-dedupe);**req-08 复跑验收时重点核对 P2 标记数与 marker-dedupe 事件** |
| req-08 | P2 | C10 | 大片空白 | 同 P1 C10 | 同上 |

req-08 台账小结(2026-09-01):5 缺陷=2×C3(盲退标记自体压)+2×C10(freeform 分页)+1×C9(**P0 重复标记,门禁盲区**);C9 触发 §10 No-Go 条款 → 修复批先行,**已落地 d040c49(2026-09-01)**,req-08 复跑闭环后继续 14 金标序(搭载 d040c49)。

**req-08 复跑判读(2026-09-02,run-e89808c395b8,d040c49 上)**:

| 需求 | 页 | 项 | 描述 | audit 对照(run-e89808c395b8) | 判读/处置 |
|---|---|---|---|---|---|
| req-08 | P2 | C9 复验 | ✅ **关闭**:FET_MID 每 pin 单枚、side 正确 | 幂等清理+`marker-dedupe` 生效 | d040c49 验证通过;复跑验收续核 |
| req-08 | P1 | C3′ | DW01A 左脚网标记仍放对侧/本体:PROTDW01:2 CSI 终态锚 (220,300) 在右侧、:5 VDD_S 锚 (175,300) 压本体(用户规范「引脚在哪侧,网标记就放哪侧」三轮未愈) | `freeze-pack-reseat` #43/#54/#56 PROTDW01:2/:5 反复 `failed:fallback`,dir left/30→auto/0→right/30 振荡 | **慢性病定性,根因链四层**(引线型 bbox 含引线贴边判定失效/avoid 含自件框同侧候选全灭/判据②收走救援落位/扫尾中心分半漏「脚与中心之间」锚)→ **已修(1fe5f00)**:`_pin_side` 脚端点聚类+同侧救援档+own-body 豁免+扫尾引脚相对口径 |
| req-08 | P1-P3 | C10′ | 仍 3 页且大面积空白(P1=prot_dw01、P2=prot_fs8205+c_vdd100n+r_vdd100、P3=r_csi1k,1 页足放) | `repack-fallback reason=no-upstream-blocks`(freeform 计划无 block-apply 被装箱门槛整拦,退 compile 流式初值) | **已修(1fe5f00)**:place-only 计划入 pack(两通道都空才回退)+freeform decompose 补 module 亲和同页 |

**req-08 三跑验收重点(搭载 1fe5f00)**:①页数=1 且无 `repack-fallback`;②DW01A 左列脚(2/5 等)网标记全在引脚左侧、不压本体;③`mark-side-guard` 事件 `fixed` 为空(= 终态无违例,有 fixed 也行但要逐条核对落位);④C9 保持关闭(P2 每 pin 单枚)。

| req-14 | P1 | C3 | DW01A 2脚连线压在本体上 | (旧代码 14c845c 上的观察,audit 对照待补;1fe5f00 重跑后再判读——同属 C3′ 侧位族) | 待 req-14 复跑 |


## 8. 14c845c 布局批真机验证(搭载)

跑完 14 条后统计(不单跑):

```powershell
# 盲退几何关触发率:reguard=改为几何关落点,unguarded=关也救不回(如实盲退)
Get-Content runs\run-*\audit.jsonl | Select-String '"reseat-blind-guard"|"merge-blind-guard"'
# 扩档命中:连接桩 offsets 270/330 是否被选用(看 connect 桩审计事件)
Get-Content runs\run-*\audit.jsonl | Select-String 'reseat=.*/(270|330)'
```

判读:reguard 占盲退总数应显著过半(几何关在救);unguarded>0 的位置逐个到图上核对「宁翼擦不隐短」是否成立(标记虽丑但网不并)。

## 9. 交付抽检(3 条全跑 + Web UI dogfood)

freeze=pack 不含 zones/titleblock/gate,另抽 3 条走完整交付路径,顺带 dogfood Web UI:

1. `req-02`(rs485,曾出并轨回归)走 CLI 全跑:`Remove-Item Env:EDALOOP_LAYOUT_FREEZE; uv run edaloop run evals/requirements/req-02-...md`,目检 titleblock/图框/gate 报告;
2. `req-14`(door-sensor,e2e 金标)走 Web UI:`uv run --extra ui edaloop ui`,浏览器里跑同需求,记录 UI 侧进度流/断线恢复的体验缺陷(进台账,类别记 `UI`);
3. `req-07`(motor,37 块最重)走 CLI 全跑,与 8-31 的 13min/7 页基线对比页数与耗时。

## 10. Go/No-Go 与产出

- **Go**:C1/C9(硬门禁项)14 条全零;新增候选判据类别(翼擦/压体/出带/图签)完成频次排序;3 条全跑交付无 P0。
- **No-Go → 修复批**:任何 C1/C9 出现 = gate 漏报,先修门禁再谈回填。
- 产出:§7 台账 + §8 判读 + p5-2 回填立项清单(按频次)+ DEVELOPMENT.md §13 行。
