# 前期调研:嘉立创 EDA 智能原理图设计 Agent(愿景 v2)

- **日期**:2026-08-17
- **状态**:前期调研(Pre-PoC)
- **愿景链路**:
  ```
  解析用户意图(需求文档 / BOM / datasheet 等多输入)
      → RAG 检索知识库(datasheet / 已验证案例 / 器件库)
      → LLM 生成原理图
      → 结果校验(机械门禁)
      → 满足交付 / 不满足 → 迭代回到生成
  ```
- **结论速览**:链路五段中每一段都已被学术研究或商业产品独立验证可行,但**没有任何系统在嘉立创 EDA 生态内把五段串成带校验闭环的开源整体**。可行性评级:整体 **高**(有商业标杆 + 学术铺路),主要不确定性在 RAG 知识库的工程构建成本与迭代收敛效率。

---

## 1. 愿景升级说明与范围

相较《research-datasheet-extraction-feasibility.md》(v1,单点 datasheet 提取),本次愿景升级为完整的设计 agent:

| 维度 | v1(已归档) | v2(本文档) |
|---|---|---|
| 输入 | datasheet PDF | 需求文档 / BOM / datasheet / 口语意图 |
| 知识来源 | datasheet 本身 | **RAG 知识库**:结构化 datasheet + 已验证案例 + 器件库 |
| 生成 | 引脚表→符号 | **LLM 生成完整原理图**(拓扑 + 选型 + 连接) |
| 校验 | 提取一致性 | 生成结果的机械门禁 + 仿真 |
| 闭环 | 无 | **校验不过→迭代重生成**,直到满足交付 |

v1 的 datasheet 提取管线降级为 v2 的"知识库入库管道"之一,结论仍然有效。

## 2. 商业竞品全景:链路已被市场验证

### 2.1 Flux.ai —— 完整愿景的商业标杆(最重要参照)

闭源 SaaS,2026-02 获 **$37M 融资**(8VC 领投),产品能力与本项目愿景逐段对应:

| 愿景段 | Flux.ai 对应能力(博客可考) |
|---|---|
| 意图解析 | Copilot 自然语言设计电路:"placing parts, connecting circuits"(2025-05) |
| RAG 知识库 | **Copilot Knowledge Base**:学习用户设计原则、选型偏好、原理图风格指南(2025-05/07);AI Research & Planning;器件研究;datasheet 解析;80 万+ 器件库带实时供应链数据 |
| 生成 | "The First AI Hardware Engineer":多步工作流——选型→原理图→布局→检查(2025-10);AI Generated Netlists |
| 校验 | AI Design Reviews(DRC/DFM 检查面板);Simulate Circuits with a Prompt(2026-03) |
| 迭代 | **"10x Faster & Self-correcting"**(2026-02);**steerable agent** 可中途改向、idea→board 单线程(2026-05);MCP server 开放外部 agent(2026-08) |

**启示**:① 端到端愿景商业成立;② 闭环自纠错是其 2026 年的核心卖点,印证"loop"是关键差异化;③ 知识库(Knowledge Base)被产品化为一级能力;④ 它闭源、绑定自家编辑器与器件库、面向海外供应链(Digi-Key 等)——**嘉立创/LCSC 生态 + 开源 + 可机械复验的门禁**是其未覆盖的空间。

### 2.2 其他商业玩家(部分重叠)

| 公司 | 定位 | 与本项目关系 |
|---|---|---|
| Quilter | 物理驱动 AI(强化学习 + 物理仿真),原理图→可制造 PCB 数小时;兼容 Altium/KiCad | 只做布局布线,不做原理图生成;无 RAG;印证"约束驱动 + 候选返回"的交付形态 |
| Jitx | 代码生成硬件(基于 Stanza DSL) | 生成路线同为"代码→设计",但语言闭源、面向大厂 |
| CircuitMind | AI 自动选型 | 仅选型段 |

## 3. 学术研究:五段链路各有直接验证

arXiv 上 "circuit design + LLM" 相关论文 82 篇(2024 起爆发),关键工作按链路段归类:

### 3.1 意图解析 + RAG 知识库

