# 车载 GPS+GPRS 追踪器设计要求

> 金标准 eval v1 · 需求样本 10(稳定性批)

## 功能
- 主控 **STM32F103C8T6**,**LC29H** 双频 GNSS 模组(UART+PPS)
- 车载 12-24V 宽压输入(防反接+TVS+保险丝),TPS54360 降压 5V,AMS1117 出 3V3
- **ACC 点火检测**光耦隔离输入
- **INA226** 电池电流监测(I2C)
- 备用锂电 **TP4056** 充电 + 理想二极管切换
- GPS/网络/电源 LED ×3,BOOT+RESET 按键

## 网络
- 3V3/5V/VBAT/IGN/GND/GNSS_TX/GNSS_RX/PPS/SDA/SCL 齐全
