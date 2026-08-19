# 桌面小站底板设计要求(超纲探边界:CAN 收发)

> 金标准 eval v1 · 需求样本 20(超纲批:含知识库无对应块的 CAN 通道)

## 功能
- 核心 **ESP32-S3-WROOM-1** 模组 + **CH340N** 烧录
- USB-C 5V 供电,AMS1117 出 3V3
- **一路 CAN 2.0**(收发器如 TJA1051/SN65HVD230,知识库若无则如实登记 uncovered)
- 电源/用户 LED ×2,BOOT/RESET 按键
- 测试点 3V3/GND/EN

## 网络
- 3V3/5V/GND/CAN_TX/CAN_RX/CAN_H/CAN_L 齐全(或 CAN 通道入 uncovered)