| 工作 | 结论 | 对本项目的意义 |
|---|---|---|
| **AaLLM**(2608.13472,2026-08) | 端到端多 agent:user specs → netlist(拓扑生成 + 尺寸);**自动从论文/教科书构建知识库 + RAG 模拟设计专家**;Designer/Critic/Evaluator 三角反馈;SPICE 调用次数降 3–4.5×,墙钟时间降 40× | **最接近完整愿景的学术实现**;验证"自动建知识库 + RAG + 反馈闭环"整条路线 |
| **MuaLLM**(2508.08137,2025-08) | 多模态 LLM agent + 混合 RAG(电路设计论文向量库),ReAct 工作流迭代检索;RAG-250 上 90.1% recall,成本降 10× | 验证"混合 RAG 检索电路文献"工程可行;其 RAG-250/Reas-100 数据集可作参考 |
| **ChipExpert**(2408.00804) | IC 设计专用开源 LLM,配 RAG 系统抑制幻觉 | 验证 RAG 是电路域 LLM 降幻觉的标准手段 |

### 3.2 LLM 生成原理图/拓扑

| 工作 | 结论 | 对本项目的意义 |
|---|---|---|
| **AnalogCoder**(2405.14918) | 首个免训练 LLM agent 经 Python 代码生成设计模拟电路;**反馈增强流自纠错**;**电路工具库:成功设计归档为可复用子电路**;成功设计 20 个电路(比 GPT-4o 多 5 个) | 验证"代码生成 + 反馈闭环 + 成功案例入库复用"三件套——与电路块库思想完全同构,可直接借鉴其 sub-circuit library 设计 |
| **AnalogXpert**(2412.19824) | 拓扑合成拆为 **block selection + block connection** 两子任务(CoT + ICL);SPICE 码表示 + 子电路库;**proofreading 增量纠错**;成功率 40%/23%(vs GPT-4o 的 3%) | 两段式生成(先选块再连线)显著优于直接生成——本项目的生成器应采用该分解 |
| **AnalogAgent**(2603.23910,2026-03) | 多 agent(Code Generator / Design Optimizer / **Knowledge Curator**)+ **自进化记忆 SEM**:执行反馈蒸馏成 playbook 跨任务复用;Gemini 92% Pass@1,小模型 +48.8% | 验证"已验证案例自动沉淀 + 检索复用"能持续提升成功率——本项目 RAG 知识库应设计为自进化 |

### 3.3 Datasheet/文档解析(知识库入库管道)

| 工作 | 结论 |
|---|---|
| **DocEDA**(2412.05301) | 版面分析模型分类 datasheet → LLM+CoT 提取电气参数;**GAM-YOLO + 拓扑识别把电路图解析成 netlist**;再经空间映射优化 | 
| (v1 调研)DatasheetReader | PDF→证据页→LLM 结构化引脚→KiCad 符号 |

结论:datasheet 的**参数表格提取**与**参考设计图→netlist**两条入库路线均有论文级验证。

### 3.4 校验与 LLM 可靠性(门禁必要性证据)

| 工作 | 结论 | 对本项目的意义 |
|---|---|---|
| **PCEval**(2601.02404) | 13 个主流 LLM 物理计算基准:**逻辑电路设计表现好,但物理引脚连接(面包板布局)严重挣扎** | 实证 LLM 生成电路的典型失败模式在连接层——**校验门禁必须含连通性/引脚级检查**(easyeda-agent 的 `sch check`/`bridge-check` 恰好覆盖) |
| ACDC(2512.09199) | LLM 生成设计对数据格式敏感、不稳定、泛化有限 | 进一步支撑"生成必须套机械门禁 + 迭代"而非一次生成 |
| ORACLE(2608.04999)、LLM-USO(2502.02764) | RL/知识迁移做尺寸优化 | 二期(参数 sizing)可参考 |

## 4. 开源生态现状与空白(结合前两轮调研)

| 能力段 | 开源现状 | 缺口 |
|---|---|---|
| 意图解析 | easyeda-agent 的 S0–S6 门控流程已从需求文档起步 | 无结构化意图模型(需求→设计约束的可校验表示) |
| 知识库/RAG | 电路块库(20 块,人工消化);Flux 的 Knowledge Base 闭源 | **开源侧无 datasheet/案例的 RAG 知识库**,无自进化机制 |
| 生成 | easyeda-agent typed actions(place/autoconnect);copilot 自然语言生成 | 无"块选择+块连接"两段式生成器,无与 RAG 的接合 |
| 校验 | easyeda-agent `sch gate` 一条龙(bbox/check/bridge/drc)+ copilot SPICE | 已较完整,可直接复用 |
| 迭代闭环 | easyeda-agent DRC 5 轮数据闭环是**人工驱动**的特例 | **无自动化"校验反馈→重生成"循环**(loop 是最大空白) |

