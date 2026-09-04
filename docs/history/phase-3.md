# [归档] Phase 3 全链路·规划与执行记录(2026-08-21,v0.5.2 关闭)

> 2026-09-01 自 DEVELOPMENT.md 原样迁出(零改写;文中 §N/表号/行内引用指迁出时主文档节号)。
> 主文档对应位置只留收官快照;本文件为该阶段执行细节的完整记录。

### Phase 3(全链路:脑洞 → 下单;2026-08-20 规划,ADR-0009)

**范围决议**:v2 五段闭环已交付(22/22=100%),启动面向终极愿景 v3 的扩展——jlc-edaloop 从「原理图闭环」升级为「全链编排者」:PCB 与下单不自研,分别通过消费 easyeda-agent pcb 命令面与 JLC 下单/商务 API 实现(原则 6:薄而规矩的公民)。下单永远止步于「报价单+订单草稿」,支付是人工硬门禁。

#### 3.0 全链路差距审计(2026-08-20,基线 v0.3.2 / 78 tests / 95 块)

| # | 终极链路环节 | 现状 | 缺口判定 | 量级 |
|---|---|---|---|---|
| G1 | 脑洞/需求输入 | M1 单轮 LLM 解析→DesignIR;`questions` 命令可列 open_questions | 解析可用;但 questions 是**旁路命令**,答案文件未回灌 run 主链路 | 小 |
| G2 | AI 拆解与细化 | DesignIR functions/interfaces/power 分解 | 无多轮细化闭环:歧义需求要么 HALT 要么 uncovered,缺「提问→答复→重规划」 | 中 |
| G3 | 知识底座(datasheet/已验证工程/开源) | M2 95 块全回读验证+案例回写;M6 ingest(7 份 PDF 泛化,2 pass/2 降级/3 边界) | 数据债:块库 parts[].lcsc 缺 62 处(多器件块);oshwlab 复刻 4 批路线图仅完成第 1 批;ingest 规则通道对无标准 pin 页 PDF 覆盖有限 | 中(数据) |
| G4 | 原理图生成 | M3 两段式+place 通道+自由拓扑子集;22/22=100%,中位 1 轮 | **已达标**;长尾器件靠块库持续扩容(运营性工作) | — |
| G5 | 器件选型 | 选型=块检索(块即 C 号);BOM 交付带逐件计价 | 无**替代料推荐**(等价类只出提示文本,不出可确认的 swap 方案);无 SMT 可制造性偏好(基库/扩展库、库存/MOQ 影响贴片费) | 中 |
| G6 | 参数设计 | P2-B 三类规则(LED 限流/分压/电源电容,E24 归一) | 覆盖面小:BUCK 纹波全公式、电感/续流管选型、反馈网络、TVS/保险丝规格、热校核均未做 | 中 |
| G7 | 成本优化 | P2-C 被动通道:cost_target 存在时查价+提示,交付 BOM 汇总 | 无主动优化:不会自动提出「换等价块降本 N%」方案并走确认门禁 | 小 |
| G8 | 智能评审 | M4 强/弱门禁+M5 归因迭代(设计正确性已机械保障) | 无 **critic agent**:机械门禁只证「画对了」,不审「设计好不好」(去耦完备性/保护缺失/热裕度等设计常识层) | 中 |
| G9 | 迭代改进 | M5:pass@5=100%,中位 1 轮,防震荡+HALT 升级 | **已达标** | — |
| G10 | PCB 自动布局布线 | §3 原非目标(交棒);上游 v0.25.1 已具备全量能力:auto-place/outline-fit/round/route-short/route-critical/power-pour/power-planes/silk-align/beautify/drc/check/layout-lint--gate/workflow 阶段门 | **全缺**:无 sch→PCB 交棒编排、无 PCB 门禁转译进 M4、无 PCB 迭代环 | **大** |
| G11 | 自动下单(PCB+SMT+元件) | 无任何模块;上游有参考:JLC SMT `selectSmtComponentList` API(base/expand/库存/阶梯价)+ bom-enrich 脚本;EasyEDA 可导出 gerber/BOM/POS | **全缺**:制造文件导出、SMT 兼容预检、三段报价(PCB 制板+SMT 贴片+元件)、订单通道 | **大** |

**结论**:原理图核心(G4/G9)已闭环;缺口集中在两端——前端的**需求细化闭环**(G1/G2)与后端的**PCB 编排+下单通道**(G10/G11,量级最大);中段增强(G5-G8)为质量与价值项。Phase 3 按「先收口前段 → 再攻后段大缺口 → 中段增强穿插」排序。

#### 3.1 Phase 3 里程碑(每批独立 Go/No-Go,防单人带宽风险 R9)

