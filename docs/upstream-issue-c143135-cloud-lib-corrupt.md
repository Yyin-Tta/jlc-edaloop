# Issue: 特定 LCSC 编号(C143135)云端器件数据疑似损坏 —— `sch place` 稳定挂死(connector did not respond / context deadline),与乱填 uuid 同签名

> 状态:草稿,待用户确认后 `gh issue create`(repo: zhoushoujianwork/easyeda-agent)

## 环境

- easyeda-agent CLI/daemon **v1.1.1**(Windows,EasyEDA Pro 3.2.184,连接器 1.1.1,`connectorVersionOk: true`)
- 单窗口、单工程、单页,EasyEDA 重启前后行为一致(排除连接器旧代码)

## 现象

`lib search --lcsc C143135` **正常返回**(元数据齐全):

```json
{"components":[{"footprintName":"SMA_L4.4-W2.6-LS5.0-RD","lcsc":"C143135",
 "libraryUuid":"0819f05c4eef4c71ace90d822a990e87","manufacturer":"FMS(...)",
 "manufacturerId":"SMAJ5.0A","uuid":"fbafe715cf2a4ad58ba384c7d477da09"}],"count":1}
```

但用该结果 `sch place` **稳定挂死到 context deadline**:

```
ok:false  DISPATCH_FAILED  "connector did not respond"  detail:"context deadline exceeded"
(约 6.2s;同一请求 id 出现在后续响应的 abandonedIds 里,证明连接器侧真的没回)
```

## 对照矩阵(同一页、同一晚、逐项实测)

| 操作 | 结果 | 耗时 |
|---|---|---|
| `lib search --lcsc C143135` | ok, 元数据正常 | 正常 |
| `sch place` C143135(uuid fbafe715…) | **DISPATCH_FAILED 挂死**(探针会话 3 次,后续响应 abandonedIds 可证;eval 内另有多轮) | ~6.2s deadline |
| `sch place` C1979411(Vishay SMAJ5.0A-M3/5A, uuid ab97a3ea…) | ok, D10 摆上 | ~3.4s |
| `sch place` C9900016950(KF301-2P, uuid 0a1dfdf3…) | ok, TERM9 摆上 | ~2.3s |
| `sch place` **乱拼 uuid** | DISPATCH_FAILED 挂死,**同签名** | ~6.2s deadline |

关键指纹:**坏件与乱 uuid 的失败签名完全相同** —— 连接器对这份"查得到元数据、取不到器件数据"的云端条目走了与"uuid 无效"相同的死路,且不是快速报错而是挂到 deadline。

## 时间线(说明这是云端数据当天变坏,非一直如此)

- 该器件所在的块(`block.usbc_ufp_power_or` 的 TVS)在我们 eval 历史里 **成功摆放 ≥25 次**;
- 最后一次成功:**2026-08-22 08:13(本地)**;
- 首次失败:2026-08-22 晚(21:15 本地起的排查会话中稳定复现);
- 期间 easyeda-agent/连接器/EasyEDA Pro 版本均未变化。

即:C143135 的云端器件数据(body/symbol 数据)在 2026-08-22 白天某刻起疑似损坏或下架——`lib search` 的元数据缓存仍在,但 place 时按 uuid 取器件本体数据失败,连接器没有兜底错误路径,表现为挂死。

## 影响

- 任何自动化工作流 place 到该编号都会稳定挂死且**无错误码可编程识别**(只能靠 timeout+同签名比对乱 uuid 才能推断);
- 我们整条 eval 流水线因此 HALT(block-apply 内含该器件时连带失败)。

## 期望

1. 连接器对"元数据可查、器件数据不可得"的云端条目**快速失败并给出明确错误码**(如 `LIB_DATA_UNAVAILABLE` + lcsc/uuid),而不是挂到 context deadline;
2. 若可能,place 挂死时把上游错误(云端返回了什么)透传到 detail,方便区分"云端数据坏"与"uuid 真无效";
3. (附)另一疑似同根问题:多页工程(实测 38 页)下 `sch netlist` 导出超时 "Netlist export returned no file",页删到 5 页内恢复正常——若也是逐页取数无超时兜底,可一并看。

## 我们的 workaround(已在 jlc-edaloop 落地)

skill 的 `references/standard-parts.json` 把 tvs.smaj5v0_sma 换成 Vishay 同规格 C1979411(pins 1=K,2=A 不变),seeds 同步;block-apply 恢复正常。