**空白确认:RAG 增强生成 + 校验闭环迭代 + 嘉立创生态落地,三者在开源世界互不相交。**

## 5. 分段可行性结论

| 链路段 | 可行性 | 依据 | 主要不确定性 |
|---|---|---|---|
| 1. 意图解析 | 高 | easyeda-agent 已跑通需求文档输入;学术 specs→netlist 已验证 | 需求歧义→设计约束的映射需人工确认门(弱门禁) |
| 2. RAG 知识库 | **中高** | MuaLLM/AaLLM 验证混合 RAG;datasheet 入库管道有 DocEDA/DatasheetReader 验证 | **知识库冷启动工程量**:结构化案例数量与质量决定检索增益;电路知识用向量检索还是图检索(拓扑是图结构)需实验定夺 |
| 3. LLM 生成 | 高 | AnalogXpert 两段式(选块+连线)40% vs 3%;AnalogCoder 代码生成 + 子电路库 | 直接生成任意拓扑仍不稳,应"块组合优先、自由生成兜底" |
| 4. 结果校验 | **最高** | easyeda-agent `sch gate` 现成;SPICE(easyeda-copilot)现成;PCEval 证明门禁必要 | 仿真仅覆盖部分电路类型(数字/混合信号难全覆盖) |
| 5. 迭代闭环 | 中 | AnalogCoder/AnalogXpert/Flux 均验证自纠错有效;easyeda-agent 有 5 轮 DRC 闭环先例 | **收敛性**:需设计迭代上限、失败归因(把 DRC finding 反馈给 LLM 的信息设计)与防震荡策略 |

## 6. 建议架构(vision → modules)

```
┌─ 输入层 ─────────────────────────────────────────────┐
│ 需求文档 / BOM / datasheet PDF / 对话意图              │
└──────┬───────────────────────────────────────────────┘
       ▼ 意图解析(LLM → 设计约束 IR:功能/接口/电源/成本)
┌─ 知识层(RAG)───────────────────────────────────────┐
│ ① 结构化 datasheet 库(v1 提取管线入库,带出处)        │
│ ② 已验证案例库(电路块库 + 成功交付回写,自进化)        │
│ ③ 器件库(LCSC ground truth + 实时库存价格)           │
│ 检索:向量(语义)+ 图(拓扑)+ 关键字(C号/型号)混合    │
└──────┬───────────────────────────────────────────────┘
       ▼ 生成层:两段式(AnalogXpert 路线)
│  a. 块选择:约束 IR + 检索结果 → 子电路块组合方案        │
│  b. 块连接:端口绑定 → netlist/typed actions(easyeda)  │
└──────┬───────────────────────────────────────────────┘
       ▼ 校验层(强/弱分级,复用 easyeda-agent)
│  强:sch check / bridge-check / layout-lint / DRC 归零  │
│  强:连通性=设计意图 IR 逐条对齐(可机械比对)           │
│  弱:SPICE 工作点(适用子集)/ 人工确认门(弱置信步骤)   │
└──────┬───────────────────────────────────────────────┘
       ▼ 迭代控制器(loop)
│  校验 finding → 归因分类 → 定向反馈(改块/改连/改值)     │
│  上限 N 轮 + 防震荡(同错两轮即升级人工)                │
│  通过 → 交付(BOM带C号/网表/审计日志)+ 案例回写入库 ②   │
└─────────────────────────────────────────────────────────┘
```

设计原则(从调研提炼):
1. **块组合优先,自由生成兜底**(AnalogXpert 数据支撑);
2. **案例自进化**:每次通过校验的交付回写知识库(AnalogCoder/AnalogAgent 路线),知识库冷启动靠迁移 easyeda-agent 电路块库 + 自己跑通的 showcase;
3. **迭代反馈要结构化**:DRC finding 带坐标/网络名回喂,不是原始日志(参考 easyeda-agent "从 DRC 明细坐标反推根因"的实践经验);
4. 弱置信环节一律人工确认门,不伪装自动化。

