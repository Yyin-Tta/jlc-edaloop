# [归档] Phase 1 可用产品·八批执行记录(2026-08-18~20)

> 2026-09-01 自 DEVELOPMENT.md 原样迁出(零改写;文中 §N/表号/行内引用指迁出时主文档节号)。
> 主文档对应位置只留收官快照;本文件为该阶段执行细节的完整记录。

### Phase 1(PoC 后 ~3 个月):可用产品

- 知识库扩容:块 20→100+(人工策展+交付回写);datasheet 管道覆盖 Top100 热门器件
- 弱门禁产品化:人工确认队列 UI(CLI/TUI 先行)
- 稳定性:连续 20 个需求交付成功率 ≥85%;迭代中位轮数 ≤3
- 发布:v0.1 GitHub 开源(README/安装脚本/skill 打包),对齐 easyeda-agent 分发纪律

**P1 首批进展(2026-08-18)**:①知识库:PC817/B0505S/24C02/STM32F103C8(48 脚全量)/ULN2003 六块补齐 lcsc+pinout,**pinout 全部经 `lib search`→`sch place`→`sch read` 回读验证**(三方 diff 第三方落地);req-04 从 HALT 变 **1 轮 PASS**;②place 后 pin 回读校验进 controller(PIN_MISMATCH 通道,STM32 48/48 精确一致实证);③ingest 管道泛化测试 7 份 PDF:ULN2003/CH340 pass,TP4056/MT3608/STM32(LQFP100 变体)提取成功但规则通道 0 行→诚实降级 low-confidence 入库,PC817/AMS1117 无标准 pin 页(声明不支持);④`edaloop questions` 弱门禁确认队列(open_questions+uncovered,支持答案文件/交互);⑤README + v0.1.0。eval 复验 pass@3=80%/pass@5=80%(req-01/02/03 均 1 轮,req-04 1 轮)。

**P1 二批进展(2026-08-19)**:①**布局策略成型**——compile 统一网格分配(两通道,2200×1800 格,--at 显式);②**窗口治理**——adapter 按 windowId 稳定排序+探活钉扎(lastSeen 会翻转,禁用),refresh_window 热身失败重解析,clear_all_pages 清所有同工程窗口(双开窗口页面残留互访=幽灵 overlap 总根因,已请用户关双开);③**substance-verify**——上游 block-apply 的 failed-rolled-back 回滚校验会说谎(部件实际在页上、gate 已过),加机械复核:gate pass 后回读网表,电源轨(DesignIR rails∪GND)全在+页面≥10 器件才判 applied;信号网缺失=弱门禁(floating-pin 本就是 warn 级);④GLM coding 端点治理:1301 内容过滤加重试+提示词扰动,coding 端点默认 thinking disabled(又快又稳);⑤schema 宽容化:DesignIR 子模型/PlannedBlock 未知键丢弃,params 值强转 str;⑥planner 目录过滤不可落图块(无 upstream 且无 lcsc);⑦M6 建议提取(带页码+原文摘录,弱门禁);⑧块库 20→**31**(SS34/SMAJ5.0A/USBLC6/2N7002/AO3401A/TS-1088/KF301/USB-C16P/8M晶振/SRD-05继电器/1x4排针 全部符号回读验证,TL431/KF301 端子块升级)。**全量 eval:5/5 PASS,pass@3=100%,pass@5=100%,中位 1 轮(req-01/04/05 各 1 轮,req-02/03 各 2 轮)。**

**P1 三批进展(2026-08-19)**:①块库 31→**53**——上游 ready/verified 块全量映射(+22:音频功放/IMU/power-path 充电/CC1101/USB-hub/codec/自动下载/PICO 模组/I2C 隔离/INA226/红外/GNSS/MEMS 麦/microSD/ACC 检测/高边开关/SD-NAND/LCD/SY8089/buck-boost/USB-C 数据/12V 降压),端口契约照抄 `blocks show`,抽样 esp32_autodownload 真机 applied+gate pass;②M6 建议提取扩多页(pin 页+封面特性页,ULN2003 实证:2.7k 基极电阻/COM 续流等 3 条带出处);③上游 rollback bug 整理成 issue 草稿(`docs/upstream-issue-block-apply-rollback.md`,含变体 A/B 复现与特征指纹),待提交 easyeda-agent 仓库。

