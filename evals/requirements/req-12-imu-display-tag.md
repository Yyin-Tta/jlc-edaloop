# 姿态监测标签设计要求

> 金标准 eval v1 · 需求样本 12(稳定性批)

## 功能
- 主控 **ESP32-S3-WROOM-1**
- **BMI270** 六轴 IMU(I2C,INT 中断)
- **SPI TFT 屏接口**(ST7789,BTB 连接器)显示姿态
- USB-C 5V 供电,AMS1117 出 3V3
- 状态 LED ×2(电源/运动检测),BOOT/RESET 按键
- **24C02** 存校准参数

## 网络
- 3V3/5V/GND/SDA/SCL/INT/LCD_*(CS/SCK/MOSI/DC/RST/BL) 齐全
