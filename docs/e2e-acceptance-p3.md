# Phase 3 端到端验收报告(REQ-27 智能门磁传感器)

> 日期:2026-08-21 · 验收对象:脑洞需求 → 原理图 → PCB → 待支付订单 全链零手工编辑
> 依据:DEVELOPMENT.md §9 Phase 3 端到端验收定义(细化问答与下单确认两类人工交互除外)

## 结果总览

| 阶段 | 结果 | 轮次/耗时 | 交付物 |
|---|---|---|---|
| 原理图(M1-M5) | **PASS** | 1 轮 | svg + netlist(hash e6a06bd0) + bom(¥2.61) + sizing(4 条) + swap + review |
| PCB(M8) | **PASS**(gate_ok=True) | 完整 pipeline+修复循环 | drc/check/layout-lint 三门禁绿 + pcb-report.md |
| 下单(M9) | **报价完成+订单草稿** | 即时 | 报价 ¥54.75(5pcs) + 预检 4 项 MOQ 提示 + order-draft.md |

**验收判定:通过**——从需求文档到待支付订单草稿,零手工编辑工程(无人工改原理图/PCB);
预检的 4 项 MOQ 提示为元件商务属性(单样数量低于 MOQ 是打样常态),已给出修复建议,不属流程缺陷。

## 各阶段明细

### 1. 原理图(run 34491818264a)
- 块选择 7 块:STM32F103C8T8 / A3144 霍尔 / 24C02 / SP3485 / 宽压输入 / AMS1117 / BOOT+RESET 按键——需求六个功能点全覆盖
- uncovered 3 项(诚实登记):LED×2(目录无 lcsc LED 块)/调试排针/A3144 上拉电阻
- sizing 4 条(含 XL1509 宽压 buck 全公式:电感/纹波/电容/TVS/保险丝)
- critic 评审:无设计层缺陷误报

### 2. PCB(42 器件 2 层板)
- pipeline 18 步全链:new-board(复用)→import→place-constrained→auto-place(gap60)→outline→tier 确认→lint-gate→route→pour→silk→mount-holes
- 调试过程:42 器件板首现 C9/R3 物理重叠短路,通用修复循环(tight/boxed/overlap 定向挪件)清零;上游 `pcb arrange` 有 stale 读 0 组件的兼容 bug(已绕开并记录)
- 终态:drc 0 fatal / check 0 error / layout-lint pass
- 交付报告含 degraded 判定通道(R14:电气安全过但可制造性警告 → 半成品+人工修板指引,本次未触发)

### 3. 报价与订单
- 预检:4 项 MOQ 拦截(C8963/C15127/C6186/C720477,qty=1 低于 MOQ 2-10)+修复建议
- 三段报价:PCB ¥2.00 + SMT ¥50.14 + 元件 ¥2.61 = **¥54.75**(5pcs 经济档)
- 订单草稿:--confirm 显式确认后生成,含人工下单步骤指引,**无任何支付动作**(R12 纪律)

## 交互清单(验收允许的两类人工交互)
1. (本次未触发)需求歧义问答——refine 通道
2. 下单确认:`order --confirm` 显式 flag + 支付在官方页人工完成

## 回归状态(同日统一回归)
- smoke 3/3 PASS · daily 8/8 PASS(7×1r+1×2r)
- all 层 14/26 + rest 增量 2(用户叫停合并层,已有结果全 PASS 无一失败)

## 遗留与后续
- `pcb arrange` 上游 stale bug:值得提 easyeda-agent issue(与已提交的 rollback bug 同源:引擎 stale 竞态)
- 42 器件级 2 层板的 boxed-in 现象:布局质量上限问题,Phase 4 可探索布局评分驱动(上游 refine 仅实现 grid-snap 维)
- 块库补 LED(带 lcsc)/排针/阻容三件套:消除 req-27 的 3 项 uncovered
