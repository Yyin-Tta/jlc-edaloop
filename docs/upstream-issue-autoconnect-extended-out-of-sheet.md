# Issue: autoconnect 扩展档位把桩线铺出图外 —— 候选枚举无「图可用区」概念,`--offset-max` 也拦不住

## 环境

- easyeda-agent CLI/daemon **v0.25.1**(Windows,EasyEDA Pro 3.2.175,连接器 0.25.1)
- 单页原理图,A4 图框,器件位于图框内;触发场景:紧凑布局下同侧引脚密集(ULN2003A 16 脚并排,GND/VIN 在顶行)

## 现象

对位于 y≈195 的引脚跑 `sch autoconnect`(无 spec、默认 rules),产生的 GND/VIN
netflag 桩线**向下**伸出 190/195,marker 锚落到 y=-26/-10 —— 图框 usable 区底边是
y=12,标记整体在图外:

```
ULNST4 pin8 (GND): stub (865,195)→(865,5),   marker text y∈[-26,-4]
ULNST4 pin9 (VIN): stub (945,195)→(945,0),   marker text y∈[-26,-4]
sch clusters 报: out-of-sheet ULNST4: y -26 < 12, ERROR
```

后果不止是视觉:clusters 把出图 marker 仍归属给该器件,组包围盒纵向从 ~95
膨胀到 ~304,下游按实测框装箱的布局器拿到的是假尺寸。

同页同排的姊妹器件 ULNST3(相同封装、相同引脚排布,只是周边 marker 密度略低)
同样的脚落在 y≈[184,207] 的图内拐角处 —— 说明这是**密集触发**的路径,不是必然。

## 根因定位(cmd_sch_autoconnect.go)

三条链叠出来:

1. **触发无界**:`planConnection` L798 —— `noCleanCandidate(all) || laneFloor > rules.OffsetMax`
   就把 `extendedOffsets` 铺进来。密集场景(每个常规候选都撞 marker)是它的设计
   场景,合理;但铺出来的上界是 `max(3×OffsetMax, laneFloor+step)`(L857-859)
   —— **只跟 lane 压力走,没有「图还有多少可用空间」的输入**。laneFloor 本身可以
   被之前的桩推得很高,雪球式越推越远。

2. **`--offset-max` 拦不住**:CLI flag 只写进 `rules.OffsetMax`,而 `candidateOffsets`
   常驻「标准档位」min+k·laneStep(netport 一档 ~89,三档铺到 ~285),extendedOffsets
   又从 OffsetMax+step 起步 —— 两处都在 OffsetMax 之上。真上界只有 `rules.OffsetCap`
   (默认 0=不设限),而它**既不在 CLI flag 里,也不在 spec JSON 里**:`acSpecRules`
   只认 `offsetRange`/`offsetStep` 等键,调用方若按 `autoconnectRules` 的字段名传
   `offsetMin`/`offsetMax`/`offsetCap`,Go 反序列化**静默丢弃未知键**,实际跑的是
   默认 18..80 —— 回包 rc=0 一切正常,调用方完全无感。实测(run-fd4bb14b4ee6):
   spec 传 `{"offsetMin":18,"offsetMax":40,"offsetCap":40}`,落点 down/**54**
   (=默认细档 18+6×6),与不传 rules 一模一样。建议:(a) spec 支持 offsetCap 或
   至少加 CLI `--offset-cap`;(b) spec rules 对未知键告警而不是静默吞。

3. **平分时方向按字典序**:`sort.SliceStable` 的 tie-break 是 `Direction < Direction`
   (L805-806),四方向枚举序 `down < left < right < up` —— 平分时**向下优先**。
   顶行器件的下方是器件本体+图外,上方是图内空旷区,平分天然吃亏。

## 建议修法(按侵入性排序,可全做)

1. **scene 里带上 sheet usable 区**(clusters 已有 `sheetUsable` 口径,同一份数据
   接进来):候选落点(marker 锚 ± 文字翼展)越出 usable 区 → 硬拒绝,与「穿件/
   触异网线」同一档。这一条堵死「出图」这个最坏的结局,密集兜底的语义不变 ——
   图内真没有位置时,extendedOffsets 照旧能在图内走远。
2. **`--offset-cap` 提到 CLI**:spec 已有 `rules.offsetCap` 且三处枚举共用
   `acCapOffsets` 一把闸(L868-869),CLI 只差一个 flag 透传。密集场景调用方
   就能说「桩再长也不能超过 X」。
3. **方向 tie-break 换成空间启发式**:平分时优先**远离图边**的方向(或至少别让
   字典序固定偏向 down)。

## 复现要点

1. 单页 A4 图框,底部留 ~200 的图内空区;
2. 放一个 16 脚并排器件(如 ULN2003A)在页面上部,顶行两脚接 GND/VIN;
3. 在它周边预置足够的同侧 marker(或直接并排放两三个同类器件)把常规候选全部
   挤脏;
4. `sch autoconnect --pin D:8 --net GND`(默认 rules);
5. 回读 `sch clusters --json` —— marker 锚 y<12 且 `out-of-sheet ... ERROR` 即复现。
