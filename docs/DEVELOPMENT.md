# jlc-edaloop 开发总纲(Development Bible)

> **文档性质**:本项目唯一开发依据(living document)。所有架构决策、进度、风险、经验教训**必须回写到本文档**。
> **维护纪律**:① 任何影响架构/接口/里程碑的决策,先改本文档再写代码;② 每次实质更新在文末《变更记录》追加一行;③ 与三份调研文档冲突时,以本文档为准。
>
> | 项 | 值 |
> |---|---|
> | 版本 | v0.6.18(2026-09-02 终态证据链收口:LayoutSnapshot fail-closed/designator 隔离/低层 apply 降级;包/CLI 版本 v0.7.0,详 §13 v0.6.18) |
> | 日期 | 2026-09-02 |
> | 状态 | **Phase 5(v1.0 打磨)进行中,墙钟 2026-09-15(§10)**:v0.6.18 代码证据链已收口(476 测试绿;包/CLI v0.7.0);`edaloop apply` 已明确为低层实验入口,工程 PASS 仅由 `edaloop run` 严格终态路径产生。**当前断点=L0 真机取证**:工程 `edaloop` 保持只读且 layout FAIL(本体重叠/marker-overlap/孤儿桩/DRC warning),下一步在全新工程复跑 req-08、req-07 和一个 block-only 需求并保存 snapshot、audit、截图、网表 hash;硬指标连续 3 次通过前冻结 v3 PCB/下单与自动案例回写。最近尝试 `runs/run-fb97781513ec` 仅有中途 audit(末事件 `mark-side-guard`),无 `loop-result.json`/delivery,标记为未完成取证,不得计入 PASS。
> | 上游调研 | `research-vision-v2-feasibility.md`(技术) · `research-eda-agent-industry-landscape.md`(产业) · `research-datasheet-extraction-feasibility.md`(datasheet 管道) |

---

## 1. 项目定位

**一句话**:面向嘉立创 EDA 专业版的开源智能原理图设计 agent——解析用户意图,RAG 检索知识库(datasheet/已验证案例/器件库),LLM 生成原理图,机械校验门禁把关,不满足则迭代重生成,直至可交付。

**愿景链路(v3 北极星,ADR-0009)**:

```
脑洞/需求描述 → AI 拆解与细化(多轮问答收敛歧义)
  → 原理图生成(器件选型 + 参数设计 + 成本优化,基于 datasheet/已验证工程/开源块库)
  → 智能评审(机械门禁 + critic 复核)→ 不满足则归因迭代,满足则继续
  → PCB 自动布局布线(M8 编排 easyeda-agent pcb 通道)
  → 自动下单(M9:报价单 → 人工确认硬门禁 → PCB+SMT+元器件订单)
```

v2 五段闭环(原理图核心)是 v3 的已交付子集:

```
解析用户意图(需求文档/BOM/datasheet/对话)
  → RAG 检索(结构化 datasheet 库 + 已验证案例库 + LCSC 器件库)
  → LLM 生成原理图(两段式:块选择 → 块连接)
  → 结果校验(强门禁:连通性/DRC/布局;弱门禁:仿真/人工确认)
  → 满足 → 交付(BOM 带 C 号/网表/审计日志)+ 案例回写知识库
  → 不满足 → 归因反馈 → 迭代(上限 N 轮,防震荡)
```

**差异化四支柱**(所有设计决策的锚点):
1. **开源 + 本地部署**(企业数据不出域,对齐军工/研究所/央国企采购画像);
2. **嘉立创/LCSC 供应链原生**(C 号/BOM 直接可下单);
3. **可机械复验的校验闭环**(loop 是核心卖点,区别于 easyeda-agent 的无 RAG、easyeda-copilot 的无门禁、Flux.ai 的闭源);
4. **案例自进化**(每次通过门禁的交付回写知识库,系统越用越强)。

## 2. 命名

- **项目名(已定)**:`jlc-edaloop`(中文:嘉立创自绘/EDA 闭环代理)。`loop` 对应校验-迭代闭环这一核心差异。
- **GitHub**:https://github.com/Yyin-Tta/jlc-edaloop.git(本地目录 `D:\gyt-pro\jlc-edaloop`,remote `origin`)
- **命名纪律**(学 easyeda-agent 四件套同名):CLI 二进制 = Python 包名 = skill slug = `edaloop`;仓库名/对外全称 = `jlc-edaloop`。

## 3. 非目标(与目标同等重要)

| 不做 | 理由/归属 |
|---|---|
| 自研 PCB 布局/布线算法 | 上游 easyeda-agent 已覆盖(auto-place/route-short/route-critical/power-pour/Freerouting);本项目 M8 只做**编排与门禁**(消费其 CLI,不重造,ADR-0009) |
| 无人值守自动支付 | 资金安全:下单止步于「报价单 + 订单草稿」,支付动作永远由人确认(M9 硬门禁) |
| 芯片级(IC)设计 | Synopsys/Cadence 战场,工具与数据模型完全不同 |
| 自建 EDA 编辑器/通信层 | 复用 easyeda-agent 的 daemon/连接器/typed actions,不重造轮子 |
| 交互式拖拽 UX 类能力 | 平台墙(官方无 `eda.*` API,见调研) |
| 模拟性能优化(增益/带宽/补偿网络/时序综合) | 二期之后(ORACLE/LLM-USO 路线已调研)。**边界区分(ADR-0010,2026-08-21)**:供电与无源器件的确定性校核与选值落图(电压兼容/电流预算/阻容值;限 R/C 标准件,值须来自规则公式或 datasheet 建议带 quote 出处,自动改已有连线禁止)属 Phase 4 范围,不属本条非目标 |
| 闭源 SaaS / 强制云服务 | 与差异化支柱 1 冲突 |

## 4. 核心设计原则(从调研提炼,评审争议时以此裁决)

1. **块组合优先,自由生成兜底**:AnalogXpert 实证两段式(选块+连线)成功率 40% vs 直接生成 3%。生成器永远先查块库,查不到才自由生成,且自由生成结果过更强门禁。
2. **强/弱门禁分级**:能机械证明的做强门禁(fail/block:连通性、DRC、bbox、pin 集合 diff);不能的做弱门禁(标记+人工确认:去耦建议完备性、需求歧义裁决)。**绝不静默通过**。
3. **校验反馈结构化**:回喂 LLM 的是归因后的 finding(带坐标/网络名/根因分类),不是原始日志——收敛性取决于反馈质量(easyeda-agent DRC 5 轮 31→0 的实践经验)。
4. **ground truth 优先于 LLM 输出**:pin 集合、器件存在性、C 号一律以 LCSC 库为准;库与提取冲突时人工裁决,不静默取任何一方。
5. **一切中间产物可追溯**:每条提取/生成/检索结果带出处(datasheet 页码/块 ID/检索分数),审计日志是一等公民。
6. **不与官方生态竞争,做薄而规矩的公民**:easyeda-agent 的 CLI/连接器/skill 独立演进,我们只做消费方;版本对其 release 钉死。

## 5. 系统架构

### 5.1 模块图

```
┌──────────────────────────────────────────────────────────────────┐
│                        edaloop (本仓库)                           │
│                                                                  │
│  ┌──────────┐   ┌───────────────────────────────────────────┐   │
│  │ M1 输入层 │   │ M2 知识层(RAG)                            │   │
│  │ intent   │──▶│ ①结构化datasheet库(引脚/参考设计/建议)      │   │
│  │ 需求/BOM/ │   │ ②已验证案例库(电路块+成功交付,自进化)       │   │
│  │ datasheet │   │ ③LCSC器件库镜像(C号/pin/库存/价格)         │   │
│  │ → 设计IR  │   │ 检索:向量+图拓扑+关键字 混合               │   │
│  └──────────┘   └───────────────────────────────────────────┘   │
│       │                    │                                      │
│       ▼                    ▼                                      │
│  ┌───────────────────────────────────────────────────────────┐   │
│  │ M3 生成层(两段式)                                          │   │
│  │  a. 块选择: 设计IR+检索 → BlockPlan(块组合+端口绑定)        │   │
│  │  b. 块连接: BlockPlan → easyeda typed actions 序列          │   │
│  └───────────────────────────────────────────────────────────┘   │
│       │                                                          │
│       ▼                                                          │
│  ┌───────────────────────────────────────────────────────────┐   │
│  │ M4 校验层                                                   │   │
│  │  强: 连通性=设计IR逐条对齐 / sch gate(复用) / pin diff     │   │
│  │  弱: SPICE工作点(子集) / 人工确认队列                        │   │
│  └───────────────────────────────────────────────────────────┘   │
│       │                                                          │
│       ▼                                                          │
│  ┌───────────────────────────────────────────────────────────┐   │
│  │ M5 迭代控制器                                               │   │
│  │  finding→归因(缺块/错连/错值/选型)→定向反馈→重生成          │   │
│  │  上限5轮 / 同错两轮升级人工 / 通过→交付+案例回写M2②         │   │
│  └───────────────────────────────────────────────────────────┘   │
│                                                                  │
│  M6 datasheet入库管道: PDF→证据页→双通道提取→交叉校验→入库①      │
│  M7 CLI/编排: edaloop run/ingest/eval 子命令,审计日志,状态机     │
│  M8 PCB编排(Phase 3): sch→PCB交棒→auto-place→outline→route     │
│     →drc/check 门禁转译→PCB迭代环(全部消费上游 pcb 命令)        │
│  M9 下单通道(Phase 3): gerber/BOM/POS导出→SMT兼容预检→报价单    │
│     →人工确认硬门禁→订单草稿(止步于待支付)                      │
└──────────────────────────────────────────────────────────────────┘
         │ subprocess(严格钉版本)
         ▼
  easyeda-agent(外部依赖): CLI/daemon/连接器/sch gate/电路块库
         │ WebSocket + 官方 eda.* API
         ▼
  EasyEDA Pro(开启"允许外部交互")
```

### 5.2 模块职责与边界

| 模块 | 职责 | 明确不做 |
|---|---|---|
| M1 intent | 输入解析→`DesignIR`;歧义生成确认问题 | 不做对话 UI(PoC 用 CLI 交互) |
| M2 knowledge | 三库存储+混合检索+案例回写 | 不做在线爬虫(批量预镜像 LCSC) |
| M3 generator | BlockPlan 生成与落图 | 不直接调 `eda.*`(只产 typed actions 或调 easyeda CLI) |
| M4 validator | 强/弱门禁执行,产出结构化 findings | 不修图(修复属 M5 决策) |
| M5 loop | 迭代控制、归因、防震荡、交付打包 | 不做无人值守全自动(默认每轮可人工断点) |
| M6 ingest | datasheet→结构化入库(v1 调研管线) | 不支持扫描件(声明不支持) |
| M7 cli | 编排以上全部+审计日志 | — |
| M8 pcb(规划) | 消费上游 pcb 命令编排布局布线全流程;PCB 门禁 findings 转译进 M4 | 不自研布线/铺铜算法;不做交互式修线(档位策略照抄上游:稀疏 route-short/稠密人机协作) |
| M9 order(规划) | 制造文件导出+SMT 兼容预检+报价+订单草稿 | 不做自动支付(人工确认硬门禁);不承诺兼容非嘉立创制造 |

### 5.3 关键数据契约(初版 schema,实现时可细化但字段名不改)

```yaml
DesignIR:            # M1 输出,全链路的"设计意图真值"
  id, source(文件/对话), created
  functions: [ {name, desc, constraints[]} ]      # 功能块需求
  interfaces:  [ {type: usb/uart/spi/gpio..., spec} ]
  power:       {inputs[], rails[{voltage, imax}], protection?}
  env:         {temp, size, cost_target?}
  open_questions: [ {id, question, options[]} ]    # → 人工确认队列

BlockPlan:           # M3a 输出
  blocks: [ {block_id(案例库ID), ports_binding{port→net名}, params} ]
  nets:  [ {name, class: power/signal/high_speed} ]
  confidence + provenance(每块检索分数/出处)

Finding:             # M4 输出 = M5 输入(结构化反馈的核心!)
  code: MISSING_BLOCK|WRONG_NET|PIN_MISMATCH|DRC_*|LAYOUT_*|...
  where: {ref/net/pin/xy}
  evidence, severity, suggested_fix_class

CaseRecord:          # 交付成功后回写 M2②
  DesignIR摘要 + BlockPlan + 最终网表hash + 门禁全绿截图/数据
  + 使用了哪些检索命中(反哺检索质量评估)
```

## 6. 技术选型(及理由)

