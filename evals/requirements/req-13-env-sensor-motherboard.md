# 环境采集多节点母板设计要求

> 金标准 eval v1 · 需求样本 13(稳定性批)
> 难度层：困难（v2 重设计 2026-08，26→14 分层）；原编号 19

## 功能
- 主控 **STM32F103C8T6**(中央),4 个子节点插槽 **2x8 排针**(各:3V3/GND/SDA/SCL/INT1/INT2/AIN/GPIO)
- **I2C 总线隔离**(2N7002DW 双 NMOS,热插拔保护)
- **RS-485**(SP3485)外联
- USB-C 5V 供电,AMS1117 出 3V3;TVS+保险丝入口保护
- 每插槽在位检测 LED ×4 + 电源/总线 LED ×2;BOOT/RESET;SWD 排针

## 网络
- 3V3/5V/GND/SDA/SCL(隔离前后 SDA_A/SDA_B)/A/B/INT1-4 齐全
