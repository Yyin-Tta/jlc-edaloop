# 锂电供电 BLE 遥测板设计要求

> 金标准 eval v1 · 需求样本 05(稳定性批)
> 难度层：中等（v2 重设计 2026-08，26→14 分层）；原编号 07

## 功能
- **ESP32-S3-WROOM-1** 主控,锂电供电
- **TP4056** 充电(USB-C 5V 输入,充电状态双 LED)
- **MT3608** 升压 5V 给外部传感器,AMS1117 出 3V3 给模组
- 18650 电池座;**TL431** 低压告警红 LED
- 用户 LED 蓝色 ×1,BOOT/RESET 按键
- I2C 扩展排针 **2x4**(3V3/GND/SDA/SCL/INT/备用×2)

## 网络
- 3V3/5V/BAT/GND/CHG_OK/LED_USER/SDA/SCL 齐全