## 7. 风险清单(新增 v2 维度)

| 风险 | 等级 | 缓解 |
|---|---|---|
| RAG 冷启动:初期案例少,检索增益低 | 高 | 迁移 easyeda-agent 20 块电路块 + 复刻 3~5 个公开参考设计(oshwlab 开源工程)做种子;检索不中时退化为块库直查 |
| 拓扑检索表示:向量检索对电路图结构不敏感 | 中 | 混合检索:拓扑哈希/图同构匹配 + 向量语义;GraphRAG 思路备选 |
| 迭代不收敛/震荡 | 中 | 归因定向反馈 + 迭代上限 + 同错升级人工(easyeda-agent DRC 5 轮闭环经验:31→0 可达) |
| 生成幻觉器件(LCSC 无此料) | 低 | 器件库 uuid 直查是硬约束,幻觉在选型即暴露 |
| 校验盲区:仿真覆盖不了的电路类别 | 中 | 强门禁不依赖仿真(几何/连通/DRC 足够);仿真作为加分项 |
| 与 Flux.ai 的定位冲突(它也在快速迭代) | 中 | 差异化:开源 + 嘉立创/LCSC 供应链 + 本地部署(企业数据不出域)+ 可机械复验 |
| 平台墙(v1 已记录:无编程 undo、增量 import_changes 失效等) | 中 | 全部沿用 easyeda-agent 已趟明的边界与 workaround |

## 8. PoC 路线(4 周,目标:验证最小闭环)

### Week 1:知识库 + 检索原型
- 搬运 easyeda-agent 电路块库(20 块)入本地库;实现混合检索(向量 + C号/关键字);
- 用 5 份需求文档测试:检索命中率与人工判断的相关性。
- **验收:Top-5 检索中含正确块的比例 ≥ 80%。**

### Week 2:两段式生成器
- LLM 生成"块组合方案 JSON"(选块 + 端口绑定),不含自由拓扑;
- 方案落 EasyEDA(复用 `sch place`/`block-apply`/`autoconnect`)。
- **验收:5 个需求中 ≥ 3 个生成的原理图通过 `sch gate` 首轮或二轮。**

### Week 3:迭代控制器
- 校验 finding → 归因(缺块/错连/错值)→ 定向重生成;上限 5 轮;
- 记录每轮收敛曲线。
- **验收:3 轮内通过率 ≥ 60%,5 轮 ≥ 80%(对照 easyeda-agent 人工驱动 DRC 31→0 的 5 轮先例)。**

### Week 4:datasheet 管道接入 + 端到端 showcase
- 接入 v1 调研的引脚表提取管线,给库外冷门器件建符号;
- 端到端跑通一个完整需求(含 1 个库外器件)并回写案例库。
- **验收:全链路无人为修改工程文件,审计日志完整。**

### Go/No-Go
- **Go**:Week 3 收敛指标达成 → 立项一期(块组合域),自由拓扑生成与 sizing 进二期;
- **调整**:Week 1 检索指标不达 → 先补知识库(人工策展 50+ 块)再重测;Week 3 不收敛 → 分析归因反馈质量,考虑引入 critic agent(AaLLM 三角反馈)。

## 9. 参考文献与链接

**商业**:[Flux.ai Blog](https://flux.ai/p/blog)(Copilot/Knowledge Base/Self-correcting/steerable agent 各文);[Quilter](https://www.quilter.ai/)

**学术**(arXiv):
- AaLLM: An End-to-End Analog Circuit Design Framework(2608.13472)
- MuaLLM: Multimodal LLM Agent with Hybrid RAG(2508.08137)
- AnalogCoder: Analog Circuit Design via Training-Free Code Generation(2405.14918)
- AnalogXpert: Automating Analog Topology Synthesis(2412.19824)
- AnalogAgent: Self-Improving Analog Circuit Design(2603.23910)
- DocEDA: Extraction and Design of Analog Circuits from Documents(2412.05301)
- PCEval: Benchmark for Physical Computing Capabilities of LLMs(2601.02404)
- ChatEDA(2308.10204)、ORACLE(2608.04999)、ChipExpert(2408.00804)

**开源**:easyeda-agent(showcase/门禁/电路块库)、easyeda-copilot(SPICE)、DatasheetReader、v1 调研文档 `research-datasheet-extraction-feasibility.md`