| 批次 | 内容 | 交付物 | Go 指标 |
|---|---|---|---|
| P3-0 数据债与决议(~1 周) | ADR-0009 转正(本文档即决议);块库 parts[].lcsc 62 处回填(多器件块逐 C 号录入);`run --answers` 把 questions 答案文件回灌主链路(答过的 open_questions 不再出题) | 交付 BOM 100% 带 C 号;歧义需求可带答案一次跑通 | 22 需求回归不回退;抽样 3 例带答案 run 全 PASS |
| P3-1 需求细化闭环(~1 周) | `edaloop refine` 子命令:run 出现 open_questions/uncovered 时暂停落图,生成问题清单→用户答复→IR 增量更新→重 plan(复用 M3,不改落图通道);uncovered 功能自动二次检索(换查询词重试一轮) | refine 命令+IR 版本化(IR-v1→IR-v2 审计留痕) | 3 例歧义需求经 refine 从 HALT/uncovered 变 PASS |
| P3-2 选型升级(1-2 周) | 替代料推荐:等价类从「提示文本」升级为 swap 提案(块 A↔块 B,价格/库存/SMT 库类型三维对比),弱门禁确认后重 plan;SMT 可制造性标注进 delivery.bom.json(base/expand/MOQ,走 JLC `selectSmtComponentList`,对齐 ADR-0004「CLI 查 ground truth、web API 查商务数据」分工) | swap 提案确认流+BOM SMT 标注 | 带 cost_target 需求产出可确认降本方案(实测 ≥1 例降本 ≥5%);BOM 全件带 SMT 库类型 |
| P3-3 sizing 扩容(1-2 周) | 新增四类:BUCK 纹波全公式(ΔI=Vo(1-D)/(L·f) 电感选型+输出电容纹波)、反馈网络分压比、TVS/保险丝规格(电压/电流定额)、热校核(功耗→温升提示);M6 sizing 建议与规则引擎参数联动(建议值喂公式输入) | sizing 规则库 v2+delivery.sizing.txt 扩展 | 电源类块(ldo/buck/boost)参数建议覆盖 ≥80%;公式代入过程可追溯 |
| P3-4 critic 评审 agent(~1 周) | 独立 LLM 复核器审 BlockPlan+交付物:设计常识维度(去耦完备/上拉下拉/接口保护/热/EMC 常识),产出结构化 findings(复用 Finding schema,severity=warn)进 M5 反馈与 questions 队列;**弱门禁**,不阻断交付(原则 2) | critic.py+评审报告入 run 目录 | 注入 5 个已知设计缺陷的 plan,捕获 ≥4;金标准 22 需求零误伤(不把正确设计标为缺陷) |
| P3-5 PCB 编排 M8(3-4 周) | ①交棒:原理图 PASS 后 sch→PCB(EasyEDA 转 PCB/网表导入,`pcb add-component` 通道备选);②编排:pcb auto-place→outline-fit/round→route-critical→route-short(档位策略照抄上游:稀疏自动/稠密人机协作)→power-pour;③门禁:pcb drc+check+layout-lint--gate 三门禁转译为 Finding 进 M4;④PCB 迭代环:复用 M5 归因模式(布局类 finding→RELAYOUT 反馈);⑤交付:PCB 源文件+截图+DRC 报告入 run 目录 | `edaloop pcb` 子命令组+M4 PCB 门禁通道 | 2 个已交付原理图工程转 PCB 全门禁绿(drc 0 违规+check 0 ERROR+layout-lint 过),零手工修板 |
| P3-6 下单通道 M9(2-3 周) | ①制造文件导出:gerber+BOM(带 C 号/库类型)+POS(坐标);②SMT 兼容预检:全件基库/库存≥qty/MOQ 达标,不达标给替代料建议(复用 P3-2);③报价单:PCB 制板(层数/尺寸/数量)+SMT 贴片(基库免上料费/扩展库费)+元件三段合计;④**人工确认硬门禁**:`edaloop order --confirm` 显式确认后才生成订单草稿(半自动:跳转待支付页面;自动支付永不做) | `edaloop quote/order` 子命令+报价单交付物 | 1 单端到端走到「待支付」(含 SMT 预检拦截 1 例缺货并给出替代);支付由人完成 |

**端到端验收(Phase 3 收口)**:1 个新脑洞需求,从需求文档输入到「PCB+SMT+元件待支付订单」全程零手工编辑(细化问答与下单确认两类人工交互除外),全链审计日志完整,案例回写知识库。

#### 3.2 Phase 3 执行结果(2026-08-21,六批全过 + 端到端验收通过)

