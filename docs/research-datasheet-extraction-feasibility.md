# 调研报告:Datasheet 结构化提取 + 校验门禁可行性

- **日期**:2026-08-17
- **状态**:前期调研(Pre-PoC)
- **范围**:验证"读取 datasheet(PDF)→ 结构化提取(引脚表 / 参考设计 / 去耦要求)→ 校验门禁 → 驱动原理图设计"这条链路的技术可行性
- **结论速览**:整体可行。引脚表提取可行性高(可做强校验),参考设计提取可行性中,去耦要求提取可做但难以强校验。生态空白确认:无项目同时实现"datasheet 提取 + 交叉校验门禁 + EasyEDA 落地"。

---

## 1. 背景与动机

当前嘉立创 EDA(EasyEDA Pro)的 agent 生态(详见《easyeda-agent 生态调研》)已实现:

- **需求 → 原理图**:easyeda-agent 的 ESP32-S3 showcase 证明了"30 行需求文档 → 全自动原理图 + 四层 PCB + DRC 归零"可行;
- **校验门禁体系**:`sch check` / `sch layout-lint`(真实 bbox)/ `sch bridge-check` / `sch drc` / `sch gate` 一条龙,以及电路块库的入库门禁(`place → wire → check → DRC=0`);
- **知识来源的替代方案**:电路块库(20 块/11 类目)——由人工预先消化 datasheet 参考设计,AI 复用拓扑。

**生态最大的空白**:没有任何项目实现"PDF datasheet → 提取引脚表 / 参考设计 / 去耦要求 → 程序化校验 → 生成原理图"的自动化链路。现有 agent 对冷门器件无能为力,只能靠 LLM 裸读 PDF,错误会静默流入原理图,没有门禁兜底。

本项目目标:补齐这一环,把 agent 从"只会画常见电路"扩展到"任意器件"。

## 2. 现有可复用资产(竞品与半成品)

### 2.1 直接相关项目

