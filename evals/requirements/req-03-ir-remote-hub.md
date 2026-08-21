# 红外遥控中枢设计要求

> 金标准 eval v1 · 需求样本 16(稳定性批)

## 功能
- 主控 **ESP32-S3-WROOM-1**
- **VSOP38338 接收 + IR928 发射**红外遥控收发单元(5V 供电)
- USB-C 5V 供电,AMS1117 出 3V3
- 学习模式/发射活动/电源 LED ×3
- BOOT/RESET 按键;**24C02** 存红外码库

## 网络
- 3V3/5V/GND/IR_RX/IR_TX/SDA/SCL 齐全
