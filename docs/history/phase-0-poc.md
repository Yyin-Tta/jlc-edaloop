# [归档] PoC 执行记录(W0-W4,2026-08-17~18 全 Go)

> 2026-09-01 自 DEVELOPMENT.md 原样迁出(零改写;文中 §N/表号/行内引用指迁出时主文档节号)。
> 主文档对应位置只留收官快照;本文件为该阶段执行细节的完整记录。

### PoC(4 周,验证最小闭环——指标源自调研文档,不达标不进入下一阶段)

| 周 | 里程碑 | 交付物 | Go 指标 |
|---|---|---|---|
| W0 | M0 环境就绪 | §8.1 全勾 + `edaloop --help` 骨架 | showcase 最小命令真机跑通 |✅ 2026-08-17 全部达成(含首次 commit/分支保护、ADR-0002 钉 v0.25.1) |
| W1 | M1+M2 原型 | DesignIR schema 定稿;块库迁移入 sqlite-vec;混合检索 `retrieve()` | Top-5 检索含正确块 ≥80%(5 需求×人工标注) |✅ 2026-08-17 达成:recall@5 = 22/25 = **88%**(GLM 解析→DesignIR.query_text 浓缩查询真实链路与 raw 回退双验证,标注见 `evals/w1-retrieval.json`)。剩余 3 miss 均为无型号的泛功能块(端子/LED),待种子文本调优 |
| W2 | M3 生成器 | BlockPlan→typed actions→EasyEDA 真机落图 | 5 需求中 ≥3 个过 `sch gate`(≤2 轮) |✅ 2026-08-17 达成:3/3 试跑全过(req-01 两轮内:首轮 mcu 块放置重叠,修复后过;req-02 8 块/req-03 5 块均**单轮无人值守**全过)。M3 落地:planner(GLM 结构化输出)→compiler(端口契约校验+block-apply 编译)→adapter(版本钉死/daemon 探活/window 路由钉扎)→audit(JSONL)。产物:runs/req01-schematic.svg + 3 份审计目录 |
| W3 | M5 迭代闭环 | Finding 归因+定向反馈+防震荡;收敛曲线数据 | 3 轮通过率 ≥60%,5 轮 ≥80% |✅ 2026-08-18 达成:**pass@3=80%(4/5),pass@5=80%(4/5)**,收敛的 4 例全部 **1 轮**通过(req-01/02/03/05);req-04 HALT——MISSING_RAIL@5V_ISO(隔离轨无 upstream 块,W2 已登记的种子库债务)+GATE_FAIL 同错两轮按设计升级人工,非震荡空转。M4/M5 落地:validate/(Finding schema+rail 对齐+gate 转译+uncovered 弱门禁)、loop/(归因器+迭代控制器:上限5轮/同错两轮HALT/RELAYOUT 反馈含 spacing 递增 400→500→600+清页重规划)、edaloop run 全链路+eval w3-loop(断点续跑) |
| W4 | M6+端到端 | datasheet 管道(引脚表强校验);全链路 showcase(含 1 个库外器件) | 全程零手工改工程;审计日志完整;案例成功回写 |✅ 2026-08-18 达成:①M6 ingest 管道真机验证(ULN2003A_ti.pdf→16 脚表,LLM/规则双通道一致,verdict=pass,入库 sqlite);②库外器件 place 通道(lib search C 号精确→sch place→逐引脚 autoconnect);③showcase(4 路 ULN2003 驱动板)零手工 **2 轮收敛**(r1 layout-lint fail→RELAYOUT 反馈→r2 spacing 500 全绿);④交付物 SVG/netlist(hash 9947851e)/案例回写 seeds/cases.jsonl |

**PoC 总 Go/No-Go**:W3 收敛指标 + W4 showcase 双达成 → 立项 Phase 1;仅 W3 达成 → Phase 1 砍掉 datasheet 管道先行;W3 不达成 → 归因反馈质量分析,引入 critic agent(AaLLM 三角)重试一次,再不达成则重估范围。