| 项目 | 做到了什么 | 缺什么 | 参考价值 |
|---|---|---|---|
| [LNfromNorth/DatasheetReader](https://github.com/LNfromNorth/DatasheetReader)(2026-06,Python) | **最接近**的完整管线:PyMuPDF 页索引 → 证据页(pin/package 页)导出 → OpenAI 兼容 LLM(DeepSeek)结构化提取 → KiCad symbol 生成(KiPart CSV + `.kicad_sym` 回退写入)+ 一致性 review 报告;支持 STM32/VL817 系族 PDF 多 target 流程;封装候选匹配 | 只有 KiCad 后端;校验仅"数据一致性检查",无强门禁;无参考设计/去耦提取;无 EasyEDA 落地 | 架构蓝本:证据页定位 → LLM 提取 → 后端生成 → review 报告,四段式可直接借鉴 |
| [easyeda/eext-datasheet-helper](https://github.com/easyeda/eext-datasheet-helper)(官方) | 本地 OCR 模型识别的数据手册 AI 查阅助手 | 纯查阅,不驱动设计;无结构化输出 | 证明官方认可 datasheet+AI 方向;OCR 能力可参考 |
| [easyeda-agent](https://github.com/zhoushoujianwork/easyeda-agent)(229 stars,Go+TS) | typed action 体系、`sch gate` 门禁、LCSC 库 uuid 选型、电路块库入库门禁 | 无 datasheet 输入端 | **落地端与门禁体系直接复用**;提取产物可走其 `sch place → autoconnect → sch gate` 现有链路 |
| [easyeda-copilot](https://github.com/biosshot/easyeda-copilot)(110 stars,TS) | 自然语言生成原理图、LCSC 意图搜索、可复用块、**SPICE 仿真**(自动选模型) | 无 datasheet 提取 | SPICE 仿真可作为参考设计提取物的验证手段 |

### 2.2 相关但不同的项目

| 项目 | 相关点 | 差异 |
|---|---|---|
| deepsheet | LLM 从 PDF/HTML datasheet 提取结构化数据 | 只做参数键值(原子规格),无引脚表/拓扑 |
| OTSE(光模块规格提取器) | 本地 LLM 解析 PDF datasheet 为结构化原子参数 | 同上,域限定光模块 |
| z3ugma/webparts-librepcb | EasyEDA/LCSC 搜索 datasheet/封装/原理图并导入 LibrePCB | 库检索工具,不做 PDF 解析 |

### 2.3 生态空白确认

GitHub 搜索 `easyeda datasheet` 仅 3 个仓库、`datasheet pin extraction` 仅 1 个、`datasheet parser LLM` 5 个(均为小项目或不同域)。**"datasheet 提取 + 交叉校验门禁 + EasyEDA 落地"三要素齐备的项目不存在,空白确认。**

## 3. 分目标可行性分析

### 3.1 引脚表提取 —— 可行性:高

**有利条件**:

1. **高度结构化**:Pin Definition / Pin Description 表格在 datasheet 中格式相对规整(编号 + 名称 + 类型 + 描述);
2. **存在 ground truth 可比对**(这是整个方案的立足点):LCSC/立创库中每个 C 号器件的符号 JSON 自带 pin number/name 全集,可直接拉取做集合级 diff;
3. **多封装变体提供冗余**:同一器件家族(LQFP64 vs QFN48)的引脚表可交叉核对;
4. **同 PDF 多处印证**:引脚表(文字)、封装图(drawing)、参考设计图(应用电路)三处出现的同一器件互相印证。

**主要困难**:

- 双栏排版、跨页表格、旋转表格——常规 PDF 表格工具(camelot 等)的已知弱点;
- 需页级版面分析:先定位 Pin Definition 页,再局部提取(证据页路线已被 DatasheetReader 验证);
- 图像型(扫描)老 datasheet 需 OCR,精度降级。

### 3.2 参考设计提取 —— 可行性:中

**有利条件**:多模态 LLM 读电路图的能力已被普遍验证(拓扑→网表的转换在信息论上是确定性的:器件 + 连接关系)。

**主要困难**:

1. 应用电路图是**图像**,不是文字表格——需要多模态模型,精度未知且难以事先承诺;
2. 图中器件标号(C1、R3)与网络标签的视觉歧义(交叉线、跳线点、省略的电源符号);
3. 部分参考设计散落在文字描述中("建议 VBUS 引脚串联 5.1kΩ")。

**兜底手段**(见 4.3):拓扑转 netlist 后过 ERC + SPICE,再与 datasheet 的 BOM/典型应用元件清单交叉比对。

### 3.3 去耦/外围要求提取 —— 可行性:低(强校验难)

**困难本质**:这类要求散落在自然语言里("每个电源引脚放置 100nF 陶瓷电容","BOOT 引脚上拉 10kΩ"),**可提取、可执行,但难以程序化证明"提取全对了"**——遗漏(该提取没提取)没有机械判据,属于开放召回问题。

**定位建议**:不伪装成强门禁。做成"带引用出处(page/原文摘录)的建议清单 + 人工确认门",执行结果(电容确实放了、值对了)可被 `sch check` 验证,但"建议是否完备"交给人工。

## 4. 校验门禁设计(方案核心)

门禁的分级哲学:**能用程序证明的做强门禁(fail/block),不能的做弱门禁(标记/人工确认),绝不静默通过。**

### 4.1 强门禁(不通过即 fail)

| 门禁 | 检查内容 |
|---|---|
| **三方 pin 集合 diff** | 提取的 pin 集合 vs LCSC 库符号 pin 集合 vs 封装焊盘数,任何不一致直接 fail |
| **pin 表内部一致性** | pin number 无重复、无跳号异常;GND/VCC 数量守恒;power/ground 类型与名称正则匹配 |
| **多封装交叉核对** | 同家族不同封装的引脚表,同名 pin 的 name/type 必须一致 |
| **双通道提取比对** | 规则解析(PyMuPDF/camelot)与 LLM 解析各跑一遍,逐 pin 比对;不一致的 pin 标记低置信度,不进入下游 |
| **同 PDF 交叉印证** | 引脚表 vs 封装图 vs 参考设计图,同一器件在三处出现须互相印证 |
| **下游门禁(已有,复用)** | 提取产物进 EasyEDA 后走 `sch place → autoconnect → sch gate`(layout-lint / check / bridge-check / drc),全部归零才算过 |

### 4.2 参考设计的验证门禁

1. **拓扑 → netlist 转换**:提取的"器件 + 网络"拓扑 JSON 转标准网表;
2. **ERC**:电气规则检查(引脚类型冲突、悬空输入等);
3. **SPICE 直流工作点仿真**:easyeda-copilot 已验证 EasyEDA 内可跑 SPICE 且能自动选模型——参考设计若连直流工作点都不收敛/不合理,直接打回;
4. **BOM 交叉比对**:拓扑中出现的器件与 datasheet 典型应用元件清单(若以表格形式存在,提取置信度高)比对。

### 4.3 弱门禁(标记 + 人工确认)

- 去耦/上拉等散文字要求:提取为建议清单,每条带**引用出处**(页码 + 原文摘录),人工确认后才执行;
- 提取 vs 库符号冲突时的仲裁:**标记人工裁决,不静默取库**(库符号本身可能是错的);
- 扫描件 datasheet:默认不支持,或整体降级为"全人工确认"通道。

## 5. 风险清单

| 风险 | 等级 | 缓解 |
|---|---|---|
| 双栏/跨页/旋转引脚表解析失败 | 中 | 页级版面分析 + 证据页局部放大重提取(已验证路线);双通道比对兜底 |
| 图像型(扫描)老 datasheet | 中 | 明确声明不支持,或降级为人工确认通道 |
| LCSC 库符号本身有错(ground truth 污染) | 中 | 冲突时人工裁决而非静默取库;多封装交叉核对可暴露部分库错误 |
| 参考设计图视觉歧义导致拓扑错误 | 中高 | ERC + SPICE + BOM 三重验证;置信度分级,低置信度不自动执行 |
| 去耦建议的遗漏无法检测 | 高(本质) | 定位为建议清单 + 人工确认,不宣称完备 |
| LLM 提取的幻觉(编造 pin) | 中 | 三方 diff 是硬约束:编造的 pin 会在集合比对中暴露 |

## 6. PoC 路线(2~3 周)

### Week 1:引脚表单点打穿(决定 Go/No-Go)

- 选 10 份热门器件 datasheet(ESP32-S3 / CH340 / AMS1117 / STM32F103 等级别,含 2~3 份多封装家族 PDF);
- 管线:PyMuPDF 定位 Pin Definition 页 → LLM 结构化输出 pin JSON(number/name/type/description + 出处页码)→ 与 LCSC 库符号 diff → 双通道比对;
- **验收指标:三方比对通过率 ≥ 95%(热门器件)**;
- 同时记录:每份 PDF 的失败模式分类(双栏/跨页/扫描/其他)。

### Week 2:参考设计提取

- 多模态 LLM 读典型应用电路页 → 输出"器件 + 网络"拓扑 JSON(带置信度与出处);
- 验证:拓扑转 netlist → ERC → SPICE 直流工作点 → 与 datasheet BOM 表交叉比对;
- **验收指标:≥ 7/10 份样本的参考设计通过 ERC + BOM 比对。**

### Week 3:全链路落地

- 提取产物接 easyeda-agent:`sch place → autoconnect → sch gate` 跑通一个**库中无电路块的冷门器件**;
- 输出物:提取 JSON schema 定稿、门禁规则清单、失败模式报告。

### Go/No-Go 判据

- **Go**:Week 1 通过率 ≥ 95%,且 Week 2 至少 7/10 通过 → 一期立项("引脚表强校验"单点打穿),参考设计与去耦建议进二期;
- **No-Go/调整**:通过率 < 80% → 分析失败模式集中度;若集中在扫描件,收窄范围为"文字型 PDF";若普遍失败,转向"人工半自动确认"产品形态。

## 7. 与现有生态的集成方式(建议)

```
datasheet.pdf
   │  PyMuPDF 页索引 + 证据页定位
   ▼
[双通道提取] ── LLM 结构化提取(多模态) ── 规则解析(camelot/表格)
   │              双通道逐 pin 比对
   ▼
[交叉校验门禁] ── LCSC 库符号 diff / 封装焊盘数 / 多封装交叉 / 内部一致性
   │              fail → 阻断;低置信 → 人工确认
   ▼
结构化产物(pin JSON / 拓扑 JSON / 建议清单+出处)
   │
   ▼
easyeda-agent 现有链路:sch place → autoconnect → sch gate(DRC 归零)
```

- 落地层**不重造轮子**:easyeda-agent 的 typed action + 门禁体系全部复用;
- 本项目新增的是**输入端**(datasheet → 结构化产物)与**提取侧门禁**(三方 diff 等);
- 产物 schema 与 DatasheetReader 的 PartIR 模型对齐参考,便于未来互通或支持 KiCad 后端。

## 8. 参考

- [LNfromNorth/DatasheetReader](https://github.com/LNfromNorth/DatasheetReader) — 架构蓝本
- [zhoushoujianwork/easyeda-agent](https://github.com/zhoushoujianwork/easyeda-agent) — 落地端与门禁体系(showcase: docs/showcase-esp32-mini.md)
- [biosshot/easyeda-copilot](https://github.com/biosshot/easyeda-copilot) — SPICE 仿真验证手段
- [easyeda/eext-datasheet-helper](https://github.com/easyeda/eext-datasheet-helper) — 官方 OCR 查阅助手
- [PyMuPDF](https://pymupdf.readthedocs.io/) / [KiPart](https://github.com/devbisme/KiPart) — 基础库
