# [归档] Phase 2 执行记录(自由拓扑/BOM 成本/sizing,2026-08-20)

> 2026-09-01 自 DEVELOPMENT.md 原样迁出(零改写;文中 §N/表号/行内引用指迁出时主文档节号)。
> 主文档对应位置只留收官快照;本文件为该阶段执行细节的完整记录。

### Phase 2+:自由拓扑生成、参数 sizing、BOM 成本优化、多页大原理图、(评估)KiCad 后端

**P2-A 批(自由拓扑受控子集,2026-08-20)**:①**双通道实现**——确定性:模式库 `freeform.py`(锂电保护 DW01A+FS8205A 交叉驱动/CAN 节点)命中即分解,controller `_augment_freeform` 在 LLM 未覆盖时注入;LLM 兜底:planner prompt 增分解规则(≤5 器件/instance 功能后缀/pins_binding 互联/模拟反馈拓扑禁止),实测 GLM 自主完成锂电保护分解并追加输入输出端子块;②**拓扑 sanity 门禁**(validate):place 通道器件电源/地脚未绑定→PIN_MISMATCH 强门禁;③planner 校验扩展:place 块引脚号必须来自目录 pinout;④正负样本真机验证:req-23 锂电保护 1 轮 PASS(prot_dw01+prot_fs 落图+网表交付);req-24 运放增益网络被诚实拒绝(uncovered 引用"v1 不支持",不瞎生成);⑤回归分级:eval w3-loop 增 `--tier smoke(3)/daily(8)/all(23)`,分级 state 文件隔离(全量回归从每轮 50 分钟降为冒烟 7 分钟);⑥负样本 req-24 移出正向回归(.bak 暂存)。

**P2-C 批(BOM 成本优化,2026-08-20)**:①**价格数据源落地**——LCSC `wmsc/ftps/wm/product/detail` API(无需鉴权,含 productPriceList 阶梯价/stockNumber/MOQ;真机验证:C8734 ¥1.73@28k 库存/C7512 ¥0.17/C16581 ¥0.19,坏 C 号优雅降级);②`generate/bomcost.py` 三件套:fetch_cost(单件实时,弱信号不抛)/summarize_bom(去重计 qty→总额+缺价/缺货清单+明细)/cost_hint_for_planner(等价类价格对比文本);③**交付层**:PASS 后 `delivery.bom.json`(按块 parts[] 展开逐件计价;req-20 实证:CH340 ¥0.59+CAN 收发 ¥0.47=总 ¥1.06;C99xx 延展号段无商务数据有明确归因,多器件块无单 C 号如实列入缺价);④**规划层**:IR `env.cost_target` 存在时才查价(无成本诉求零开销),等价类(can/rs485/usb/ldo/buck/boost)价格对比注入 planner(标注"仅参考,勿为省钱牺牲功能");⑤**检索层零侵入**——价格波动不污染检索排名(原则:价格是弱信号,检索是语义通道)。**遗留**:块库 parts[].lcsc 回填(现 62 处,多器件 upstream 块的多 C 号录入是下批数据工作)。

**P2-B 批(参数 sizing 规则子集,2026-08-20)**:①`generate/sizing.py` 规则引擎——三类确定性公式:LED 限流(R=(V-Vf)/I,E24 归一+功率档位提示+V_rail≤Vf 不可行判定)、分压网络(目标比例+E24 实际误差%+偏置电流提示)、LDO/BUCK 电容(datasheet 惯例区间子集,全纹波公式 Phase 3);`size_for_plan` 保守启发式(块 ID 识别,识别不出不猜);②交付层:PASS 后 `delivery.sizing.txt`(带公式代入过程);③ingest 联动:建议提取 kind 增 `sizing` 类(含具体参数值的原文建议);④真机实证:req-01 交付含 LDO 电容建议+压差功耗提醒。**范围纪律**:模拟反馈/补偿网络继续不做(sizing 是建议生成,不自动改连线)。

**P2 收官(2026-08-20)**:smoke(3/3)+daily(8/8)分层回归全 PASS、全部 1 轮——自由拓扑/BOM 成本/sizing 三项(ADR-0008 排序 A→C→B)零回归落地。交付物四件套定型:delivery.svg + delivery.net.json(hash) + delivery.bom.json(逐件价) + delivery.sizing.txt(公式过程)。**Phase 2 遗留**:块库 parts[].lcsc 回填(BOM 覆盖率)、自由拓扑模式库扩展、多页大原理图/KiCad(按 ADR-0008 触发条件挂起)。
