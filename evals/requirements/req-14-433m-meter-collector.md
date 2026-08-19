# 433MHz 无线抄表集中器设计要求

> 金标准 eval v1 · 需求样本 14(稳定性批)

## 功能
- 主控 **STM32F103C8T6**(SPI)
- **CC1101** 433MHz 收发前端(IPEX 天线)
- **RS-485**(SP3485)下联电表
- USB-C 5V 供电(CH340N 烧录),AMS1117 出 3V3
- **SD-NAND** 贴片存储(SDMMC 4-bit)
- RF/总线/电源 LED ×3,BOOT/RESET 按键,SWD 排针

## 网络
- 3V3/5V/GND/SPI_*(CSN/SCK/MOSI/MISO/GDO0/GDO2)/A/B/DE_RE 齐全
