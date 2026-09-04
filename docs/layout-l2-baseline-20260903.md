# L2 布局专项基线（2026-09-03）

本报告记录三个已打开 EasyEDA Pro 工程的只读基线。采集命令只包含 `health/pages/sch list/clusters/layout-lint/check/bridge-check/drc/gate/read/sheet-geometry/text-list/layout-score/nets/netlist`，没有发送写操作。CLI、daemon、connector 均为 `v1.2.10`，EasyEDA 为 `3.2.175`。

## 结论先行

当前仍处于 L2 交付验证，布局专项路线没有跑偏。三工程都不能宣称 v2 可用：

- block-only 的本体、cluster、出带和浮脚几何硬指标为零，但 gate 仍因缺失图签和聚合 DRC warning 失败；
- req-08 本体相交为零，但存在 1 处 cluster 包络冲突、2 个单引脚网、页面空白率约 99.06%，gate 失败；
- req-07 的 6 页均出现 cluster 重叠，P6 有 13 处重叠并包含大量 bridge/orphan/multi-net-wire，存在 32 个单引脚网，不能进入写修复；
- `layout-lint` 的 0 overlap 不能覆盖 `clusters` 的包络口径；`layout-score` 只是诊断分，不是交付门。

## 统一指标

| 工程 | 页数 | body overlap | cluster overlap | cluster 包络冲突 | 出带 | 空白率 | 网表 | nets | gate |
|---|---:|---:|---:|---:|---:|---:|---|---|---|
| `edaloop-block-baseline` | 1 | 0 | 0 | 0 | 0 | 99.57% | 可解析 | 通过（1 单引脚网） | FAIL（图签、DRC warning） |
| `edaloop-req08-baseline` | 1 | 0 | 0 | 1（C1 ↔ TERMBATT） | 0 | 99.06% | 可解析 | FAIL（2 单引脚网） | FAIL |
| `edaloop-req07-baseline` | 6 | 0 | 19（逐页 1/2/1/1/1/13） | 55（逐页 3/8/1/6/7/30） | 1（P6） | 平均 97.76% | 可解析 | FAIL（32 单引脚网） | 全页 FAIL |

req-07 各页 `layout-score` 为 P1 81.2、P2 98.0、P3 88.6、P4 95.5、P5 91.0、P6 89.5；这些分数不能抵消 cluster/check/bridge/DRC 失败。

## 具体阻塞

### block baseline

`bodyOverlap=0`、`clusterOverlap=0`、`outOfSheet=0`、`floatingPins=0`、网表可解析；但 `sch check --strict` 有缺失图签，`sch drc --strict` 有 1 条聚合 warning。因此它是几何回归夹具，不是交付样本。

证据：[manifest.json](../runs/evidence/l0-block-20260903-r3/manifest.json)。

### req-08

唯一的 cluster 包络冲突为 `C1 ↔ TERMBATT`。同时 `sch nets --strict` 报 `$1N305`、`FET_MID` 两个单引脚网；check 还报 floating pin、dangling stub、marker overlap、缺分区/说明/图签。该工程当前保持 HALT 现场，不执行写修复。

证据：[manifest.json](../runs/evidence/l2-req08-20260903-r1/manifest.json)。

### req-07

P1 有 `L1 ↔ U1` 重叠；P2 有 `U2 ↔ HDRSWD1`、`U2 ↔ C5`；P3 有 `R5 ↔ J2`；P4 有 `R9 ↔ RLEDM1B`；P5 有 `R13 ↔ RLEDM2B`；P6 有 13 处 cluster 重叠并有 `U4` 出带。全工程还存在 3 个 wire bridge、7 个 orphan-stub（P6）、多条 marker-overlap/dangling-wire/wire-crossing，以及 32 个单引脚网。

证据：[manifest.json](../runs/evidence/l2-req07-20260903-r1/manifest.json)。

## 本轮代码动作

为保证三个工程并行打开时不会串工程，主链新增显式路由：

- `edaloop run --project <工程名或 UUID>`；
- `edaloop run --window <windowId>`，优先于 project；
- `stage_run(..., project=..., window=...)` 透传到 `EasyedaAdapter`；
- `EasyedaAdapter(window=...)` 覆盖环境变量并固定目标窗口。

这是 L2 的可复验性修复，不是布局算法扩展。定向测试 28 项通过；后续真机写操作必须显式指定 `--project` 或 `--window`，并在写前 inspect、写后 save 与全套 strict gate。

## 下一阶段执行顺序

1. 暂停继续写入三个现有基线工程，保留上述 HALT 证据。
2. 在全新工程上用显式 `--window` 路由启动回归；每个工程每次变更后重新采集 manifest、审计、截图和网表 hash。
3. 只允许针对已定位问题修复：req-08 的包络冲突/单引脚网；req-07 的具体 cluster overlap、bridge/orphan 和分页出带。禁止继续调大间距或让 LLM 反复重排。
4. 每轮写后固定执行 `sch save`、`sch list`、`sch clusters --strict`、`sch check --strict`、`sch bridge-check`、`sch drc --strict`、`sch gate --strict`。
5. 三个代表需求各连续 3 次达到 body/cluster/out-of-sheet/floating-pin/重复载体/错网全零，且交付物可解析后，才进入 v2 可用候选评审。

在此之前，项目定位仍是“可审计的 alpha 工程辅助工具”；不扩展 PCB、报价、下单、复杂 RAG 或自动案例回写。
