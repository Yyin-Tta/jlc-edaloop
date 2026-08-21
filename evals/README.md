# evals 金标准集 v2

14 份需求文档（三层难度）+ 10 份 datasheet。

2026-08 重设计：26 → 14（每个需求实测 ≈4 分钟，全量 26 个 ≈104 分钟太久）。
**编号即难度顺序**：01–04 简单 / 05–09 中等 / 10–14 困难；各文件头部标注难度层与原编号。
裁撤的 12 个移入 `requirements/archive/`（保留旧编号），理由与覆盖替代者见 `archive/README.md`。

## requirements/（14 份，按难度编号）

### 简单 easy 01–04 — 单电源域 / 小 BOM / 块直命中 / 预期 1 轮 ≈16min

| # | 文件 | 覆盖点 |
|---|------|--------|
| 01 | `req-01-esp32s3-mini-2layer.md` | ESP32-S3+CH340N+AMS1117+USB-C，2 层，真实客户源基线，测试点/丝印/天线禁区 |
| 02 | `req-02-rs485-sensor-hub.md` | STM32F103+SP3485+24C02+CH340N+SWD，RS-485 最小形态（原 06） |
| 03 | `req-03-ir-remote-hub.md` | ESP32+IR 收发+24C02，最简外设组合（原 16） |
| 04 | `req-04-smart-dial-oled.md` | 超纲探边界：OLED+触摸 → uncovered 登记行为（原 22） |

### 中等 medium 05–09 — 多块组合 / 电源树≥2 级 / 自由拓扑 / refine 歧义 ≈20min

| # | 文件 | 覆盖点 |
|---|------|--------|
| 05 | `req-05-battery-ble-telemetry.md` | TP4056+MT3608+AMS1117 三级电源树 + TL431 低压告警 + 18650（原 07） |
| 06 | `req-06-pico-native-usb-gateway.md` | 原生 USB（无 CH340 路径）+ microSD SPI + INA226 + SS34 防倒灌（原 13） |
| 07 | `req-07-motor-driver-board.md` | ULN2003 双通道步进 + 宽压 XL1509 降压 + 8 路输出 LED（原 15） |
| 08 | `req-08-liion-protection-freeform.md` | 自由拓扑分解（DW01A+FS8205A，无 MCU）（原 23） |
| 09 | `req-09-ambiguous-sensor-node.md` | 强歧义：refine 提问-答复-重规划闭环（原 26） |

### 困难 hard 10–14 — 隔离 / 叠层 / 双 MCU / 母板 / 端到端 ≈20min

| # | 文件 | 覆盖点 |
|---|------|--------|
| 10 | `req-10-esp32s3-industrial-4layer.md` | 4 层叠层（L2 GND / L3 3V3）+ RS-485 预留 + 双色 LED（原 02） |
| 11 | `req-11-isolated-dido-module.md` | B0505S 隔离电源 + PC817×4 + ULN2003 + GND_ISO 分区；历史难例（唯一曾 2 轮收敛）（原 08） |
| 12 | `req-12-hybrid-dual-mcu-gateway.md` | STM32+ESP32 双 MCU 双电源域，35–50 器件，UART 桥+EEPROM+SWD（原 05） |
| 13 | `req-13-env-sensor-motherboard.md` | 4×2x8 插槽母板 + I2C 总线隔离（2N7002DW）+ TVS，多连接器布局（原 19） |
| 14 | `req-14-door-sensor-e2e.md` | 宽压 XL1509+A3144+SP3485，需求→原理图→PCB→订单端到端全链（原 27） |

## 覆盖矩阵

| 维度 | 覆盖者 |
|------|--------|
| 主控：ESP32 模组 / STM32 / 双 MCU / 无 MCU | 01·03·04·05·06·10 / 02·07·09·11·12·13·14 / 12 / 08 |
| 电源：单 LDO / 充电+升降压树 / 宽压降压链 / 隔离电源 / 双域防倒灌 | 01·02·03·04 / 05 / 07·14 / 11 / 06·12 |
| 通信：USB-C+CH340 / 原生 USB / RS-485 / I2C / SPI / IR | 01·02 / 06 / 02·10·14 / 03·06·13 / 06 / 03 |
| 布局：2 层 / 4 层 / 隔离分区 / 母板多连接器 | 01 / 10 / 11 / 13 |
| 行为通道：block-apply / freeform / uncovered 登记 / refine 歧义 / e2e 订单 | 全部 / 08 / 04 / 09 / 14 |
| 历史难例 | 11 |

## 与回归级的关系（`--tier`）

| 层级 | 成员 | 时长 |
|------|------|------|
| `easy` / `medium` / `hard` | 01–04 / 05–09 / 10–14 | ≈16 / 20 / 20 min |
| `smoke`（改动最快验证） | 01（易·block 基线）+ 08（中·自由拓扑）+ 11（难·隔离难例） | ≈12 min |
| `daily`（常规 PR） | easy 全 4 + 05 + 08 + 09 + 11 = 8 个 | ≈32 min |
| `rest`（发版增量） | 全量 − daily = 6 个（06·07·10·12·13·14） | ≈24 min |
| `all`（真全量重跑） | 14 个 | ≈56 min |

smoke/daily 选取原则延续：覆盖两通道（block-apply + freeform）、历史难例（隔离）、refine 歧义。

## datasheets/（10 份，全部校验 %PDF magic）

| 文件 | 器件 | 来源 |
|------|------|------|
| `esp32-s3_datasheet_en.pdf` | ESP32-S3 SoC | espressif.com 官方 |
| `esp32-s3-wroom-1_wroom-1u_datasheet_en.pdf` | ESP32-S3-WROOM-1 模组 | espressif.com 官方 |
| `CH340C_wch.pdf` | CH340 系列 USB 转串口（含 N/C/G 等变体） | LCSC C84681（WCH 官方 datasheet 镜像） |
| `AMS1117_ds1117.pdf` | AMS1117 LDO | advanced-monolithic.com 官方 |
| `stm32f103c8.pdf` | STM32F103C8 | st.com 官方 |
| `TP4056.pdf` | TP4056-42-ESOP8 锂电充电 | LCSC C16581（TopPower 官方镜像） |
| `MT3608_aerosemi.pdf` | MT3608 升压 | Aerosemi（Olimex 镜像） |
| `PC817.pdf` | PC817 光耦 | components101 镜像 |
| `MAX485_MAX1487-MAX491.pdf` | MAX485 RS-485 收发 | Maxim/ADI 官方 |
| `ULN2003A_ti.pdf` | ULN2003A 达林顿阵列 | TI 官方 |

### 下载备注（踩坑记录）

- Mouser/LCSC 表层 URL 均反爬（返回 HTML）：LCSC 需解析 `lcsc.com/datasheet/...` 预览页内嵌的 `datasheet.lcsc.com/datasheet/pdf/<hash>.pdf?productCode=<C号>` 直链（带 Referer 可下载）。
- WCH 官网（wch.cn）为 Vue SPA，文件 API `api1.wch.cn/api/official/website/common/downloadFile` 返回权限错误，放弃；改走 LCSC 镜像。
- SparkFun cloudfront（TP4056 常用镜像）已 502，且 Wayback 快照同样捕获的是 502 页，不可用。
- components101 为可用的社区镜像（PC817）。
