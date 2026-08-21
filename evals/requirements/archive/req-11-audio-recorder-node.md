# 音频录音节点设计要求

> 金标准 eval v1 · 需求样本 11(稳定性批)

## 功能
- 主控 **ESP32-S3-WROOM-1**(I2S 音频)
- **ES8311** codec(I2C 控制+I2S 数据)+ **MEMS 模拟麦克风**
- **AW8737** 功放接 4Ω 喇叭(可选 DNP)
- USB-C 5V 供电,AMS1117 出 3V3;**microSD** 卡座存录音(SPI)
- 录音状态/存储活动/电源 LED ×3;BOOT/RESET 按键

## 网络
- 3V3/5V/GND/I2S_*(MCLK/BCLK/LRCK/DIN/DOUT)/SDA/SCL/SD_* 齐全