**P1 四批进展(2026-08-19)**:①**`edaloop replay` 落地**(M7 收尾)——审计 JSONL 重放最终轮动作序列(LLM 不重算),单动作失败记录不中断,真机验证:重放 run-010c5615bc14 r2 全部 7 动作 → gate pass;注意审计事件名(gate)与 Action.kind(sch-gate)不同名,gate 事件现补记 args;②**交付打包**进 stage_run PASS 路径——SVG/网表(+sha256_16)落 run 目录,`edaloop run` 结束打印交付清单(req-01 实证:delivery.svg + delivery.net.json + hash e150f8cd);③**扩容调研**(`docs/research-kb-expansion-oshwlab.md`):oshwlab 是 SPA 无公开 API,判定"不做爬虫,走主题清单+半自动策展"(4 批 × 12 ≈ 100 的路线图已列)。

**P1 五批(稳定性验证,2026-08-19)**:金标准集扩到 **22 需求**(+15:RS485 集线/锂电 BLE/隔离 DIDO/USB 坞/车载 GPS/音频记录/IMU 标签/PICO 网关/433M 集中器/电机驱动/红外中枢/电源监测/数据记录/环境母板 + 3 超纲:CAN/OLED 触摸/热电偶)。**全量 eval:22/22=100%,中位 1 轮,双验收线(≥85%、≤3 轮)达成**(详见 `docs/stability-report-p1.md`)。修复:轨家族归一 bug(5V_ISO vs VISO/+VO 误报 MISSING_RAIL,req-08 HALT 根因);req-19 为布局非确定性抖动(复跑即过,系统性解法列待办)。超纲 3 例行为符合设计:无块功能如实 uncovered 不阻断交付。缺口清单(CAN/OLED/触摸/热电偶/传感器批)已入扩容调研。

**P1 六批(缺口批,2026-08-19)**:①块库 68 块新增 15(CAN×2:TJA1051/SN65HVD230;显示:SSD1306 OLED;人机:TTP223 触摸/EC11 编码器;传感:MAX6675 热电偶/DS18B20/DHT11;RTC:DS3231;指示:WS2812B/蜂鸣器;MAX3232/AO3400A/AMS1117-1.8/PTC 保险丝——全部符号回读验证,PIR 库中无件跳过);②**超纲 3 项复验全 PASS**(req-20 CAN 2 轮/req-21 热电偶 1 轮/req-22 OLED+触摸 1 轮,tc1 已进 plan);③skill 打包(`skills/edaloop/SKILL.md`,wheel 含 skills 目录);④v0.1.0→**v0.2.0**(uv build 通过);⑤上游 rollback issue 已由用户提交 easyeda-agent。

**P1 七批(B/C/D 批,2026-08-20)**:①块库 +27→**95**(B 批电源:DW01A/FS8205A 锂电保护对/MP1584/TPS5430/RT9193/MP2359/CR1220 座/Micro-USB/SRD-12V;C 批人机外设:拨动开关/DIP 拨码/DRV8833 电机驱动/W25Q64/CH340K/SIT1051/HR911105A 网口;D 批传感运放:A3144 霍尔/GL5528 光敏/火焰/MQ-2/BMP280/SHT30/BH1750/VL53L0X/LM358/MCP6002/LM393——29/35 回读成功,声敏/土壤/MX1508/TD410/APS6404/SG2033/XH 座库中无件跳过);②**W1 指标适配**:块库 20→95 竞争密度 4.75x,top-k 5→8;引入功能等价类(仅 USB-C 三块/5V 端子两块——电压/功能不同的绝不并类),recall@8=**88%** 维持 Go;③真机抽样回归(req-06 1 轮 PASS);④GitHub Release v0.2.0 已建+dist 双附件补齐(仓库转公开后)。

**P1 八批(布局策略 v2,2026-08-20)**:①**根因数据分析**——721 次 block-apply 审计:504 失败,spacing 400 失败率 70%→600 仅 3%,失败集中在多器件大块(ch340/esp32/充电路径);②**v2 落地**:compile 按上游块保守占位表动态分配格子(大块 2800/中 2200/分立 place 900)+初始 spacing 400→**600**+轮内重试 `_jitter_at`(+350 偏移避开冲突几何);③controller spacing 递增改 600→750→900;④planner 校验前移:upstream_id 抄写截断(req-11 根因)进重试循环;⑤**全量 22 需求 eval:22/22=100%,全部 1 轮**(v1 分布 17×1r+3×2r+2×3r→v2 全 1r,中位/最大轮数双降);⑥Phase 2 方向论证 ADR-0008(排序:自由拓扑受控子集→BOM 成本→sizing 子集;多页/KiCad 挂起)。
