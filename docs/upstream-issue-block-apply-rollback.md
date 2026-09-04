# Issue: `sch block-apply` 报 `failed-rolled-back`(verified=true) 但器件实际留在页面上 —— 回滚校验与真实状态不一致

## 环境

- easyeda-agent CLI/daemon **v0.25.1**(Windows,EasyEDA Pro 3.2.175,连接器 0.25.1)
- 单窗口、单工程、单页(P1),页面刚 `sch clear` 过

## 现象

对一个空页依次执行多个 `sch block-apply` 后,几乎所有块都返回:

```
status: failed-rolled-back
failure: layout verification found N overlap(s) and N pin coincidence(s)
rollback: complete=true verified=true attempted=6 survived=0
```

但事后 `sch read` 显示**这些"已回滚"的器件全部还在页面上**(169 个器件,含全部 6-7 件的块),随后 `sch gate` 四阶段全 **pass**(layout-lint/check/bridge-check/drc)。

即:**manifest 声称 "rollback verified: all newly placed primitive IDs are absent",与页面真实状态矛盾**;而 gate 证明最终布局其实完全合法。

## 稳定复现(两个变体都触发)

### 变体 A:同参数重复 instance

```
easyeda sch clear
easyeda sch block-apply block.esp32s3_wroom1_module --instance esp32_wifi --spacing 400 --at 2600,300 --bind 3V3=3V3 --bind GND=GND --json
# -> applied (正常)
# 同一命令原样再跑一次(同 instance 名):
# -> failed-rolled-back, "6 overlap(s) and 6 pin coincidence(s)"
#    但页面上此时有两份该块的器件(第一份 + "回滚掉的"第二份都可见)
```

### 变体 B:先 place 通道器件再 block-apply

页面先经 `lib search` + `sch place` 放了一个 STM32F103(LQFP48) + 一个 24C02(place 通道,各自坐标),再执行:

```
easyeda sch block-apply block.esp32s3_wroom1_module --instance esp32s3 --spacing 400 --at 2600,300 --bind ...
```

overlap/pin-coincidence 计数恰好等于**该块自身的器件数**(esp32 块 6/6、ch340 块 7/7、ams1117 块 4/4、tactile 块 2/2)——像是块的 6 个器件被与"自身/幽灵"判定重叠。随后 `sch read` 器件都在,`sch gate` 全 pass。

## 关键观察

1. **overlap 计数 = 块自身器件数**的特征指纹暗示布局校验读到的是一份"幽灵几何"(同一批器件被数了两遍,或读了陈旧缓存);
2. **gate 与 block-apply 的布局判定互相矛盾**(同一页,前者 pass 后者 fail);
3. 回滚路径声明 `verified=true` 但器件幸存 —— `schematic.component.delete` 的 read-back 校验可能同样读到了陈旧状态。

## 疑点方向(供参考)

- block-apply 布局校验读 geometry 用的快照可能是 **place 前**或**跨窗口/跨页残留**的陈旧 bbox 缓存;
- EasyEDA Pro 对同一工程开过两个窗口时(我们都关掉了)残留更易复现;单窗口下变体 A/B 仍 100% 复现;
- v0.25.1 的 `sch place --designator` 原子分配与 block-apply 的 designator 分配是否存在竞争。

## 我们的 workaround(已在 jlc-edaloop 落地)

gate pass 后做 substance 复核:`sch read` 回读网表,若 DesignIR 的电源轨全部存在且页面器件数 ≥ 阈值,则信任页面而非 failed 标志。这在我们的迭代闭环里工作正常,但上游若能修复根因(或至少让 rollback verification 说真话)会更好。

## 附件

- 复现序列审计日志:每次失败均带完整 args(见 jlc-edaloop 仓库 `runs/run-230fc00ad1f2/audit.jsonl`)
- 平台:Windows 10/11,daemon 单实例 60832

感谢这个项目,block 库 + gate 体系非常好用。
