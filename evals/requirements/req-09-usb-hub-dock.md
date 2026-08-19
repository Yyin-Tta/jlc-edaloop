# USB 四口扩展坞核心板设计要求

> 金标准 eval v1 · 需求样本 09(稳定性批)

## 功能
- **CH334F** USB2.0 四口 hub(自供电,12MHz 晶振)
- 上行 **USB-C**(双取向数据),下行 4 个 USB-C 座
- 外部 **5V/2A 端子**供电,各下行口 **SS34** 防倒灌 + 电源 LED
- **USBLC6-2SC6** ESD 保护上行口
- 过流指示 LED ×1(预留)

## 网络
- 5V/GND/HOST_DP/HOST_DM/DN1-4_DP/DN1-4_DM 齐全
