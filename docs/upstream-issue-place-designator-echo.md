# Issue: `sch place --designator` 回显请求名,而平台静默改号后真实落名不同 —— 回包 designator 不可信

## 环境

- easyeda-agent CLI/daemon **v0.25.1**(Windows,EasyEDA Pro 3.2.175,连接器 0.25.1)
- 单窗口、单工程、两页(P1/P2),P1 已有同名器件,P2 为空页

## 现象

P1 上已有器件 `ULN2U1`(试放阶段落的)。向 P2 落放并显式指定位号:

```
easyeda sch place --lib L --uuid U --x 255 --y 110 --designator ULN2U1 --doc P2
# 回包:{"result": {"component": {"designator": "ULN2U1"}}}
```

回包说 `ULN2U1`,但回读页面:

```
easyeda sch clusters --json --doc P2
# designator = "ULN2U2"   ← 平台对全工程重名静默 +1,真实落名变了
```

即:**EasyEDA 对全工程重复位号会静默改号(+1 顺延),而 place 回包回显的是请求名,不是落地名**。调用方按回包名字去 `sch autoconnect --pin ULN2U1:1` → `no pin found`;按回包名字去 clusters 回查实测框 → miss。

同轮实测的姊妹案例:`CLDOOUT2`(P1)→ P2 落成 `CLDOOUT3`,回包仍说 `CLDOOUT2`。

## 根因定位(extension/src/actions.ts schematicComponentPlace)

```ts
modified = await eda.sch_PrimitiveComponent.modify(primitiveId, { designator });
component = modified;                 // ← 用 modify 的返回对象序列化回包
return { result: { component: serializeComponent(component) } };
```

`modify` 返回的对象**回显请求值**(与 getState_Rotation 在 create 后回显输入是同一族平台行为,CLAUDE.md 已有记载:"immediately after create can echo the input — a fresh re-pull (getAll) shows the real stored value")。位号赋值撞上平台去重时,回显值 ≠ 存储值。

## 建议修法

`modify` 赋位号后不要信返回对象,用 `getAll()` 重拉一次该 primitiveId,以**存储值**序列化回包;若存储值 ≠ 请求值,回包加一个显式字段(如 `designatorAssigned: true, requested: "ULN2U1"`),让调用方能区分"按请求落名"与"被平台改号"。或者至少在 warnings 里带上 `designator renamed by platform: ULN2U1 -> ULN2U2`。

另一面的坑也值得记进文档:调用方若依赖"请求名=落名"的假设,在多页共存(试放页+正式页)场景必翻车——我们控制器侧已改为按落放坐标从页面回读认领真实位号,不依赖回包。

## 复现要点

1. 单工程两页,P1 落一个 `--designator X1N1` 的器件;
2. P2 落同名 `--designator X1N1`;
3. 对比 place 回包的 `component.designator` 与 `sch clusters --doc P2` / `sch list --page P2` 的位号——不一致即复现。
