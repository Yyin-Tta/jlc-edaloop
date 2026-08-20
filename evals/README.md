# evals 金标准集 v0

5 份需求文档 + 10 份 datasheet。

## requirements/（5 份）

| 文件 | 类型 | 说明 |
|------|------|------|
| `req-01-esp32s3-mini-2layer.md` | 改写 ×1 | 源自 [easyeda-agent esp32MiniRequire.md](https://github.com/zhoushoujianwork/easyeda-agent) · 变体 A：2 层经济版（USB-C + CH340N + AMS1117 + 测试点） |
| `req-02-esp32s3-industrial-4layer.md` | 改写 ×2 | 同源 · 变体 B：4 层工业版（对齐原版叠层 + RS-485 预留 + 双色 LED） |
| `req-03-power-board.md` | 自写 | 电源板：12-24V 输入 / TP4056 锂电 / MT3608 升压 5V / AMS1117 3V3 三轨 |
| `req-04-interface-board.md` | 自写 | 接口板：MAX485 + PC817 隔离 + ULN2003 驱动 + B0505S 隔离电源 |
| `req-05-hybrid-dual-mcu-gateway.md` | 自写 | 混合板：STM32F103C8T6 + ESP32-S3-WROOM-1 双 MCU 网关 + CH340 + 24C02 |

原始需求文档快照：见仓库 `D:\gyt-pro\easyeda-agent`（空壳）→ 实际原文取自
`https://raw.githubusercontent.com/zhoushoujianwork/easyeda-agent/main/esp32MiniRequire.md`（32 行，客户口吻）。

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