| 层 | 选型 | 理由 | 备选 |
|---|---|---|---|
| 语言 | **Python 3.12+** + `uv` | LLM/PDF/RAG 生态最快;easyeda-agent 是 Go,经 CLI 子进程消费,零耦合 | — |
| LLM(文本) | **GLM-5.3**(智谱,OpenAI 兼容端点) | 用户决策(ADR-0007);中文强;key 兼 cover 多模态候选;必须可换任意 OpenAI 兼容端点(含 DeepSeek/本地 vLLM/Ollama)——**接口层强制抽象,禁止业务代码直连 SDK** | DeepSeek 官方 API(回切项) |
| LLM(多模态,datasheet 图表) | 待 PoC 定(候选:Claude / GLM-4V / Qwen-VL) | Week1 评测选型,写入 ADR | — |
| Embedding | **BGE-M3**:PoC 走硅基流动 API,Phase 1 落本地权重 | 中英双语;云端/本地同为 1024 维可无缝互切;接口层强制抽象,禁止业务代码直连(ADR-0006) | 纯本地 FlagEmbedding(即兜底方案) |
| Reranker | **BGE-reranker-v2-m3**(硅基流动,Phase 1 可换本地) | 混合检索精排(dense+BM25 粗排 → rerank),显著优于纯向量召回 | 本地 FlagEmbedding |
| 向量库 | **sqlite-vec** | 单文件、零运维、可随库分发 | Qdrant(超过 50 万条再迁) |
| PDF 解析 | PyMuPDF(+ camelot 备用通道) | v1 调研已验证 | — |
| 结构化输出 | pydantic v2 + LLM 结构化输出 | schema 即契约 | instructor |
| LLM 编排 | **不引入框架**(裸函数调用) | PoC 阶段显式状态机比 langchain 可调试;M3 之后再评估 | — |
| 测试 | pytest + goldens(固定 datasheet/需求样本) | 评测基线是命根 | — |
| 用户界面 | **Chainlit 2.x**(`[project.optional-dependencies] ui`,ADR-0012) | 聊天+上传+长任务流式;Ask* 家族映射弱门禁问答;core 零依赖走 extra(纪律同 ADR-0002) | Gradio(依赖重)/Open WebUI(产品形态不合+品牌条款) |

## 7. 仓库结构(约定)

```
jlc-edaloop/
  docs/
    DEVELOPMENT.md          # 本文档(总纲)
    research-*.md           # 三份调研(只读存档)
    adr/NNNN-*.md           # 架构决策记录(小事进本文档,大事出ADR)
  src/edaloop/
    intent/                 # M1
    knowledge/              # M2(store/retrieve/writeback)
    generate/               # M3(plan/apply)
    validate/               # M4(strong/weak/findings)
    loop/                   # M5(controller/attribution)
    ingest/                 # M6(datasheet pipeline)
    llm/                    # provider 抽象(强制)
    ui/                     # Web UI(Chainlit 薄适配+会话层纯逻辑;core 零依赖,ADR-0012)
    cli.py                  # M7
  seeds/                    # 知识库种子(迁移的电路块+参考设计)
  evals/                    # 金标准集:需求样本×期望指标+固定datasheet集
  tests/
  pyproject.toml
```

## 8. 前期部署工作(M0,开发第 0 周 checklist)

### 8.1 环境清单

- [x] Git 仓库初始化,main 分支保护,推送远端——2026-08-17 首次 commit(b8be6b9)+ push 完成;分支保护规则(禁 force push/禁删)由用户网页端配置
- [x] Python 3.12 + uv;pyproject + 依赖(pydantic/pymupdf/pytest)+ `src/edaloop` 七模块骨架 + CLI(`edaloop --help`,pytest 4/4)
- [x] **EasyEDA Pro 安装**(Windows),设置开启「设置→系统→允许外部交互」——2026-08-17 用户真机验证通过
- [x] **easyeda-agent 安装**:`install.sh` 四件套(CLI/daemon/连接器.eext/skill)——2026-08-17 用户真机验证通过;版本钉 **v0.25.1**(ADR-0002,已录入 pyproject `[tool.edaloop]`)
- [x] 验证 `easyeda daemon health` 连接器非 stale;跑通官方 showcase 的最小命令(`easyeda sch read`)——用户报告通过(ADR-0004 的 `resolve-lcsc` 字段核验仍待真机补验)
- [x] LLM API key:**GLM-5.3**(ADR-0007,`EDALOOP_LLM_KEY` 入 `.env`,gitignore);同一智谱 key 兼 cover GLM-V 多模态候选
- [ ] 多模态模型候选 key 各一(评测用)
- [ ] BGE-M3/reranker:PoC 走硅基流动 API(`EDALOOP_EMBED_KEY` 入 .env,同一 key 复用 reranker);本地权重下载挪至 Phase 1 前完成(ADR-0006)
- [x] LCSC 访问:调研 easyeda-agent 现成的库查询通道(`resolve-lcsc` 命令复用)vs 直接 API;结论出 ADR → **ADR-0004 草案:PoC 复用 easyeda CLI 单通道**(待 W0 真机验证转正)
- [x] evals 金标准集 v0:**5 份需求文档**(从 easyeda-agent 的 esp32MiniRequire.md 改写 2 份 + 自写 3 份:电源板/接口板/混合)+ **10 份 datasheet**(v1 清单:ESP32-S3/CH340/AMS1117/STM32F103 等)→ `evals/`(README 含清单与下载踩坑记录;CH340 实取 LCSC 镜像的 CH340 系列 datasheet)

### 8.2 运行时拓扑(本地)

```
edaloop(Python) --subprocess--> easyeda CLI --> daemon(:60832) <--WS-- 连接器.eext <-- EasyEDA Pro
```
单机全本地;外呼仅:LLM API + embedding/reranker API(均可换本地端点实现全离线,ADR-0006)。

## 9. 里程碑路线

> **2026-09-01 重组注**:Phase 0-4(已收官)的执行细节——差距审计表、批次计划、逐批执行叙事与经验教训——已**原样归档**至 [docs/history/](history/)(零改写);本节只保留各阶段**收官快照**与**仍生效的决议**。Phase 5(进行中)维持全量记录。

### PoC(4 周,2026-08-17~18 全 Go)——执行记录详 [history/phase-0-poc.md](history/phase-0-poc.md)

W0 环境就绪(真机四件套验证)→ W1 混合检索 recall@5=**88%** → W2 真机落图 3/3 过 `sch gate`(M3 四件套 plan/compile/adapter/audit)→ W3 迭代闭环 pass@3=pass@5=**80%**(M4/M5 落地:Finding/归因/防震荡 HALT)→ W4 datasheet 管道(ULN2003 16/16 脚双通道校验)+ 库外器件 place 通道 + showcase 零手工 2 轮收敛 + 案例回写。双指标达成 → 立项 Phase 1。

### Phase 1 可用产品(2026-08-18~20,八批全过)——详 [history/phase-1.md](history/phase-1.md)

块库 20→**95**(全部符号回读验证;七批 W1 指标适配 top-k 5→8+功能等价类,recall@8=88% 维持);金标准 5→**22 需求**,全量 eval **22/22=100%、全 1 轮**(布局策略 v2:721 次审计标定占位表+spacing 600)。机制沉淀:substance-verify(rollback 说谎防御)/窗口治理(windowId 粘滞钉扎)/`replay`/交付打包/`questions` 弱门禁队列;skill 打包 + GitHub Release v0.2.0。

### Phase 2(2026-08-20,ADR-0008 排序 A→C→B 全落地)——详 [history/phase-2.md](history/phase-2.md)

P2-A 自由拓扑受控子集(确定性模式库+LLM 兜底+拓扑 sanity 门禁;负样本诚实拒绝) / P2-C BOM 成本(wmsc 免鉴权实时价,fetch/summarize/cost_hint;价格=弱信号,零侵入检索) / P2-B 参数 sizing 三类规则(LED 限流/分压/电源电容,E24 归一)。回归分层 smoke/daily/all 定型;交付物四件套定型(svg+net.json+bom.json+sizing.txt)。遗留(parts[].lcsc 回填等)→ P3-0 承接。

### Phase 3 全链路(2026-08-21 六批全 Go + 端到端验收 ✅,v0.5.2 关闭)——详 [history/phase-3.md](history/phase-3.md)

「脑洞→原理图→PCB→订单草稿」全链打通:P3-0 数据债(--answers 回灌+IR 版本化)→ P3-1 refine 细化闭环(decisions 双注入)→ P3-2 swap 选型(JLC SMT API 不可达,降级 wmsc 近似)→ P3-3 sizing 扩容(BUCK 纹波全公式/TVS/保险丝/热校核)→ P3-4 critic 六维评审 → P3-5 PCB 18 步编排(上游 stage 状态机+通用布局修复循环)→ P3-6 三段报价+订单草稿(--confirm 硬门禁,永不支付)。**e2e 验收(req-27 智能门磁):零手工编辑走通全链**。计划偏差与遗留详归档;新增风险 R12-R15 已并入 §11。

### Phase 4 质量深耕(2026-08-21~22 七批全 Go;ADR-0010)——详 [history/phase-4.md](history/phase-4.md)

