# archive：v2 金标准重设计裁撤件

2026-08 重设计（26 → 14，分简单/中等/困难三层）时裁撤的需求。本目录不被 `evals_w3._REQ_DIR.glob("req-*.md")`
扫描（非递归），自动退出回归；如需复活，`git mv` 回上级目录并加入 `_TIER` 对应层即可。

**注意**：本目录文件保留**旧编号**（v2 重设计前）；现行 14 件已按难度重编号 01–14（01–04 简单 / 05–09 中等 /
10–14 困难），对应关系见 `evals/README.md`。下表"覆盖替代者"一列使用**新编号**。

裁撤原则：功能/行为与保留件重叠者优先裁；独有功能域但历史稳定 1 轮 PASS、区分度低者次之。

| 文件（旧编号） | 原批次 | 裁撤理由 | 覆盖替代者（新编号） |
|------|--------|----------|-----------|
| `req-03-power-board.md` | v0 自写 | 与 05 同一条 TP4056+MT3608+AMS1117 电源树 | req-05 |
| `req-04-interface-board.md` | v0 自写 | 隔离布局与 11 重叠（11 还多 B0505S 隔离电源） | req-11；RS-485 三件套由 02/10/14 部分覆盖 |
| `req-09-usb-hub-dock.md` | v1 稳定性 | CH334F hub 为独有 USB 拓扑，但 USB-C 差分已由 01/10 覆盖 | req-01/10 |
| `req-10-vehicle-gps-tracker.md` | v1 稳定性 | 大杂烩板，各子功能均有专件覆盖 | 14(宽压+A3144)、05(TP4056)、12(防倒灌) |
| `req-11-audio-recorder-node.md` | v1 稳定性 | I2S 音频为独有域但历史稳定，区分度低 | 无（I2S 域暂时让位，需要时可复活） |
| `req-12-imu-display-tag.md` | v1 稳定性 | SPI 显示接口与 06(microSD SPI) 重叠 | req-06 |
| `req-14-433m-meter-collector.md` | v1 稳定性 | CC1101 RF 为独有域，其余(SPI/SD/RS-485)均有覆盖 | 无（RF 域暂时让位）；SPI→06、RS-485→02 |
| `req-17-power-monitor-switch.md` | v1 稳定性 | 继电器+INA226+RS-485 组合，子功能均有专件 | 07(ULN2003 驱动)、06(INA226)、02(RS-485) |
| `req-18-data-logger.md` | v1 稳定性 | SDMMC 存储与 06(microSD) 重叠，双输入防倒灌与 06/12 重叠 | req-06 |
| `req-20-desk-station-can.md` | v1 超纲 | CAN uncovered 行为与 04(OLED/触摸) 同类 | req-04 |
| `req-21-industrial-temp-controller.md` | v1 超纲 | 热电偶 uncovered 行为与 04 同类 | req-04 |
| `req-25-highside-switch-freeform.md` | v2 自由拓扑 | 自由拓扑通道由 08(DW01A+FS8205A) 代表 | req-08 |
