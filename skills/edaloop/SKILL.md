---
name: edaloop
description: 嘉立创 EDA 专业版智能原理图设计 agent。当用户需要从需求文档/BOM/datasheet 生成原理图、迭代修复原理图、入库 datasheet、检索电路块库、跑评测或重放设计过程时使用。覆盖 EasyEDA Pro(JLC EDA 专业版)原理图自动化:RAG 检索 + 两段式生成 + 机械校验闭环。
---

# edaloop skill

面向嘉立创 EDA 专业版的开源智能原理图设计 agent:需求 → DesignIR → RAG 检索 → BlockPlan → 真机落图 → 机械校验 → 迭代 → 交付(BOM C 号/网表/审计)。

## 前置条件

- 本仓库已安装(`uv sync`)+ `.env` 配置(`EDALOOP_LLM_KEY`/`EDALOOP_EMBED_KEY`/`EDALOOP_PROJECT`)
- easyeda-agent 四件套就绪(本项目钉 **v0.25.1**),EasyEDA Pro 开启「允许外部交互」,目标工程已打开

## 命令速查

```powershell
# 全链路(需求文件 → 迭代落图 → gate → 交付)
uv run edaloop run <需求.md> [--max-rounds N]

# 分步
uv run edaloop plan <需求.md>              # 只出 BlockPlan
uv run edaloop apply runs/plan-<id>.json   # 落图 + gate
uv run edaloop replay runs/run-<id>        # 审计重放(不重算 LLM)

# 知识库
uv run edaloop seed                        # 种子块重建(68 块)
uv run edaloop retrieve "查询"             # 混合检索调试
uv run edaloop ingest <datasheet.pdf>      # 引脚表+建议提取入库

# 弱门禁确认(需求歧义/未覆盖项)
uv run edaloop questions <需求.md> [--plan runs/plan-<id>.json]

# 评测(w1 检索 88%/w3 迭代闭环 22/22)
uv run edaloop eval --subset w1-retrieval
uv run edaloop eval --subset w3-loop       # 真机,断点续跑
```

## 工作流(意图 → 检索 → 规划 → 落图 → 校验迭代 → 交付)

1. **需求澄清**:跑 `questions` 收集 open_questions 答案;歧义不擅自决定
2. **`run` 一把梭**:上限 5 轮迭代,同错 2 轮自动升级人工(HALT);产物在 `runs/run-<id>/`(audit.jsonl + delivery.svg/net.json)
3. **HALT 处置**:看 `runs/run-<id>/audit.jsonl` 的 `round-validate`/`loop-halt` 事件定位归因;知识库缺口→补块,布局问题→重跑
4. **超纲功能**:知识库无块的功能会如实登记 uncovered(弱门禁),不阻断交付;需要真覆盖时先补 seeds 再 seed
5. **门禁零豁免**:交付判定只信 gate verdict + substance 复核,绝不跳过校验

## 纪律(违反会破坏项目根基)

- LLM/embedding 全走 provider 抽象层,禁止业务代码直连 SDK
- easyeda-agent 版本钉死 v0.25.1,升级须独立验证
- 金标准(evals/)不回退:每次变更必跑 pytest + evals 子集
