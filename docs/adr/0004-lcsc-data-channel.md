# ADR-0004:LCSC 数据通道(easyeda-agent CLI 复用 vs 直连 API)

| 项 | 值 |
|---|---|
| 状态 | **草案**(2026-08-17)——推荐方案已定,待 W0 真机安装 easyeda-agent 后验证 `resolve-lcsc` 实际输出(字段完整性/稳定性),再转 ✅ |
| 影响模块 | M2 knowledge(LCSC 器件库镜像)、M6 ingest(pin 集合 diff 通道) |
| 上游关联 | DEVELOPMENT.md §4 原则4(ground truth)、§5.2 M2 边界(不做在线爬虫);research-datasheet-extraction-feasibility.md(符号 JSON pin 全集) |

## 背景

- M2 三库之一需要 LCSC 器件库镜像(C 号 / pin / 库存 / 价格);原则4 要求 pin 集合、器件存在性、C 号一律以 LCSC 库为 ground truth。
- M6 datasheet 管道的「三方 pin 集合 diff」依赖:LCSC 库每个 C 号器件的符号 JSON 自带 pin number/name 全集,可直接拉取(调研已确认可行)。
- M2 边界明确:不做在线爬虫(批量预镜像)。

## 候选方案

| 候选 | 说明 | 初判 |
|---|---|---|
| A. 复用 easyeda-agent CLI 库查询通道(`resolve-lcsc` / uuid 直查) | 经钉版本的 CLI 子进程调用 | **推荐** |
| B. 直连 LCSC/立创 API | 官方无公开 API 合同,需逆向/维护鉴权 | 备选 |
| C. 自建爬虫批量镜像 | 违反 M2 边界 | 否决 |

## 决策(草案):PoC 单通道走 A

1. 所有「逐 C 号 ground truth 查询」(pin 全集、器件存在性、封装)经 easyeda-agent CLI 完成,不直连任何 LCSC HTTP 端点;
2. 抽象出 `LcscProvider` 接口(与 `llm/` 同纪律),A 通道为默认实现,为 B/本地镜像留切换位;
3. W0 验证项:`resolve-lcsc` 输出是否含 pin number/name 全集;若缺失,pin ground truth 改走「拉取符号 JSON」路径作为 A 的补充子通道。

**理由**:

- **同源性**:easyeda-agent 落图本身就是 LCSC uuid 选型,查询与生成用同一数据源,天然消除「查得到但画不出」的错位;
- **零逆向成本与合规安全**:官方无公开 API,B 通道需逆向维护,稳定性/合规风险高;
- **频率匹配**:PoC 规模(5 需求 × 10 datasheet)逐条在线查询足够,无需镜像。

## 限制与后续

- 库存/价格的**批量镜像**是 Phase 1+(BOM 成本优化)需求,逐条 CLI 查询不适合批量 → 届时评估官方/合作通道,另出 ADR;
- 强依赖 easyeda-agent CLI 面:已由 R3 登记,版本钉死(ADR-0002)缓解。

## 后果

- 正:W0 无额外账号/合规成本;ground truth 与落图同源;接口留了切换位。
- 负:批量场景受限(Phase 1 需补);CLI 子进程调用有进程开销(PoC 可忽略)。
