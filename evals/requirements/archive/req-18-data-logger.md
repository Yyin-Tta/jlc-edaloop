# 数据记录仪设计要求

> 金标准 eval v1 · 需求样本 18(稳定性批)

## 功能
- 主控 **ESP32-S3-WROOM-1**
- **SD-NAND** 贴片存储(SDMMC 4-bit,焊死抗震)
- **CH340N** 烧录调试
- USB-C 5V ∥ 5V 端子双输入(SS34 防倒灌)
- AMS1117 出 3V3
- 存储/心跳/电源 LED ×3;BOOT/RESET 按键;测试点 3V3/GND/EN

## 网络
- 3V3/5V/GND/SDMMC_*(CLK/CMD/D0-D3)/TX/RX 齐全