| 批次 | 状态 | 实际交付(vs 计划) | Go 验证 |
|---|---|---|---|
| P3-0 数据债 | ✅ | parts lcsc 62 处计划→**89/90=98.9%**(upstream 块主料回填 17);`run --answers`+IR 版本化(revision/decisions) | 3 例带答案 run 全 PASS(req-02 1r/req-03 1r/req-10 2r) |
| P3-1 refine | ✅ | `edaloop refine`(--list 问题清单/--answers 应用)+uncovered 二次检索;**decisions 双重注入**(检索 query+planner prompt,决策落块) | req-26 强歧义:refine 后 plan 4→6 块(双电源/485/NTC 全落) |
| P3-2 选型 | ✅(降级项) | swap 提案(三维 ≥5% 才提)+SMT 标注;**JLC selectSmtComponentList 域名不可达(R13 实锤)→wmsc componentLibraryType+号段启发近似** | 单测 6 例;降级路径诚实标注 unknown |
| P3-3 sizing | ✅ | BUCK 纹波全公式/TVS(VRWM/VC)/保险丝(1.25 降额)/热校核(Tj 裕量<20°C 警示) | 单测 10 例;req-27 交付 4 条(含宽压输入 TVS+保险丝) |
| P3-4 critic | ✅ | 六维度 LLM 评审(去耦/上拉/接口保护/电源完整性/热/EMC)→delivery.review.txt;宁缺毋滥 | 单测 5 例;req-27 零误报 |
| P3-5 PCB M8 | ✅ | 18 步 pipeline(place-constrained/tier 梯/set-assembly/lint-gate/confirm 链/route/pour/silk/mount-holes)+**通用布局修复循环**(tight/boxed/overlap 定向挪)+degraded 判定通道(R14) | 真机:①2 器件小板 17/18 步 rc=0 三门禁绿;②req-27 42 器件板:C9/R3 短路清零,gate_ok=True |
| P3-6 下单 M9 | ✅(按 R13 兜底) | 预检(MOQ/库存/no-lcsc+修复建议)/三段报价/`order --confirm` 硬门禁(无 flag 拒执行,永不支付);gerber 走客户端下单流程+人工指引(R13:上游无 gerber CLI 导出) | 真机报价 ¥54.75@5pcs,预检拦 4 项 MOQ |

**端到端验收(2026-08-21,req-27 智能门磁)**:✅ **通过**——脑洞需求文档 → 原理图(1 轮 PASS,7 块,交付 6 件:BOM ¥2.61/sizing 4/swap/critic)→ PCB(42 器件 2 层,三门禁绿,pcb-report.md)→ 报价(¥54.75)+ 订单草稿(--confirm)——**零手工编辑工程**。全程审计留痕;详见 `docs/e2e-acceptance-p3.md`(本地)。回归:smoke 3/3+daily 8/8+增量层全 PASS 零失败。

**计划偏差与经验**(对照 3.1 原计划):
1. P3-2 SMT 库类型:计划的 JLC API 通道不可达 → 按 R13 预案降级 wmsc 近似,不阻塞交付;
2. P3-5 档位策略:上游实为 **stage 状态机**(tier 梯子/assembly profile/确认指纹/mutating 失效),非简单命令串——pipeline 按真机行为重排(tier1 --empty→set-assembly→tier2/3 --empty→lint-gate→tier4 实认领→confirm-layout/outline);
3. P3-5 布局修复:原计划"布局类 finding→RELAYOUT 反馈"演化为 **lint 输出正则→定向挪件循环**(42 器件板实测:overlap 短路两轮清零);修复后不可盲目 outline-fit(会把挪开的空间收回);
4. P3-6 制造文件:上游无 gerber CLI 导出 → 制造文件包=pcb dump+快照+人工下单指引(订单草稿含步骤清单),符合"薄而规矩公民"定位;
5. 遗留:块库缺带 lcsc 的 LED/排针/阻容件(req-27 三项 uncovered 之源);上游 `pcb arrange` stale 读 0 组件 bug(候选 issue);42 器件级 boxed-in 布局质量=Phase 4 评分驱动布局方向。

**依赖与顺序纪律**:P3-0→P3-1 串行(前者是后者的数据/接口前提);P3-2/P3-3/P3-4 为中段增强,单人开发按编号串行、可按需求热度插队;P3-5 依赖上游 v0.25.1 pcb 面(升级版本=独立 PR+全量回归,纪律不变);P3-6 依赖 P3-5 交付物。每批结束更新本文档+变更记录。

#### 3.3 Phase 3 新增风险(已并入 §11 登记)

- **R12 下单资金安全**:误下单/错规格下单=直接经济损失。缓解:默认止步报价单;`--confirm` 显式双确认;预检报告随报价存档。兜底:第一期只做「报价+跳转」,订单草稿生成也放人工触发。
- **R13 JLC 下单 API 无公开契约**:接口靠抓包/网页逆向,随时变更。缓解:通道隔离在 M9 内部 provider 抽象(对齐 llm/embedding 纪律);坏响应优雅降级为「导出制造文件+人工下单指引」。
- **R14 上游 PCB 能力边界**:稠密板 Freerouting 布通率/人机协作档打断自动化。缓解:档位策略照抄上游设计(稀疏才全自动);PCB 迭代环上限轮数沿用 M5;不达标诚实降级为「PCB 半成品交付+人工修板指引」。
- **R15 范围蔓延**:全链愿景 vs 单人带宽。缓解:Phase 3 七批各自 Go/No-Go,任一批连续两周不达标记挂起,优先保 P3-5/P3-6 主干。