- **F1 需求识别**:IR v2(Spec 结构化约束/宽压轨/Env.fab)+ AcceptanceSpec(需求→规格→checker 映射,「## 期望指标」不再丢弃)+ FUNC_UNCOVERED 恒弱 checker + refine 深化(embedding 近邻换词);eval refine:生成率 94%/可执行率 91%/零误伤。
- **F2 知识检索**:BlockRecord 电气字段+wmsc 回填工具链(fetch→人工审→apply)+ datasheet 电气参数表通道(视觉行聚类)JOIN 回填 + 检索 v3 五通道(电气 digest/rail 转换器/意图行槽/同族 cap/案例第五通道);w1 扩标注 74 条 recall@8=93-95% ≥92% Go。
- **F3 布局质量**:A4 尺度发现(1170×825,历史产出全超幅)→ 三带流式布局 → 真机墨迹表标定(_INK_CELL/_PLACE_INK)→ 行-货架页流+多页通道+收口管线(clusters 探针/拆组/group-arrange gap 梯子/钳移兜底/zone-arrange 修复级);G33 批(v0.6.10)治页爆炸与图框丢失。
- **F4 电气正确性**:check_voltage_compat(强,供电口分向分类防误杀)/ check_current_budget(三态)/ check_rails 双向化 / PARAM_OFF_SPEC(弱观察)/ sizing 轮内化+输入来源表+std R/C 落图通道 / critic 闭环;eval electrical 10/10 捕获 0 误杀,params 全 Go。
- eval 分层扩 electrical/params/refine(§10);pytest 234;R16-R20 已并入 §11。

#### Phase 4 决议(ADR-0010,仍生效;原 §4.2 原文)

1. **§3 非目标措辞修订**(已改):区分「模拟性能优化(增益/带宽/补偿网络/时序综合)——仍二期之后」与「供电与无源器件确定性校核与选值落图——Phase 4 范围」;后者边界=限 R/C 标准件、值须来自规则公式或 datasheet 建议(带 quote 出处)、自动改已有连线禁止。
2. **ADR-0010① 布局实现路径与命令选型论证**:编排上游命令面(A4 primitive/zones set/zone-plan/zone-draw/note/zone relayout·tidy/sheet tidy/layout-score/sheet-geometry/page-new)+自研分区与评分策略,**不自研 EDA 布局算法**(原则 6 薄公民);显式论证**为何不用 `sch autolayout --engine template`**——其 --apply 拒绝含任何 wire/netflag/netport/netlabel 的页,而 block-apply 落块即带内部连线;替代管线 place→autolayout→autoconnect 会放弃块通道的原子接线优势(place 通道自由拓扑件可在接线前借用,记录备查);修复环优先上游 zone relayout/zone tidy/sheet tidy,自研挪件仅兜底。
3. **ADR-0010② 电气数据分级**:块级 v_min/v_max/i_typ(desc 搬运+人工终审+LCSC wmsc[paramVOList 映射字典,首日 spike 定论])=强门禁输入;datasheet prose 建议类(带 quote/page)=弱门禁;datasheet 数值参数表(P4-6 通道)=可机械校验类,按双通道一致性分级入库。
4. **ADR-0010③ 案例回写**:触发=gate 全绿+交互交付 run;防污染三护栏(仅交互写/origin 标记/hash 去重)为**硬约束**,违者评测基线作废。
5. **§5.3 契约注记**:DesignIR/BlockRecord/Finding 字段名只增不改;PlannedBlock.zone=S0 spec modules[].zone 单一真值源+category→zone 映射表;Finding 新增 code 及分级:VOLTAGE_OUT_OF_RANGE(strong)/RAIL_BUDGET_OVER(strong)/RAIL_BUDGET_UNKNOWN(weak)/PARAM_OFF_SPEC(weak,连续 2 批零误报转强)/FUNC_UNCOVERED(恒 weak)。
6. **§10 工程纪律增补**:eval 分层扩至 layout/electrical/refine(发版跑 all 含新层;PR 跑 daily+相关新层;layout 层先 daily 级跑通再升全量)。

### Phase 5(v1.0 打磨与发布,2026-08-22 规划,ADR-0011)

**范围决议**:Phase 1-4 交付了「需求→原理图→PCB→下单草稿」全链与四重点质量深耕(F1-F4),七批全部 Go 收口。Phase 5 定位**打磨而非扩量**:不加新能力面,把已交付能力里**只被单测/探针验证过、从未在真数据上闭环**的通道走通,补齐评测全集与发布工程,产出可打 tag 的 v1.0.0。明确出 v1.0 范围:多页大原理图增强/KiCad 后端(ADR-0008 挂起条件不变)、模拟性能优化(§3)、自由拓扑模式库扩量(v1.x 候选)、上游 issue 推进(等用户确认,独立于本 phase)。

#### 5.0 Phase 5 差距审计(2026-08-22,基线:提交 4370012 / pytest 234 / 97 块 / w1 14 需求 74 条 93-95% / cases 2 条 / smoke3+daily8 绿)

| # | 环节 | 现状 | 缺口判定 | 量级 |
|---|---|---|---|---|
| G27 | 上游版本钉扎 | ADR-0002 钉 v0.25.1(adapter._PINNED_VERSION+pyproject [tool.edaloop]);本机实装 **v1.1.1**(2026-08-22 `easyeda --version` 实测);用户双机轮换开发,另一台 0.25.1 计划同步升级 | check_version 对不一致 raise → 本机任何真机 stage_apply 必被版本门拦;「升级=独立批+全量回归」的回归从未执行(0.25.1→1.1.1 跨 26 minor,R17 探测清单全部过期) | **高(真机阻塞)** |
| G28 | 生产数据闭环 | elec_rows 视觉行提取(真 PDF 单件)+JOIN 回填(合成表)+elec-deny(注入样本)各自验证过 | 生产 runs/knowledge.db **无 datasheets 表**(2026-08-22 实测,仅 blocks 族+cases)——「ingest→store→rebuild→回填→deny」真数据端到端零执行;eval 库每次从 seeds rebuild 同样无 datasheets,JOIN 通道在评测路径也结构性关闭 | 高 |
| G29 | 案例生产消费 | 案例通道+回写三护栏落地,种子案例 2 条;跨需求命中以探针演示(req-02 案例→req-14 rs485 6→3) | 真实(非 req-*/evals 源)run 的 case-writeback **从未触发**,生产审计链无案例事件;P4-6 Go 的「回写案例被后续 run 消费,审计留痕」仅以消融探针满足,非 run 级证据 | 中 |
| G30 | 评测标注全集 | w1 标注 14 需求 74 条(v2 分层重组后现行件);_req_path 已兼容 archive/ | 13 件归档需求文本仍是有效检索输入,26 需求全集标注自 P4-6 顺延未做;P5-1 回填会改索引文本(电气 digest 入 FTS)→ 排名会动,没有全集回归网 | 中 |
| G31 | BOM/落图数据尾差 | parts lcsc 覆盖 89/90=99%(P2 期 62 处已大幅回填) | 8 块零 lcsc:2 个 std-value 通道件(设计如此,plan 期查表得 C 号)+battery-18650-holder+5 个 up-\* 多器件块(parts 不全);delivery.bom.json 有价覆盖率从未量化 | 低 |
| G32 | 发布工程 | 仓库版本 pyproject 0.2.0 与进度脱节;P3/P4 各批有 smoke/daily 纪律但无「发版」动作 | 无 tag 纪律(仅代码 tag v0.5.0 于 Phase 4 起点)、无发版 checklist 文档、README/快速上手未与 CLI 现状对齐;§10「发版跑 all 含新层」未成文执行过 | 中 |
| G33 | 布局页爆炸+图框丢失(**2026-08-24 修复**,G33 批详 §13 v0.6.10) | 根因三连:①titleblock `--data` 写损毁 sheet 符号引用(0.26.0 明令禁令)→图框灭→group-arrange 拒动+钳移静默死→2 轮同码 HALT;②place 通道零量测恒 400×250 保守格+单列页流→1 件/页。**已修**:F1 titleblock 只读化(仅 `--show`,审计每页)/F2 几何缺失钳移回退带 `(100,300,1100,780)`+审计(不再静默)/F3 行-货架页流+16 类真机实测墨迹表(`_PLACE_INK`,宽块独行=旧单列逐坐标保真,`_FLOW_W=400` 单开关回退)/F4 overlap evidence 补 `a=/b=` 位号;req-11 **34 页→7 页、60-70min→13min PASS** | 残余(下批):翼展垫 `_WING_PAD_X=120` 对长网名低估→多引脚大件邻域重叠(req-01/06/07 确定性 HALT 族:Q1/U2-U3/MCUSTM32 邻域;钳移链能修非每轮收敛——**治本=dx 按真实网名长度估算**(网名 autoconnect 前已知),治标=垫 120→180);out-of-sheet REPLAN 类(req-02 历史病灶+req-07 J2)未治 | 低(页爆炸消除、图框不再复发;残余为布局收敛质量非正确性) |

**结论**:能力面已齐,欠的是三件——**真数据闭环**(G27 版本门先开,G28 数据流再通)、**评测全集**(G30,给回填后的检索一张完整回归网)、**发布工程**(G32)。排序:P5-0 先行(真机门被版本卡死,P5-1/P5-2 都要真机)→ P5-1 → (P5-2/P5-3/P5-4 可交错)→ P5-5 收口。

#### 5.1 Phase 5 里程碑(每批独立 Go/No-Go,R15 纪律延续;Go 指标全部机械评测)

| 批次 | 内容 | 交付物 | Go 指标 |
|---|---|---|---|
| P5-0 上游钉扎对齐(~2-3 天,**先行**) | ①钉扎 0.25.1→1.1.1:adapter._PINNED_VERSION+pyproject [tool.edaloop] 同步(双机开发前提:另一台用户将同步升级,版本门保持严格单值——拦住未升级机器正是它的职责);②**版本门漏洞修复(执行中发现)**:check_version 仅 stage_apply 调用,**run 主链(stage_run/LoopController)从不查版本**——P4-6 的 daily 8/8 实为 1.1.1 实装+0.25.1 钉扎的漂移下裸跑;门前移到 controller.run() 首轮前(真机非 dry_run 才查,Fake/无方法适配器跳过);③**命令面再探测**(R17 清单重跑:destagger/zone-arrange/extract-layout 等在 1.1.1 的可用性+新命令扫描,结论回写探测清单);④全量回归:pytest+eval 全层(w1/w3-loop+electrical/params/refine)+smoke3+daily8(**清 state 真跑**——执行中另发现 smoke/daily 有 resume state 缓存,旧 PASS state 会 skip(done) 掏空回归门,发版纪律补一条:升级批必须清 state);⑤ADR-0002 修订记录(§12);⑥**smoke 真跑暴露三颗环境雷(2026-08-22 晚取证,非代码回归)**:(a) EasyEDA 云端库 C143135(FMS SMAJ5.0A,tvs.smaj5v0_sma)器件数据损坏——`sch place` 拉全量数据挂死 API(6.2s 熔断,整栈重启前后一致;对照 C9900016950/C1979411 均 2-3s 成;当日 08:13 前同块 25 次成功)→ 换 C1979411(Vishay 同 MPN 同封装,pins 不变):skill `references/standard-parts.json`(daemon 活读,实证)+本地 `seeds/blocks.jsonl` tvs-smaj5 同步;(b) **页数累积压垮 netlist 导出**:eval 复用工程,page-clear 只清内容不删页 → 38 页时 `sch netlist` EDA_CALL_FAILED「Netlist export returned no file」→ block-apply 内置净验证全缺脚假阴性 → GATE_FAIL(bridge-check 活着,砍到 1 页即恢复)→ **controller._ensure_pages 增页修剪**(删计划外 ^P\d+$ 孤儿页,单发不重试,失败不判负;+1 单测);(c) lceda-pro 连跑 25h 连接器劣化(daemon seqAbandoned 3→13)→ 用户重启即愈;⑦上游 issue 候补两枚(C143135 云数据/netlist 导出随页数超时),待用户确认后报 | 新钉扎+版本门前移+探测清单更新+页修剪+回归报告 | 全量回归零回退(w3 轮数允许 +1);版本门对 1.1.1 放行、对其他版本仍拦(单测断言);探测结论全部回写 R17 行;block-apply 探针 rc=0(6/6 placed+reconciled,修复后实证) |
| P5-1 生产数据闭环(ingest→JOIN→deny,~1 周;依赖 P5-0) | ①evals datasheet 语料(10 PDF)真机 ingest 入生产库(datasheets 表落地,行数/通道统计);②seed 重建后 JOIN 回填量化(回填块数/字段数,抽检 ≥5 处对回原 PDF 页码与数值);③elec-deny 真数据探针(24V 直入类查询 deny 生效,seeds vmax 数据现成);④w1 回归(回填改索引文本→排名会动,这是本批最大回归面) | 生产 datasheets 表+回填报告+deny 探针 | ≥8 部件入库且 elec 行 ≥50;回填 ≥10 块字段且抽检 5 处与 PDF 一致(**一处错=No-Go**);deny 探针通过;w1 ≥92% 不回退 |
| P5-2 案例生产消费(~3-5 天;依赖 P5-1 生产库就绪) | ①≥2 份金标准集外、**非 req-\* 命名**需求真机 run(复用 P3 全新需求收口先例),断言 case-writeback inserted=true 落审计;②后续相近需求 run 消费该案例(检索 evidence 含 case 通道命中+块组进候选,审计留痕);③护栏回归(eval 源零写入已有单测,补生产 cases 行数断言) | 审计链案例事件+消费证据 | 回写 ≥1 条 inserted=true;后续 run 案例通道命中 ≥1 次且块组进候选;top-k 污染检查(不相近查询不得拿 case 加分,复用消融断言) |
| P5-3 w1 26 需求全集标注(~1 周,纯数据;依赖 P5-1 排名稳定) | ①archive 13 件补标注(同口径:需求原文点名件+泛功能块分类,金标按文本完整性不迁就检索);②全集基线连跑 3 次定方差;③Go 线按基线数据定(先跑再定线,杜绝先验凑数),写死进 evals_w1 | 26 需求标注集+基线报告 | 标注条数 ≥120;3 次运行极差 ≤2 条;Go=基线均值−1 条且 ≥90%(机械声明入代码) |
| P5-4 BOM 尾差清理(~3-5 天,独立可提前) | ①✅基线已量化(2026-08-22,22 个历史 run,124 BOM 行):**有价 87/124=70%;缺价归因 C99xx 延展号段无商务数据 26 行(74%,数据源限制)/SSL 瞬态 6 行/up-esp32_autodownload 无 lcsc 3 行/std 无值件 3 行**——原计划猜的「up-\* parts 不全」实测只占 3 行,最大缺口是 C99xx;②bomcost fetch 加一次重试消 SSL 瞬态(弱信号不抛原则不变);③up-esp32_autodownload 等 no-lcsc 块 parts 补录(wmsc 查 C 号,优先基础库件避开 C99xx);④**C99xx 与 std 无值件单列豁免**(数据源限制如实报告占比,不进有价分母) | 覆盖率报告+补录块(回读校验)+重试修复 | **非豁免行有价覆盖率 ≥95%**(豁免行=C99xx/std 无值,单列报告);SSL 瞬态重试后 ConnectError 行归零;补录块 wmsc 回读一致;w1 不回退 |
| P5-5 v1.0 发布收口(~2-3 天,收口) | ①pyproject 1.0.0+仓库 tag v1.0.0(tag 本地创建,**push 需用户明示**);②发版 checklist 文档化(全层 eval+pytest+真机 smoke+清 state 真跑纪律,**固化仓库根 RELEASING.md——docs/ 是 gitignored 本地文档,放那里双机不同步**);③README/快速上手刷新(装依赖→seed→run→交付物四件套→refine);④§13 变更记录 | v1.0.0 发布+RELEASING.md | 全层 eval 绿+pytest 绿+真机 smoke ≥1 需求 PASS(1 轮);checklist 存在且本次发版即按它执行(首次执行即验证) |

**依赖与顺序纪律**:P5-0 先行(G27 真机阻塞,后续真机批全依赖);P5-1 次之(G28 数据流+给 P5-3 稳排名);P5-2 依赖 P5-1;P5-3 依赖 P5-1(回填后排名稳定再标注定线);P5-4 全程独立可并行;P5-5 收口。每批结束更新本文档+变更记录;门禁零豁免(§10)。

#### 5.2 ADR-0011 决议(本节即决议,随本规划生效)

1. **v1.0 = 打磨不扩量**:范围限定真数据闭环(G27/G28)+评测全集(G30)+数据尾差(G31)+发布工程(G32);新能力面一律出 v1.0。
2. **ADR-0002 修订**:easyeda-agent 钉扎 0.25.1→1.1.1。双机开发环境(用户两台轮换),版本门保持严格单值——跨机漂移正是版本门要拦的对象;另一台升级走四件套同版(CLI+connector .eext 以 GitHub Release 为准)后,两台同过 1.1.1 门。
3. **生产数据闭环先于评测扩容**:G28 的回填会改检索索引文本(电气 digest 入 FTS),必须先跑 w1 回归确认不回退,再投入 26 需求标注工作量(防「标注完又改排名」返工)。
4. **案例生产消费的验收以审计为准**:探针演示不算数,须 run 级审计事件(case-writeback inserted=true+后续 run 检索 evidence 含 case 通道),与 P4-6 Go 判据原意对齐。

#### 5.3 Phase 5 新增风险(已并入 §11 登记)

- **R21 回填改检索排名,w1/daily 回退**(高):seeds 不动、回填只走生产库 JOIN(缺才补不覆写),单批可整体 revert;P5-1 Go 内嵌 w1 ≥92% 不回退,不达标不合入。
- **R22 上游 1.1.1 命令面漂移**(中):0.25.1→1.1.1 跨 26 minor,命令签名/行为可能变(destagger/no-connect 等已知副作用面在 1.1.1 状态未知);P5-0 探测先行+全量回归零豁免;不可用命令按 R17 模式降级/绕行。
- **R23 全集标注工作量挤占**(中):13 件×~5-8 条纯人工;放在 P5-1 后且可切分逐件提交;Go 线由基线数据定而非先验,标注本身不阻塞其他批。

#### 5.4 P5-0 断点(2026-08-23 双机交接;代码已 WIP 提交推送,**回归未全绿,非 closeout**)

**状态快照**:①②③⑤⑥⑦全部就绪——钉扎 1.1.1(adapter+pyproject)、版本门前移 `controller.run()` 真机首轮前、R17 探测回写(zone-violation 反向除名)、ADR-0002 修订(§12)、tvs C143135→C1979411 真机验证(req-01 fresh4 15/15 applied、0 apply-error)、页修剪真机首删 P5 正确、C143135 云端库损坏 issue 草稿(docs/upstream-issue-c143135-cloud-lib-corrupt.md,待用户确认后 gh issue create)。另:evals_w3 resume 修复(**只跳 PASS,HALT/ERROR 重跑**,+2 测试——旧代码把环境崩溃遗留 HALT 行当已完成跳过,smoke 曾被掏空成 2/3 而不自知)。回归:pytest **239 全绿**/w1 94.6%/electrical+params+refine 全 Go/**smoke 1/3**(req-08 PASS r1;req-01、req-11 挂在下述环境雷,均非代码回归);daily/rest 未跑。主仓 WIP 断点提交已推(`wip(p5-0)`),全绿后补 §13 v0.6.10+本节执行记录即为 closeout。

**三颗环境雷(全部非代码)**:

1. **P1 图框持久关闭(req-01 HALT 根因)**:工程 edaloop-w2-req01 首页 P1(uuid `7c4a751c5136cb01`)的 Border/Title Block 结构开关被持久关掉(2026-08-22 晚调试/崩溃期间;跨 app 重启仍在)→ `sch group-arrange/clusters --doc P1` 单发读拿不到 sheet 图元→「取不到图纸几何」拒动→P1 overlap 治不动→GATE_FAIL×2→HALT;同分钟 titleblock 的 settleRead 却能读到(上游读不对称,issue 候选)。**修复**:`easyeda sch titleblock --data '{"Title Block":{"value":1},"Border":{"value":1}}' --doc 7c4a751c5136cb01` → `easyeda sch sheet-geometry` 验 bbox 回来;无效则弃旧工程换全新空工程(page-new 页自带框)。注:此损伤在本机工程上;另一台机器首次跑会新建工程天然绕开,若复现同症状用同一命令(--doc 换其首页 uuid)。
2. **连接器假死**:负载 30-70 分钟进程在/WS 死(`easyeda health` 见 `windows: []`),完全重启 EasyEDA 即愈,今日 3 次;req-11 单 req 60-70 分钟(~30-38 页重载属正常规划,非 bug)正好撞窗。
3. **lib search 间歇查空**(KF301 C9900016950、C1525、C4075 等已知好件返回空)——上游记录过的限流型抖动,当日放大。

**次要发现(未修)**:controller.run() 对 CompileError 无轮内容错(req-11 r3 规划退化出 isolator-pc817 无 pins_binding→炸穿整 req 成 ERROR;v0.2.2 教训-d 同类,应纳入重试循环);上游 group-arrange/clusters 单发读 vs titleblock settleRead 不对称(issue 候选#3)。

**双机同步基建(2026-08-23 理顺)**:`docs/`=嵌套仓 jlc-edaloop-notes;`runs/`=嵌套仓 jlc-edaloop-runs(自旧 `run/` 迁入,*.db 排除);`bash docs/sync.sh "说明"` 开工/收工各跑一次(幂等)。**⚠ sync.sh 必须在两目录都成为仓之后才能跑**——`git -C runs` 若无嵌套 .git 会向上落到主仓,其 `add -A` 会把 easyeda-agent//samples//.easyeda/ 吞进主仓提交并推送。另一台机器首次接入,先查 `ls -d docs/.git runs/.git`:已有 .git → 直接 `git pull`;没有 → 无旧 `run/` 克隆时现场 init(`cd runs && git init && git remote add origin https://github.com/Yyin-Tta/jlc-edaloop-runs.git && git fetch origin && git checkout -f -B main origin/main`;docs/ 同法,远端换 jlc-edaloop-notes.git;本机独有文件不受影响,同名文件被远端正本覆盖——state 正该如此),有旧 `run/` 克隆则迁移:`mkdir -p runs && git -C run fetch && mv run/.git runs/.git && git -C runs reset --hard origin/main`。旧 `run/` 目录善后(本机已完成,另一台迁移后同理):903 件已在 93187aa 历史,31 个未提交件归档于 `runs/run-legacy/`,原目录已删。

**续跑清单(另一台机器)**:①`easyeda health` 确认窗口→(本机工程才有雷1,新机器直接跑)②smoke:`./.venv/Scripts/python.exe -m edaloop.cli eval --subset w3-loop --tier smoke > runs/p5-0-smoke-fresh5.log 2>&1`(state=runs/w3-loop-state-smoke.json 已随仓同步;req-08 skip(done),req-01/req-11 重跑)③daily(无旧 state,直接跑)④rest ⑤全绿→§13 加 v0.6.10 行+本节补执行记录→closeout 提交(排除 easyeda-agent//run//samples//.easyeda/;**docs/runs 不进主仓**)。

#### 5.4.1 P5-0 续作与 G33 批中途插入(2026-08-24,本机)

P5-0 回归续跑中插入 G33 修复批(详 §13 v0.6.10),如实记录:

- **插入动因**:daily 被 G33 卡死——req-01 连续 HALT、req-11 34 页/单 req 60-70 分钟,回归在时间上不可行;三路探察定因果链(titleblock `--data` 写毁 sheet 引用=图框灭→group-arrange 拒动+钳移静默死;place 通道零量测恒 400×250 保守格→1 件/页),见差距表 G33 行。
- **G33 批交付**:F1 titleblock 只读化/F2 钳移回退带/F3 行-货架页流+16 类真机实测墨迹表(scripts/calibrate_place_ink.py,证据 .claude/measure-place-ink.json)/F4 overlap evidence 补位号;pytest 248 全绿(基线 239+新增 9,保真清单零回退);真机 req-11 **34 页→7 页、60-70min→13min PASS**、req-01 PASS。
- **回归终局 13/17**(smoke 3/3:01/08/11;daily 6/8:03/04/05/08/09/11;rest 4/6:10/12/13/14)。4 个确定性 HALT 同族定性、非 G33 回归:daily req-01(Q1×R8/R6 邻域重叠,attempts 2/3/4 均 6/8)、req-02(out-of-sheet MCUSTM32/SWDHDR/EEPROM24,REPLAN 历史病灶);rest req-06(U3×L1/C6×U3)、req-07(MCUSTM32×R8 同对复发+J2 out-of-sheet+REBIND_NET VSSA)。**共因=多引脚大件邻域拥挤**:标定墨迹用 ~9 字符网名(CALNET8)量,真实网名更长→真实翼展超估算→吃 `_WING_PAD_X=120` 垫→重叠;钳移链能修但非每轮收敛(2-3 轮内不收敛即 HALT)。下批治本=**dx 按真实网名长度估算**(网名 autoconnect 前已知:`symbol_width + max(len(net))×char_width + stub`),治标=`_WING_PAD_X` 120→180(计划既定第一步)。
- **连接器假死定性**(回归期间死亡 1 次,靠看门狗拉回):克隆上游 v1.1.1 源码研究(easyeda-agent/,gitignore 排除),三方定责=**上游主责**(webview 主线程饿死触发+register() 被静默忽略的已知 wedge ≥5min 不自愈+无 project.open+观测黑洞)/**本项目次责**(窗口粘滞钉扎、不解析 NO_CONNECTOR/STALE_WINDOW、round 间无冷却)/无责 5%(RETRY_ENV 纪律+看门狗 v2 是当日唯一有效恢复)。已提 **issue #185**(zhoushoujianwork/easyeda-agent,草稿 docs/upstream-issue-connector-wedge-reconnect.md);新复现向量=daemon 全程在场。
- **看门狗 v2 实战验证**(runs/p5-0-watchdog.sh):真实探针+分级恢复(轻=doc open 重绑前台/重=优雅杀 App→重启→180s 窗口重探+toast 求人工开工程)+resume 只跳 PASS;今日整条链靠它自动拉回。
- **止损纪律执行**:确定性 HALT×3 次即停(daily attempt 4、rest attempt 3 手动终止),不空转烧机;孤儿进程核后才重启下一链。
- **P5-0 遗留**:上述 4 HALT 归入布局质量下批(非 P5-0 环境项);R21/R22 探测结论已录差距表;P5-0 以 13/17+环境雷全消(雷1 随 G33-F1 治本、雷2 已定性+看门狗、雷3 当日未再发)记为**条件通过**,布局下批后补全量回归。

#### 5.4.2 Web UI 批中途插入(2026-08-25,用户指示;非 P5 六批范围)

用户指出 agent 无用户交互界面(上传文件/对话不便,此前只有批处理 CLI+答案文件回灌),经选型对比(Chainlit/Gradio/Open WebUI 三选一,定案 Chainlit=ADR-0012)当日落地骨架:

- **事件总线**:`AuditLog(run_dir, listener=)` 挂点 + `stage_run(audit_listener=)` 透传——audit 本就是全链路唯一事件汇(controller/pipeline 全部经此落盘),UI 只观察不侵入业务;listener 异常吞掉,UI 崩不拖垮 run。
- **分层纪律**:`ui/session.py` 纯逻辑(不 import chainlit:`runs/ui/<会话>/attachments` 目录约定+路径消毒防穿越+`format_event` 事件→用户可读行)与 `ui/app.py` Chainlit 薄适配(换前端只重写 app.py);core 零 import ui。
- **意图路由(ui/router.py,2026-08-25 用户实测反馈补)**:初版聊天路由纯字符串规则(非命令文本一律当需求),"你会做什么"直接触发落图模式确认——补分诊层:能力/用法类短问(≤48 字+关键词)走零成本快路径回能力卡;其余文本问一次 LLM 出 `{intent, reply}` JSON(结构化,能力事实进 system 提示词);失败/无 key 一律回落 requirement(宁可错跑不吞需求);带需求文件的消息不过分诊(显式信号)。
- **交互流**:欢迎页=能力介绍+常驻功能按钮(action_callback:`提需求·开始设计`→AskUserMessage 收需求经分诊进 run / `上传 datasheet 入库`→AskFileMessage / `切换落图模式`),自由发消息/上传 `.md/.txt` 亦直达同链路,斜杠命令降级为隐藏兼容(初版把命令教给用户被评审打回:聊天 UI 不该暴露 CLI 心智);默认 dry-run 不碰真机(首次 run 弹确认);run 跑 `asyncio.to_thread`,audit 事件经 `call_soon_threadsafe` 队列流式刷聊天 → 非 PASS 自动收 refine 问题逐个弹选项 → IR-v2 一键增量重跑;PASS 后 SVG 内嵌+BOM/报告文件元素。
- **入口/依赖**:`edaloop ui` 子命令(subprocess 起 chainlit;缺依赖提示 `uv sync --extra ui`);`ui=["chainlit>=2,<3"]` 走 optional-dependencies——核心依赖零增量;.gitignore 补 `.chainlit/`/`.files/`/`chainlit.md`。
- **验证**:tests/test_ui_session.py +13(listener 收事件/崩溃不拖垮/事件翻译/目录消毒),pytest 268 全绿;服务级冒烟 `edaloop ui --headless` 启动干净 HTTP 200。**首次真机使用即踩 API 契约漂移**(`cl.Action` 必填 name+payload,`id=` 形状被 pydantic 拒收)——同批修完并排查同族地雷:本版无 `Message.stream()` 改 `send()+update()`、`AskFileMessage.max_size_mb` 默认 2MB 装不下 datasheet 提到 40、`Ask*` 默认超时 60/90s 统一 900s、选择结果从 `res["name"]` 读;补 Action 离线构造冒烟(Message/Ask* 构造需活跃会话上下文,离线不可造)。
- **遗留**:①浏览器端带 LLM 全流程人工冒烟未做(需 key+种子库,留作用户首验);②refine 问题 UI 侧 cap 8 个,超出仍走 CLI;③改动未提交(与 P5-0 WIP 同树,提交时机用户定)。

#### 5.4.3 req-07 目检七缺陷批(2026-08-31;代码+单测全绿,真机重跑 #1 完成见 §5.4.4)

用户对 freeze=pack 首轮交付(run-8d6416cb6322,P1-P7)逐页目检点名七缺陷,归因五类根因,全部代码级修复+单测(338 绿)。真机复验曾被 GLM 429「7 日用量上限」阻塞——用户把 .env 切到 Anthropic 协议端点后解禁(适配与新发现详 §5.4.4)。顺手补:round-plan 审计现在落全量 plan JSON(下次断供可按原计划不调 LLM 复跑)。验收命令:`EDALOOP_LAYOUT_FREEZE=pack uv run edaloop run evals/requirements/req-07-motor-driver-board.md` 再逐页 `sch clusters --strict --doc <页>`。

- **D1 页映射错位(装箱从 P3 起/P1 空转)**:freeze 分支 audit placements/tgt_pages/inst_page 三处 `P{p+2}`,生产路径是 `P{p+1}`——试放墨迹逐块量测后已清场,P1 完全可承载生产内容,却整体让位空页。修=三处改 `P{p+1}`;KEEP_P1 试放标注层与收尾清层均以"P1 是否在交付页集"判(P1 交付时清层=毁交付物)。
- **D2 全页空白率 8-58%(gap 双重计费)**:volume 口径化后 netport 文字/netflag/桩线已计入各自块 cell,piece/shelf 双 200 间隙里只剩空画布——200 是 body 口径时代防"文字翼归属歧义"的垫,对 volume 是双重计费。修=packer `_PIECE_GAP=60`、`_shelf_gap=clamp(60,120,行高×25%)`(矮行 60/高行 120);真机若再现归属歧义(req-07 模式:layout-lint 过、clusters fail、组 box 互膨胀)回弹这两个常数即可。
- **D3/D5 标记叠器件(RC1,标记墨迹从不进质量关)**:`_connect_stub` 只避带边+自件同列脚,「方向=离带边最远」把标记甩进本体(P3 reverse_VIN 正中 REVERSER1、P5 U3 脚4 GND 旗压 U3、J2.A6 文字压 R8);rail 网不进紧凑化,GND 旗落哪算哪。修=①`_connect_stub(body_rects=)`:标记墨迹矩形(锚↔翼端外包络 ±14)不得压任何本体框(>2 计面积),桩线段查本体(框内缩 3 防脚在渲染边内误杀),全候选压体时取压叠面积最小者兜底;**own_body 只进墨迹检查不进桩线检查**(脚长在本体边上,桩穿自件几何强制,按桩线查自件=全候选覆没退 planner);offsets 有避让集时扩 120/150。②`_reseat_escape_marks` 扩触发:原只查"锚出带",加 ②墨迹压本体 ±2 ③墨迹压他件标记(P5 J2.B1A12 旗与 C13_N7 文字叠、REVERSER1 双 reverse_VIN 互叠);配对删共轴要求(拉移/clamp 斜甩后脚-marker 不共轴,共轴判据把出纸标记永久跳过——P4 LED6 翼甩 y=-116);审计补 stale mx,my bug(todo 元组带坐标)。
- **RC2 closeout 后重叠复探(P4 LED3/LED6 本体全叠+pin2 同点隐短、P6 缝隙叠、LED10 顶沿 876>813 出纸)**:收口序 rotate→reseat→closeout→compact,最后一程 compact 的 `_pull_long_pairs` 还在动器件,closeout 探针看不见末端几何。修=新增 `_overlap_reprobe(page, round_no, members, oversize)`:clusters ERROR→**本体口径**复核(翼碰翼交 reseat,本体相交才分离)→选动小件(npins/area/desig 序)→8 方向×40..320 找空位(避全部本体余 15、不出 [12,12,1158,813]、位移最小)→group-move(拒则 modify 回退,restub 内置)→自画线 bbox 记账平移→移后回读丢网脚重落→一次一动重探,上限 6;生产与 freeze 两收口点各挂一次;oversize 页跳 out-of-sheet(块高出带是装箱定案)。`_find_slot` 出图检查四边全查(原只查下/左,漏上沿),试放虚空件(x≥1500)只保地板判据。
- **D7 P7 线大量跨器件本体(RC4)**:compact 走线障碍原豁免"网内成员块"整个块——多块网的长边绕行时他端块本体仍是障碍物却被豁免。修=逐边计算障碍 `fb=[r for d,r in bodies.items() if d not in (a[0],b[0])]`(只豁免本边两端件;同块内导线跨自家件图形是常规画法)。
- **D4 m2_led2 框异常大 / D6 led_pwr 块内空白**:框大=volume 口径正常化(文字翼计入 cell,框必须罩住墨迹),连线问题由 RC4/RC2 覆盖;块内空白由 RC2 拉近修复后复验。

#### 5.4.4 Anthropic 协议端点适配 + 重跑 #1 验收 + 拉移并轨修复批(2026-08-31)

**通道切换**:.env 换 `EDALOOP_LLM_BASE=https://open.bigmodel.cn/api/anthropic`(GLM coding 套餐的 Anthropic 协议端点,Claude Code 同款)。新增 `AnthropicCompatChat`(tests/test_llm_anthropic.py 4 测):①system 消息拎顶层 `system` 参数;②`max_tokens=65536` 必填——该端点 thinking 强制开且 `disabled/budget_tokens` 均被静默忽略(实测 200 仍带 thinking 块),思考与正文共享预算,8192 被 make_plan 长提示的思考吃光(stop=max_tokens 零 text);③必须 SSE 流式——非流式的 read timeout 是"首字节前"单窗口,65536 预算的长思考生成 10-30min 必超时(实测 25min 反复 ReadTimeout 空转);流式读超时按块间隔计,永不触发;④content 块数组只拼 text_delta,无 text 增量按可重试错处理。`get_llm()` 按 base 含 `/anthropic` 路由,调用方无感。

**树杀修复**(run-955eb4729cff 定性):`subprocess.run(timeout=)` 只 TerminateProcess 直子进程,孙进程继承管道句柄时 communicate() 永等不到 EOF——run 在 designator-rename 后冻死 38min 零审计(py-spy 栈停 `_communicate join`)。`_subprocess_run` 改 Popen+communicate(600s),超时 `taskkill /PID /T /F` 树杀(win32)后抛 AdapterError 走既有失败/重试路径(连接器 wedge 项目侧次责项之一)。**教训**:Windows 下一切带 timeout 的子进程调用,超时路径必须树杀;py-spy dump 是冻死定性的第一步,先抓栈再下结论(本次曾误判"正常流式中"提前杀任务,时间线用 py-spy+文件 mtime 对账纠正)。

**重跑 #1(run-5a2ddef8a563)七缺陷对照**:status=FREEZE(正常终态,CLI 退出码 1 仅 PASS=0)。①页映射:P1 起排 ✅(4 页 P1-P4,旧 7 页);②空白率:waste 0.36-0.75(旧 0.42-0.92),4 页分页判据正常 ✅;③-⑦ clusters 层复核:5 处 ERROR 逐对本体 bbox **全部不相交**(P1 C3↔C2、P2 HDRM3↔ULNM4+ULNM3↔U2、P3 R10↔CVDDA1、P4 R2↔J2),纯翼擦边零隐短(上轮 LED3↔LED6 本体全叠+pin 同点)✅;overlap-reprobe 真机出手(P2 LED1、P3 RNRST1)✅。

**新缺陷(本批 P0 net 存在性终检首战告捷)**:net-presence 报 VIN(P1/P2)、M1_IN_D(P2)、M2_IN_C(P2/P3)三网零载体。引脚级定性(run `sch read` 逐脚核):**不是断线是并轨**——PMOSREV1:3(应 VIN)=5V、ULNM3:4(应 M1_IN_D)=GND、MCU1:21+ULNM4:3(应 M2_IN_C)=3V3、ULN COM(应 VIN)=5V、R3/LED4 单脚悬空;buck 输入 U1:1 只剩输入电容孤立供电,J1:1 错挂 SW 节点。根因链:freeze 收口序 replay→autoconnect→reseat(标记全对)→**arrange group-move(ULNM3/ULNM4/MCU1,已知不保网)**→compact→overlap-reprobe→reseat(补回部分)——引脚挂**错网**而非无网,reseat 只认「脚无网」=检测盲区;错网脚有网,页级 net-presence 才看得到。修=新增 `_repair_missing_nets`(freeze 终检的修复通道):缺网页上凡 autoconnect 规划绑定该网的脚(renamed_r 同款换名翻译),实测网≠规划网 → `sch disconnect` 删错网残桩(平台真行为:清 net+删端点导线+netport)→ `_restub_net_pins` 按计划网重落;上游块 ports_binding 模板内部网(如 buck 的 C1_N*)不在动作流,记 unverified 交目检;修后复检,余缺仍只审计不阻断(目检裁决)。审计事件 `net-repair`(repaired/unverified)。**遗留**:J1:1 挂 SW 节点属上游模板拓扑问题(upstream block 内部网命名),修复通道覆盖不到,交上游块库 issue 候选。

**重跑 #2(run-7db9b9f61430,带修复通道)验收结论**:status=FREEZE,5 页 30 块。**电气维度全绿**:net-presence 零事件(对比 #1 三网零载体);引脚级复核控制链全通(MCU2:10-17=STEP1/2_IN_A..D,本页 ULN3+跨页 ULN4 贯通,VIN→VIN_PROT 三页贯通);零错网(信号脚无一挂轨);悬空脚全为合法未用(ULN 空通道/MCU 空 GPIO/USB 空脚)。并轨未再现,修复通道未触发(保险在位)。**布局维度**:零本体相交(12 处 clusters ERROR 逐对本体检=全部翼擦:10 overlap+ULN4 簇悬垂线 y=-78+全页出带元素 0)——但翼擦 12 处比 #1 的 5 处多,根因=reseat 盲退 fallback 标记无几何质量关(#1/#2 fallback 规模 94/110 相当,慢性病非回归,本次 5 页更密显形更多;reprobe 在末轮 reseat 之前跑,盲落标记在探针后落纸)。**下一批候选**:①fallback 标记几何关(planner 盲落后按墨迹避体重落,或 _connect_stub 候选耗尽时扩避让档)②收口序再补末轮 reseat 后的 reprobe(「顺序即盲区」二次实证)。

#### 5.4.5 产品梳理批(2026-09-01,用户批准;发版卫生提前+布局治本+P5-1 启动+目检审计协议)

产品视角复盘定调(四病):**布局是无底洞缺出口判据**(连续 4 插入批全扑布局,翼擦 5→12 反复,无"到哪算完"的判据)/公开仓饿死(v0.6.11~13 三批代码滞留工作树,README 钉扎还写 0.25.1)/**QA 发现倒挂**(req-07 七缺陷全用户目检发现而门禁全绿;空白率 8-58% 无机械指标)/数据飞轮零圈(G28 自 P4-6 拖延至今)。本批五件事,§10 三条纪律(布局收口判据/插入批代价核算/Phase 5 墙钟 2026-09-15)随本批先行生效:

1. **发版卫生提前批**(P5-5 部分前置):v0.6.11~13 三批代码以合并提交入公开仓;钉扎口径三处归一 1.2.10(README 0.25.1→1.2.10,pyproject [tool.edaloop] 1.1.1→1.2.10,adapter 1.2.10=权威);pyproject 包版本 0.2.0→0.7.0;tag v0.7.0(push 需用户明示)。
2. **布局治本批**(§5.4.4 遗留三件,时间盒内做完即收,残余翼擦按 §10 记账):①dx 按真实网名长度估算(替 `_WING_PAD_X=120` 常数垫,网名 autoconnect 前已知)②fallback 标记几何关(_connect_stub 全候选压体时取压叠面积最小者+reseat 盲退同口径)③末轮 reseat 后补挂 reprobe。验收=单测+真机 req-07 复验(硬门禁:本体交 0+电气绿;软:翼擦计数不劣化即收)。
3. **P5-1 生产数据闭环启动**(与 2 并行,不因软性布局缺陷阻塞):evals 10 PDF 真机 ingest 入生产库→JOIN 回填量化→elec-deny 探针→w1 回归 ≥92%(§5.1 Go 判据照旧)。
4. **全量目检审计协议**:14 金标需求 freeze=pack 单遍系统目检+缺陷分类学+机械化清单(真机长跑,协议+命令交付用户执行;Web UI 首次真实 dogfood)。机械化目标是把目检发现转成 checks.py 的可机械判据(对齐 R18 教训:评分高≠工程可读)。
5. **本批自身遵守插入批代价核算**(§10 新规):本批动因=产品复盘四病;挤占=P5-1 约一周顺延但同期并行启动、P5-2/3 顺延;恢复路径=墙钟 09-15 前按 §5.1 顺序续跑,发版卫生与布局治本均为 P5-5 与布局下批的前置工作,非额外扩量。

(执行结果回填,2026-09-01):

1. **发版卫生**✅:eb222c1(v0.6.11~13 三批工作树代码+版本三处归一 1.2.10+README 刷新+gitignore 收口)+ v0.7.0 本地 tag(push 待用户明示)。
2. **布局治本**✅(代码侧,353 测绿,commit 14c845c):三件全落地——①dx 真实网名估算核验(v0.6.12 已实现 `_wing_extra`,本批核验无缺);②盲退几何关 `_guarded_autoconnect`(盲落前后双量测,坏标记=出带/压体/压他标→disconnect+`_connect_stub` 重落,重落失败如实盲退审计 unguarded,「宁翼擦不隐短」);③扩档 270/330(body_rects 相位)+末轮 reseat 后 post-reseat3 终态复探(freeze/生产双尾)。**真机复验搭载目检审计批**(visual-audit-p5.md §8),不单独跑。
3. **P5-1 生产数据闭环**:**真数据贯通(5/10 入库,批量 Go 判据未达但闭环活性已证)**。首跑 10 PDF **0 入库**暴露生产批三连缺陷(cd3d724):GBK 控制台 UnicodeEncodeError 中断批(1/10 即崩)、`>=4` 采纳阈值把 3 脚件逼向多封装合并(AMS1117 12 脚 pin1/2/3 重复→fail 不入库)、提取 prompt 无多封装规则;第二批再曝三缺陷(bacf474):**JOIN 语义错配**(库 ref=AMS1117-3.3/CH340K 带 die 变体后缀,datasheet 部件名是裸名,精确等值 JOIN 零命中→前缀回退)、**供电行子串误配**(`"vin" in param` 命中脚注「(VIN−VOUT)≤12V」行,把 1.21V 基准电压回填成供电范围→startswith 收紧)、PC817「internal connection」页标记缺失+单份异常弃批。终局:**入库 5/10**(AMS1117 3脚/MT3608/PC817/TP4056/ULN2003A 16脚 pass),elec 行 105;**拒收 5**(esp32-wroom×规则通道错配 41脚/esp32-s3×子表漏提 17脚/stm32×族表合并 100脚/CH340C×封装变体混并/MAX485×族名+规则噪声——同根:族级/多封装/多表页超出单表提取模型,诚实 fail 记 backlog);**回填 3 块/6 字段**(TP4056 4-8V/MT3608 2-24V/ULN2003A 0-50V,全对 PDF 抽检);**elec-deny 真数据探针**:24V 直入 TP4056(v_max=8)DENY✓、MT3608(24V 边界)放行✓、ULN2003A(50V)放行✓;**w1 回归 recall@8=70/74=94.6%(Go≥92 ✓,负样本断言 24V ldo 出局/5V 不误伤全过;首轮跑至 req-10 后被硅基流动瞬时 400 打断,重跑全量通过——瞬时 5xx/4xx 嵌入毛刺的自动重试是 backlog)**。
4. **目检审计协议**✅交付:docs/visual-audit-p5.md(14 金标 freeze=pack 单遍跑序+逐页 12 项清单 C1-C12+缺陷分类学→机械化映射表——翼擦/压体/出带/图签 4 类候选判据进 p5-2 按频次立项,框外墨迹/并轨形态/文字重叠 3 类留人工;3 条全跑交付抽检+Web UI dogfood 搭载;硬门禁项 C1/C9 出现即 No-Go 先修门禁)。
   - **执行进展(2026-09-01)**:req-08 首跑 5 缺陷(2×C3 盲退标记压自体/2×C10 freeform 分页/1×**C9 P0**);C9=同 pin 同网重复标记终态(FS8205A pin2/pin5 各两枚 FET_MID,全 failed:fallback 而 gate=pass——net-presence 只查网存在不查重复,§10 No-Go 触发)→ **修复批先行(d040c49,v0.6.16)**:盲退链三处幂等清理(`_stale_ids` 最近同网脚归属,双管 FET 相邻同网脚拆得开/共享旗平距不误删)+终态去重门禁 `_dedupe_pin_markers`(freeze 画框前+生产尾轮 reseat 后,同 pin 同网 ≥2 载体→留优删余,删不动计 wire_breaks);358 测绿(+3 锁行为,fake 补 prim-delete 真删+disconnect 拆不净夹具)。后续 13 金标搭载 d040c49 续跑,req-08 复跑闭环。
   - **执行进展(2026-09-02,req-08 复跑判读+d040c49 验证)**:C9 修复**验证生效**(P2 FET_MID 每 pin 单枚、side 正确);复跑再曝两缺陷(用户目检):**①标记侧位慢性病**(PROTDW01:2 CSI 终态钉 DW01A 右侧、:5 VDD_S 压本体——用户规范「引脚在哪侧,网标记就放哪侧」)→ 根因链四层:引线型符号 bbox 含引线(DW01A bbox 40..275、本体核 145..225)「贴边 ≤20」侧位判定全员失效;avoid 含自件框把同侧候选全判压叠;reseat 判据②收走救援档落位(三趟原地打转);同侧扫尾中心分半口径漏「脚与中心之间」压体锚 → **四层同修**: `_pin_side` 脚端点列/行聚类侧位判定(聚类含目标脚:DW01A 中排脚夹在两簇间隙)+`_connect_stub` 同侧救援档(压叠只在自件框=引线、方向在本侧/顺边、他件全净→先于全局最小压叠)+reseat 判据② own-body 豁免(`_mark_beyond_pin` 越过脚端点朝外=至多擦引线)+同侧扫尾改引脚相对口径(`_mark_toward_body`)。**②place-only 计划摊 3 页**(5 个 SOT23 小件、每页空白率 >80%)→ 根因=freeform/标准件计划无 block-apply,`_repack_actions` 旧门槛 no-upstream-blocks 整个拦在装箱外退 compile 流式初值 → 放行 place 通道(两通道都空才回退),freeform decompose 补 module=模式 id(装箱亲和同页);362 测绿(+4:聚类侧位/引脚相对扫尾/own-body 豁免/place-only 单页)。**req-08 三跑验收重点:页数=1、DW01A 左脚标记全在左侧、mark-side-guard 事件零 fixed**。
5. **插入批核算**:本批自身已记 §13 v0.6.15 行三件(动因/挤占/恢复);P5-2/3 顺延至目检审计与 P5-1 收口之后。

## 10. 验收与工程纪律

- **每个 PR 必跑**:pytest + evals 子集(金标准不回退);
- **每轮迭代状态可复放**:审计日志记录(输入IR/检索命中/BlockPlan/findings/修复动作),支持 `edaloop replay`;
- **门禁零豁免**:任何"先跳过校验"的代码路径禁止合入 main;
- **依赖钉死**:easyeda-agent 版本升级=独立 PR+全量 evals 回归;**版本门覆盖所有真机变更路径**(run 主链与 apply 同门,P5-0 前移修复);**升级批的 eval 必须清 w3-loop state 真跑**——resume state 的 skip(done) 会把旧 PASS 记录当新回归放行(P5-0 实证);
- **布局收口判据(2026-09-01 定案,布局批 Go/No-Go 依据)**:**硬门禁(阻断交付)=本体口径相交 0**(`sch clusters --strict` 逐页 ERROR 逐对以本体 bbox 复核为零)+ 引脚同点/错网 0(电气维度 net-presence+connectivity+电压兼容全绿);**软指标(记账不阻断)=翼擦**(纯墨迹级重叠:零本体交+零隐短)**记数进 backlog 不追修**、页空白率不劣于上一基线、页数≤行-货架页流合理密度、出带元素 0;布局批时间盒 1 周,到线按现状收口,残余翼擦如实转 v1.0.x;
- **插入批代价核算(2026-09-01 增补)**:任何计划外插入批(用户指示/中途发现)的 §13 变更行必须记三件:**插入动因、挤占/顺延的原计划批、恢复路径**;连续插入 ≥2 批时须重排 Phase 余下里程碑并更新 §5.1 排序,防止计划批被无限顺延;
- **Phase 5 墙钟(2026-09-01 定案)**:**2026-09-15 为 v1.0 发布线**,到线按现状执行 P5-5 收口(全层 eval+pytest+真机 smoke+tag v1.0.0),未完项如实转 v1.0.x 里程碑,不整体延期;
- 每完成一个里程碑:更新本文档对应状态+变更记录+经验教训。

## 11. 风险登记册(评审时逐条过)

| # | 风险 | 等级 | 触发信号 | 缓解 | 兜底 |
|---|---|---|---|---|---|
| R1 | RAG 冷启动,检索增益低 | 高 | W1 指标<80% | 种子扩容(oshwlab 开源工程复刻 50+);检索不中退化为块库直查 | 人工策展冲刺 |
| R2 | 迭代不收敛/震荡 | 高 | W3 同错≥2轮占比高 | 归因定向反馈;震荡检测→升级人工 | critic agent(AaLLM) |
| R3 | easyeda-agent 上游破坏性变更 | 中 | 版本升级回归失败 | 版本钉死;只依赖稳定 CLI 面 | fork 其 skill 层 |
| R4 | 嘉立创官方下场整合 agent | 中 | 官方市场出现同类 | 差异化在 RAG+闭环+开源;争取被官方生态收录而非对抗 | 转型上游知识库/评测层 |
| R5 | 多模态 datasheet 提取精度不足 | 中 | W4 引脚 diff<95% | 双通道+三方 diff 已设计;限文字型 PDF | 降级人工确认入库 |
| R6 | LLM 幻觉器件/引脚 | 中 | pin diff 拦截量 | ground truth 硬约束(原则4) | — |
| R7 | 平台墙(无undo/增量import失效等) | 中 | 落图部分失败 | 快照+反向操作回滚;沿用 easyeda-agent 趟明的 workaround | 整轮重画(幂等落图) |
| R8 | 窗口期收窄(Flux 中国区/国产大厂下沉) | 中 | 竞品发布 | 加速开源社区建设先占位 | — |
| R9 | 单人开发带宽 | 高 | 里程碑连续延期 | 严格 PoC 范围;非目标清单挡需求 | 砍 W4 保 W3 |
| R10 | SPICE 覆盖不了目标电路类别 | 低 | 弱门禁误报 | 仿真仅加分项,强门禁不依赖 | — |
| R11 | PoC 期 LLM/embedding 走云端,数据出域 | 低 | 涉密/企业数据场景试用 | PoC 数据仅公开 datasheet;接口层抽象隔离 | Phase 1 本地 BGE-M3 权重 + 本地 LLM 端点 |
| R12 | 下单资金安全(误下单/错规格) | 高 | P3-6 真机试单 | 默认止步报价;--confirm 显式双确认;预检存档 | 只做「报价+跳转」,订单草稿也人工触发 |
| R13 | JLC 下单 API 无公开契约,随时变更 | 中 | P3-6 接口联调 | M9 内部 provider 抽象隔离 | 降级为导出制造文件+人工下单指引 |
| R14 | 上游 PCB 能力边界(稠密板布通率) | 中 | P3-5 板子超过 esp32-mini 复杂度 | 档位策略照抄上游(稀疏才全自动);迭代环沿用 M5 | PCB 半成品交付+人工修板指引 |
| R15 | 全链愿景范围蔓延 vs 单人带宽 | 高 | Phase 3 批次连续延期 | 七批独立 Go/No-Go;挂起机制保主干(P3-5/6) | 砍中段增强(P3-2/3/4)保两端闭环 |
| R16 | 块库电气数据错误→强门禁误杀正确设计 | 高 | P4-3 金标准出现误伤 | 字段×类目矩阵+≥20% 抽检(一处错=No-Go);数据缺失三态降级(UNKNOWN-warn 不静默);quote/provenance 出处 | 强门禁仅对已回填类目生效,其余弱告警 |
| R17 | 上游文档超前二进制(0.25.1 期:destagger/zone-arrange 不在二进制;zones status 存在;zone-plan 校验 5 项;zone-violation 判据 0.25.1 仍产出但新版文档称 retired)。**P5-0 探测更新(2026-08-22,1.1.1 实测)**:代码使用面 20 个 sch+18 个 pcb+lib search/by-lcsc+blocks ls/search/show 全部在位;原超前命令 destagger/extract-layout/zone-arrange 在 1.1.1 已落地可用(zone-violation 反向除名,2026-08-23 --help 复测不在);四件套同版(CLI=daemon=connector=1.1.1) | 中(已降:命令面全在) | 后续版本升级时命令 fallthrough 到父 help | 以当前钉扎版 --help+二进制 grep 为真源(现为 1.1.1);adapter 命令存在性探测(surface 门)模式保留;文档矛盾记入探测清单 | Go 指标不依赖超前命令;版本升级走 ADR-0002 独立批+全量回归(P5-0 即该批) |
| R18 | 布局质量指标机械化的代表性不足(评分高≠工程可读) | 中 | P4-1 达标但人工评审差 | 指标锚定上游 layout-lint/layout-score/zone-plan 机械输出+人工抽检 ≥20% 升为 Go 必要条件 | 人工评审作弱门禁补充(questions 队列),不阻断交付 |
| R19 | 分区/紧凑化/strict/多页引入新抖动源,回归回退 | 中 | P4-1/P4-2 轮数上涨 | 三类变更(分区重排/几何收紧/strict)分批独立 Go(P4-1/P4-2 拆批);WARN 分布统计前置;多页按块数阈值分层开关 | 简单需求保持单页直落(特性降级而非删除) |
| R20 | 电气参数入库工作量(95 块×字段+golden 标注)挤占 F3 主干带宽 | 高 | P4-0 超两周未达 Go | P4-0 与 P4-1 并行启动;wmsc spike 首日定通道;LLM 预标+人工终审;策展分批(power/passive/负载类优先) | 强门禁范围收缩到已回填类目,P4-3 照常交付(未知即弱告警) |
| R21 | datasheet 回填改检索索引文本(电气 digest 入 FTS),w1/daily 排名回退 | 高 | P5-1 回填后 w1 <92% | seeds 不动,回填只走生产库 JOIN 且只补缺不覆写,单批可整体 revert;Go 内嵌 w1 ≥92% 不回退 | 不合入,回填通道加开关降级 |
| R22 | 上游 0.25.1→1.1.1 跨 26 minor 命令面漂移(签名/行为/已知副作用面状态未知) | 中 | P5-0 全量回归真机跑挂 | 命令存在性探测先行(R17 模式);回归零豁免;不可用命令降级/绕行并记探测清单 | 钉扎暂缓在可用版本,漂移项逐条登记 issue 候选 |
| R23 | 26 需求全集标注(13 件纯人工)挤占带宽 | 中 | P5-3 超一周未完 | 放 P5-1 后(排名稳定再标);逐件可切分提交;Go 线由基线数据定而非先验 | 标注不阻塞其他批,先按 14 需求现行集发 v1.0(如实记录覆盖口径) |

## 12. 决策记录(轻量 ADR,大事拆 adr/ 目录)

| # | 日期 | 决策 | 状态 |
|---|---|---|---|
| ADR-0001 | 2026-08-17 | 愿景冻结为 v2 五段闭环链路 | ✅ |
| ADR-0002 | 2026-08-17 | easyeda-agent 依赖钉死 v0.25.1;升级=独立 PR+全量 evals 回归 | ✅(钉扎值链:ADR-0011 修订为 1.1.1→2026-08-28 连接器平台侧自动升级跟随至 1.2.10(现值,adapter=pyproject=README 三处归一于 2026-09-01 产品梳理批);升级纪律不变;另 P5-0 补洞:版本门前移到 run 主链,此前仅 apply 查) |
| ADR-0003 | 待定 | 多模态模型选型 | ⏳ W1 |
| ADR-0004 | 2026-08-17 | LCSC 数据通道:PoC 复用 easyeda CLI(`resolve-lcsc`),留 `LcscProvider` 切换位;直连 API 备选,爬虫否决 | 📝 草案,W0 验证 |
| ADR-0005 | 2026-08-17 | 项目定名 `jlc-edaloop`;仓库 https://github.com/Yyin-Tta/jlc-edaloop.git | ✅ |
| ADR-0006 | 2026-08-17 | Embedding PoC 走硅基流动 BGE-M3(+reranker 精排);provider 抽象强制;Phase 1 本地兜底 | ✅ |
| ADR-0007 | 2026-08-17 | 文本 LLM 切换为 GLM-5.3(智谱端点);抽象纪律不变;key 兼 cover 多模态候选 | ✅ |
| ADR-0008 | 2026-08-20 | Phase 2 方向:自由拓扑受控子集→BOM 成本→sizing 子集;多页/KiCad 挂起(详见 adr/0008) | ✅ 已执行完(P2-A/C/B 落地) |
| ADR-0009 | 2026-08-20 | **终极链路 v3 决议**:项目从「原理图闭环」扩展为「全链编排者」——PCB(M8)消费上游 pcb 命令不自研、下单(M9)止步订单草稿+人工确认硬门禁;§1 愿景 v3/§3 非目标修订/§5 架构扩展/§9 Phase 3 七批计划同步生效 | ✅ |
| ADR-0010 | 2026-08-21 | **Phase 4 决议(质量深耕)**:四重点 F1 需求识别/F2 知识检索/F3 布局质量(最高优先)/F4 电气正确性;布局=编排上游 zone/多页/layout-score 命令面+自研分区评分策略,不自研 EDA 算法(含「为何不用 sch autolayout」选型论证);确定性选值落图边界(§3 措辞修订);电气数据三级分级(块级字段强门禁/prose 建议弱门禁/数值表可机械校验);案例回写防污染三护栏;详见 §9 Phase 4(4.2 节即决议全文) | ✅ 规划生效 |
| ADR-0011 | 2026-08-22 | **Phase 5/v1.0 决议(打磨与发布)**:范围=真数据闭环+评测全集+发布工程,不扩能力面;**ADR-0002 修订:easyeda-agent 钉扎 0.25.1→1.1.1**(双机轮换开发,另一台将同步升级,版本门保持严格单值拦跨机漂移);生产数据闭环先于评测扩容(回填改排名,先回归再标注);案例消费验收以 run 级审计为准(探针不算);多页增强/KiCad/模拟优化/模式库扩量明确非 v1.0;详见 §9 Phase 5 | ✅ 规划生效 |
| ADR-0012 | 2026-08-25 | **Web UI 选型 Chainlit 2.x**(用户指示插入,非 P5 范围):三选一对比弃 Gradio(传递依赖重/表单心智不合长跑 agent 中途问答)与 Open WebUI(产品形态不合:只认 OpenAI 兼容端点+自定义品牌条款);架构=AuditLog.listener 事件总线(观察不侵入)+ui/session.py 会话层纯逻辑+app.py 薄适配,换前端只重写适配层;依赖走 optional extra 保核心零增量(纪律同 ADR-0002);UI 默认 dry-run、真机落图须显式确认 | ✅ 骨架落地(详 §5.4.2) |


## 13. 变更记录

> **2026-09-01 拆分**:v0.1→v0.6.8 共 37 行(PoC/P1/P2/P3/P4 期)已存档 [history/changelog-v0.1-v0.6.8.md](history/changelog-v0.1-v0.6.8.md)——存档侧注记 P1/P2 期文档版本与包版本两套混用的重号(v0.2.0 与 v0.3.1~v0.3.3 各两行),以**日期+批次**为唯一键。本表自 Phase 5 规划(v0.6.9)起续记,追加纪律不变。

| 日期 | 版本 | 摘要 |
|---|---|---|
| 2026-09-02 | v0.6.18 | **终态证据链收口(本轮代码+474 测试)**:①`LayoutSnapshot` 严格终态回读与 fail-closed 门禁证据:readback failure/malformed payload/outer envelope failure 不得伪装为空页或 PASS;body/ink/electrical 三种几何口径分离,终态快照写入 audit;②designator 映射按 `block_instance` 隔离,兼容连接器实际改号并让 autoconnect/expectedPinToNet 跟随真实位号;③交付 SVG 导出前清理精确目标并校验新文件,核心交付物不完整禁止案例回写;④gate 非零退出码即使 stdout 含 nominal PASS 也降为 unverified/`GATE_UNVERIFIED`;⑤真实 EasyEDA 适配器强制严格布局审计,环境变量和显式 `strict_layout=False` 均不能关闭;⑥controller 终态边界归一化组件/marker 类型别名,避免 schema 漂移静默丢件;⑦`edaloop apply` 明确降级为低层实验路径,名义 gate PASS 不再作为工程 PASS(完整终态契约仍以 `edaloop run` 为准)。**未改变真实工程结论**:当前 `edaloop` 工程仍处于只读诊断的 layout FAIL(本体/标记/空白率/DRC 等问题),不得写成已通过;下一批先做全新工程只读取证与受控布局实验。 |
| 2026-08-22 | v0.6.9 | **Phase 5「v1.0 打磨与发布」规划生效(ADR-0011)**:①§9 新增 Phase 5:差距审计 G27-G32(基线 4370012/234 tests/97 块/w1 74 条 93-95%/生产库无 datasheets 表/上游实装 1.1.1 vs 钉扎 0.25.1)+六批里程碑 P5-0..P5-5;②**上游钉扎对齐定调 0.25.1→1.1.1**(用户双机轮换开发告知:本机 1.1.1、另一台将同步升级,版本门保持严格单值——拦未升级机器是设计行为);③排序决议:版本门先行(真机阻塞)→生产数据闭环(ingest→JOIN→deny 真数据端到端,P4-6 各通道只有单测/探针级验证)→评测扩容(回填改索引文本必须先回归再标注)→发布收口(v1.0.0 tag+发版 checklist 文档化);④R21-R23 入册(回填改排名/26 minor 命令面漂移/标注工作量);⑤明确非 v1.0:多页增强/KiCad/模拟优化/模式库扩量 |
| 2026-08-24 | v0.6.10 | **P5-0 上游钉扎批(条件通过,详 §5.4/§5.4.1)+ G33 布局修复批(中途插入)**:①**P5-0 交付**=钉扎 1.1.1(adapter+pyproject)+版本门前移 run() 真机首轮前+R17 探测回写+ADR-0002 修订+evals resume 修复(**只跳 PASS,HALT/ERROR 重跑**——旧代码把环境崩溃遗留 HALT 行当已完成,smoke 曾被掏空 2/3 不自知)+tvs C143135→C1979411 真机验证+页修剪首删;②**G33(差距表)**:根因三连=titleblock `--data` 写毁 sheet 引用(0.26.0 明令禁令)→图框灭→group-arrange 拒动+钳移静默死→2 轮同码 HALT;place 通道零量测恒 400×250+单列页流→1 件/页(req-11 37 块→34 页/60-70min)。**修**=F1 titleblock 只读化(仅 --show+审计)/F2 几何缺失钳移回退带 (100,300,1100,780)+审计(不再静默)/F3 行-货架页流+16 类真机实测墨迹表 `_PLACE_INK`(scripts/calibrate_place_ink.py,netport 翼展带 autoconnect 实测;宽块独行=旧单列逐坐标保真,`_FLOW_W=400` 单开关回退)/F4 overlap evidence 补 a=/b= 位号;req-11 **34 页→7 页、60-70min→13min PASS**;③**回归 13/17**(smoke 3/3、daily 6/8、rest 4/6;pytest 248 全绿),4 个确定性 HALT 同族定性=多引脚大件邻域拥挤(标定用 ~9 字网名,真实网名更长→翼展超估吃 120 垫)+out-of-sheet REPLAN 历史病灶(req-02/J2),**非 G33 回归**;下批治本=dx 按真实网名长度估算+`_WING_PAD_X` 120→180;④**连接器假死定性+issue #185**:克隆上游 v1.1.1 源码(easyeda-agent/,gitignore)三方定责=上游主责(webview 饿死触发+register() 静默忽略 wedge ≥5min 不自愈+无 project.open+观测黑洞)/本项目次责(粘滞钉扎/不解析 NO_CONNECTOR/round 间无冷却);已提 zhoushoujianwork/easyeda-agent#185(新复现向量:daemon 全程在场);⑤**看门狗 v2**(runs/p5-0-watchdog.sh)实战验证:真实探针+轻/重分级恢复+resume 只跳 PASS,当日整链自动拉回;**止损纪律**:确定性 HALT×3 即停不空转。**教训**:a) 上游写入禁令(0.26.0 titleblock --data)必须静态审计强制,不能靠记忆;b) 标定墨迹必须用真实长度的网名量,估算翼展不吃单侧常数垫;c) resume 语义=只跳 PASS 是评测器正确性底线;d) 长回归期间 EasyEDA 窗口须留前台不遮挡(上游 A4:后台窗口重画布计算永不完成);e) 环境死亡(deterministic×3)与代码回归的止损线要分开判定 |
| 2026-08-25 | v0.6.11 | **Web UI 批(用户指示中途插入,非 P5 六批;详 §5.4.2/ADR-0012)**:①`AuditLog(listener=)` 事件总线挂点+`stage_run(audit_listener=)` 透传,controller/pipeline 业务逻辑零改动;②ui/ 分层落地:session.py 会话层纯逻辑(`runs/ui/<会话>/attachments` 目录约定+路径消毒+format_event 事件翻译,不 import chainlit 可独立测试)+ app.py Chainlit 薄适配(dry-run 默认、真机显式确认;to_thread 跑 run+call_soon_threadsafe 队列流式刷聊天;非 PASS 弹 refine 问题→IR-v2 一键增量重跑;欢迎页能力按钮交互(action_callback 三入口:提需求/传 datasheet/切模式,斜杠命令降级为隐藏兼容——初版命令式欢迎页被用户评审打回:聊天 UI 不该暴露 CLI 心智);`.md/.txt` 上传即需求;PASS 后 SVG 内嵌+BOM/报告文件元素);③`edaloop ui` 子命令+`[project.optional-dependencies] ui=["chainlit>=2,<3"]`(核心依赖零增量,纪律同 ADR-0002)+.gitignore 补 .chainlit/.files/chainlit.md;④tests/test_ui_session.py +13 + test_ui_router.py +7(分诊快路径/LLM 路由/垃圾输出兜底),pytest 275 全绿;headless 冒烟 HTTP 200;⑤**聊天意图路由**(用户实测"你会做什么"被当需求触发落图确认后补):router.py 快路径+LLM 分诊+失败回落 requirement(详 §5.4.2)。**教训**:a) UI 事件源不必新造——audit 本就是全链路唯一事件汇,挂 listener 即得全量进度流,零侵入;b) chainlit 各小版本 API 面漂移必须逐项对源码:`run` 不认 `-p`、`Action` 必填 name+payload(选结果读 `res["name"]`)、本版无 `Message.stream()` 只有 `update()`、`AskFileMessage` 默认 max_size_mb=2——import 干净≠能跑,离线可构造对象(Action)加构造冒烟,会话内对象只能真机验;c) AskUserMessage 附带文件不进 payload(#1087),上传语义独立 AskFileMessage 更干净;d) 长任务 UI 流式=业务跑 to_thread+事件经 call_soon_threadsafe 回主循环队列+哨兵终止,同步阻塞调用绝不能占住事件循环。**遗留**:浏览器端带 LLM 全流程人工冒烟;refine 问题 UI 侧 cap 8;改动未提交(P5-0 WIP 同树) |
| 2026-08-31 | v0.6.12 | **req-07 目检七缺陷批(详 §5.4.3;代码+单测 338 全绿,真机重跑被 GLM 7 日配额 429 阻塞至 09-04)**:①D1 freeze 页映射 `P{p+2}`→`P{p+1}`(试放墨迹已清,P1 该承载交付;KEEP_P1/收尾清层按"P1∈交付页集"判);②D2 gap 双重计费修正 piece 200→60/shelf 200→clamp(60,120,行高×25%)(volume 口径下间隙只剩空画布,8-58% 空白率主诉);③D3/D5 RC1 标记避体:`_connect_stub(body_rects=,own_body=)` 墨迹矩形避全部本体(**own_body 豁免桩线检查**——脚在本体边上桩穿自件几何强制,不豁免=全候选覆没退 planner)+`_reseat_escape_marks` 扩触发②压本体③压他件标记、配对删共轴(斜甩标记也要救)、审计 stale mx,my 修正;④RC2 新增 `_overlap_reprobe` 终态复探(closeout 后 compact 还在动件,探针看不见末端几何;本体口径分离+8 向找空位+一次一动上限 6,生产/freeze 双挂点),`_find_slot` 四边钳制+虚空件地板判据;⑤D7 RC4 compact 走线障碍逐边豁免两端件(原豁免整块成员=多块网他端本体被穿);⑥round-plan 审计落全量 plan JSON(断 LLM 后可按原计划复跑——本次 429 暴露:计划不落盘,freeze-pack 审计又无原始动作事件,replay 无从重放)。**教训**:a) 收口序里"最后还在动几何的阶段"之后必须再探一次——顺序即盲区;b) 避让检查要分墨迹/桩线两口径:标记压本体是缺陷,桩跨自件图形是常规画法,混用要么误杀要么漏放;c) 审计字段要在"断供复跑"场景下自检:只记摘要不记全量产物的字段,配额一断就是单点;d) gap 常数与 cell 口径强耦合,口径变(2026-08-30 volume 化)而常数不动=双重计费静默吃页。**遗留**:真机 freeze=pack 重跑+逐页 clusters --strict 验收(等 2026-09-04 配额解禁);D2 若真机再现归属歧义回弹 gap 常数 |
| 2026-08-31 | v0.6.13 | **Anthropic 端点适配+树杀+拉移并轨修复批(详 §5.4.4;单测 344 绿)**:①`AnthropicCompatChat`(system 拎顶层/max_tokens 65536 共享思考预算/SSE 流式必选/只拼 text_delta),`get_llm()` 按 base 路由;②`_subprocess_run` Popen+taskkill /T /F 树杀(subprocess.run 超时只杀直子进程,孙进程持有管道句柄=communicate 永挂,run-955eb4729cff 冻死 38min);③重跑 #1(run-5a2ddef8a563)七缺陷全对照过(P1 起排/4 页/waste 降/clusters 5 ERROR 全翼擦零本体交/overlap-reprobe 真机出手);④新缺陷「拉移并轨」:net-presence 报 VIN/M1_IN_D/M2_IN_C 零载体,引脚级定性=挂**错网**(PMOSREV1:3=5V、ULNM3:4=GND、MCU1:21=3V3)非无网,reseat「脚无网」判据的盲区——新增 `_repair_missing_nets`(autoconnect 规划绑定为权威,disconnect 删错桩+按计划网重落+复检,审计 net-repair;上游模板网记 unverified)。**教训**:a) "有网"≠"对网",网名保真是比连接存在性更强的不变量,检测与修复都要对网名;b) 检测器上线第一战就该信它——net-presence 首跑就抓到目检没看出的电气级缺陷 |
| 2026-09-01 | v0.6.14 | **文档重组批(纯文档,零代码变更)**:①Phase 0-4(已收官)执行细节原样归档 `docs/history/`(phase-0-poc / phase-1 / phase-2 / phase-3 / phase-4 / changelog-v0.1-v0.6.8,零改写,行号锚点校验后机械切分),§9 只留各阶段收官快照+仍生效决议(ADR-0010 六项保留原文);Phase 5 全量记录不动;②头部元数据刷新:v0.6.9/2026-08-22 → v0.6.14/2026-09-01,状态栏由「当前断点=P5-0」刷新至 req-07 布局收敛续作(含 09-01 两 run FREEZE 未回写如实现状);③§13 拆分:本表自 v0.6.9 起续记,v0.1→v0.6.8 共 37 行存档并注记版本号重号;④维护纪律不变:影响架构/接口/里程碑的决策先改本文档,实质更新文末追加 |
| 2026-09-01 | v0.6.15 | **产品梳理批(§5.4.5,用户批准五件全执行;主仓 eb222c1+14c845c+cd3d724,354 测绿)**:①§10 三纪律生效(布局收口判据/插入批代价核算/Phase 5 墙钟 09-15);②发版卫生:三批滞留代码入公开仓+钉扎三处归一 1.2.10+pyproject 0.7.0+README 刷新+tag v0.7.0(本地);③布局治本三件(盲退几何关/扩档 270/330/post-reseat3 终态复探),真机复验搭载目检审计批;④P5-1 ingest 生产批三连修(GBK 崩溃/3 脚件阈值/多封装 prompt)+ 10 PDF 真机入库+回填/deny/w1 量化(数字详 §5.4.5 回填);⑤目检审计协议交付 docs/visual-audit-p5.md(12 项清单+分类学→机械化映射,QA 倒挂的治本入口)。**插入批核算(§10 新规首例)**:动因=产品复盘四病(布局无底洞/公开仓饿死/QA 倒挂/数据飞轮零圈);挤占=P5-2/3 顺延、P5-1 与本批并行未断;恢复=墙钟 09-15 前按 §5.1 序续跑。**教训**:a) 生产批第一次真跑就暴露三个纯 Windows/纯小器件缺陷——离线 353 测绿与"能在生产环境转起来"是两回事,数据闭环的价值第一步就是逼出这些;b) 采纳阈值这种"过滤垃圾"的防御性常数,会在真实长尾(3 脚件)上反向诱导数据污染(合并多封装),防御判据必须写明它排除的是什么 |
| 2026-09-01 | v0.6.16 | **目检审计批 C9 修复(d040c49;req-08 首跑触发 §10 No-Go 先修门禁;358 测绿)**:①**C9=同 pin 同网重复标记**(run-746b24879342 终态:FS8205A pin2 两枚 FET_MID 110,300+160,300、pin5 两枚 250,300+260,300,全 failed:fallback 而 round-validate gate=pass)——根因=reseat/盲退多轮拆-落循环里 disconnect 只拆「端点在本脚的导线+netport」,盲落标记的线不总落回脚端点,拆不尽的旧标记留页上、重落再添新枚,载体只增不减;net-presence 只查「网存在」不查「重复」=门禁盲区实锤。②**双治**:盲退链三处幂等清理(`_guarded_autoconnect` 首落/确定性重试/二次盲退前,`_stale_ids` 按「最近同网脚归属」prim-delete 本脚残枚——严格更近、平距不动:双管 FET 两脚同网相邻 140 拆得开,共享旗两脚正中不误删;误删由 net-presence+修复通道兜底)+终态去重门禁 `_dedupe_pin_markers`(freeze 画框前+生产 `_apply` 尾轮 reseat 后全页扫,同 pin 同网 ≥2 载体→留带内>不压体>离脚最近一枚删余枚,删后复列核验,平台删不动/无 id → 计 wire_breaks 交阈值门/目检)。③测试 +3(归属拆分/盲退两回不累积/终态去重含删不动与共享旗不触发);fake 补 `delete_primitives` 真删覆写(父类空实现吞调用)+`disconnect_keeps_marks` 拆不净夹具(真机 C9 形态,默认关零扰动)。**教训**:a) 目检协议第一跑就抓到 P0 且给出精确坐标——「无对应 audit 事件=门禁盲区」的登记口径有效;b) fake 的「全删」disconnect 语义比真机干净,掩盖了拆不净累积缺陷(同 2026-08-25 #5 fake-reality 分歧族),真机缺陷形态必须以夹具开关进 fake;c) 修复器自身也会制造重复(盲退重落只添不减)——幂等性(落前先清)与终态核验(扫后删余)要成对出现,单靠任何一半都留缝 |
| 2026-09-02 | v0.6.17 | **目检审计批 req-08 复跑两修(用户目检定案;362 测绿)**:①**标记侧位慢性病**(run-e89808c395b8:PROTDW01:2 CSI 终态钉 DW01A 右侧、:5 VDD_S 压本体;用户规范「引脚在哪侧,网标记就放哪侧」连续三轮未愈)——根因链四层同证:a) 引线型符号 bbox 含引线(DW01A bbox 40..275、本体核仅 145..225),「贴本体边 ≤20」侧位判定对全员失效,左脚退「离带边最远」序先试右侧;b) `_connect_stub` 的 avoid 含自件框,同侧候选全判压叠永不严格,退全局最小压叠=原位压体;c) `_reseat_escape_marks` 判据②(压体)把救援落位再收走,重落→再收走,页内三趟 reseat 原地打转(审计 dir left/auto/right 振荡);d) `_fix_wrong_side_marks` 中心分半口径漏「脚与件中心之间」的压体锚(旧判据 _side(-15)=left 与左脚同侧)。**四层同修**:`_pin_side` 同件脚端点列/行聚类侧位判定(x 轴 60 间隙分簇,最左簇=left/最右簇=right,x 单簇再按 y;**聚类必须含目标脚**——DW01A 中排脚 x=155 恰在左簇尾 95 与右簇头 230 的间隙,滤掉再聚类就成了「中心脚」;判不出退贴边判定再退带边序,误判比漏判糟)+`_connect_stub` **同侧救援档**(严格档→同侧救援档(压叠只在自件框=引线、方向在本侧/顺边、他件全净)→全局最小压叠档,对侧垫底)+reseat 判据② own-body 豁免(owner 的 `_pin_side`+`_mark_beyond_pin` 判锚越过脚端点朝外=至多擦引线,自件框不算压体)+同侧扫尾改引脚相对口径(`_mark_toward_body`:锚朝本体方向偏过脚端点=压体/对侧/斜蹭统一违规,顺边垂直出线合法)。②**place-only 计划摊 3 页**(5 个 SOT23 小件、每页空白率 >80%)——freeform/标准件计划无 block-apply,`_repack_actions` 旧门槛 `no-upstream-blocks` 把它们整个拦在装箱外退 compile 流式初值(`_PageFlow` 行-货架流逐个小件摊页);放行 place 通道(两通道都空才回退 no-blocks,`missing` 同判),试放-量测-装箱-重放链路对 place 块本就完备;freeform `decompose` 补 `module=模式 id` 装箱亲和同页。③测试 +4(引线型聚类侧位含右侧封锁/引脚相对扫尾含「脚与中心之间」锚/own-body 豁免含朝体对照/place-only 两钮单页)+test_freeform module 断言;fake 夹具沿用零改动。**教训**:a) 「贴边」类几何判据隐含假设 bbox=本体,引线型符号(分立器件主流画法)不满足——侧位/朝向要从**数据**(脚端点簇位)推,不要从**框**(bbox 边距)推;b) 修复器与门禁判据必须共享同一几何口径(越过脚端点=外侧合法),否则 A 修好 B 收走,迭代空转;c) 装箱入口的通道门槛要按「可装之物」判,不按「上游块有无」判——place-only 也是一等公民 |
| 2026-09-02 | v0.6.18-doc | **L0 取证状态校准**:当前离线回归为 `uv run pytest -q` **476 passed**;包元数据、模块和 `edaloop --version` 均为 **0.7.0**。`runs/run-fb97781513ec` 只有 49 行 audit,末事件为 `mark-side-guard`,缺少终态结果和 delivery,按 `INCOMPLETE_RUN` 处理,不计入真机 PASS；L0 仍需在全新工程完成 req-08、req-07、block-only 的逐页 snapshot/audit/网表 hash 取证。 |
