# 双模网关设计要求(ESP32 PICO 原生 USB 版)

> 金标准 eval v1 · 需求样本 13(稳定性批)

## 功能
- 主控 **ESP32-S3-PICO-1-N8R8**(原生 USB 下载,无 CH340)
- USB-C 数据+供电双角色
- **microSD** 卡座(SPI 存数据)
- **INA226** 电源监测(USB 电流)
- 5V 端子备用输入(SS34 防倒灌),AMS1117 出 3V3
- 电源/USB 活动/存储 LED ×3,BOOT/RESET 按键

## 网络
- 3V3/5V/GND/USB_DP/USB_DM/SD_*/SDA/SCL 齐全
