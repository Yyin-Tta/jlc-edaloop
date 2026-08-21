# 智能表盘设计要求(超纲探边界:OLED + 电容触摸)

> 金标准 eval v1 · 需求样本 22(超纲批:OLED 屏与触摸 IC 知识库未覆盖)

## 功能
- 主控 **ESP32-S3-WROOM-1**
- **0.96" I2C OLED**(SSD1306 类)显示
- **电容触摸**(TTP223 类)按键 ×2
- USB-C 5V 供电,AMS1117 出 3V3;**24C02** 存界面配置
- 背光/触摸反馈/电源 LED ×3;BOOT/RESET 按键

## 网络
- 3V3/5V/GND/SDA/SCL/OLED_RST/TP1/TP2 齐全(OLED/触摸可 uncovered)
