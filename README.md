# jlc-edaloop

面向嘉立创 EDA 专业版的开源智能原理图设计 agent:解析用户意图 → RAG 检索知识库(datasheet/已验证案例/器件库)→ LLM 两段式生成原理图(块选择 → 块落图)→ 机械校验门禁把关,不满足则**归因反馈迭代重生成**,直至可交付。

```
需求文档/BOM/datasheet
  → DesignIR(M1 意图层)
  → 混合检索(M2:dense + BM25 + 型号加分 + rerank,四通道 RRF)
  → BlockPlan(M3a:GLM 规划) → typed actions(M3b:确定性编译)
  → EasyEDA Pro 真机落图(easyeda-agent CLI,block-apply / place+autoconnect 双通道)
  → 强门禁(M4:连通性/layout/DRC/pin 回读)+ 弱门禁(人工确认队列)
  → 不满足 → 归因反馈(REPLAN/RELAYOUT/...) → 迭代(上限 5 轮,同错 2 轮升级人工)
  → 满足 → 交付(BOM C 号/网表/审计日志)+ 案例回写知识库
```

## 差异化

1. **开源 + 可本地部署**(LLM/embedding 全走 OpenAI 兼容抽象层,可切本地端点);
2. **嘉立创/LCSC 供应链原生**(BOM 带 C 号,`lib search` C 号精确选型);
3. **可机械复验的校验闭环**(loop 是核心:每轮 gate 报告 + 结构化 Finding + 审计日志全程留痕);
4. **案例自进化**(通过门禁的交付回写 `seeds/cases.jsonl`)。

PoC 战绩(W0-W4,2026-08):检索 recall@5=88%;真机落图 5 需求 ≥3 过 `sch gate`;迭代闭环 pass@3=80%/pass@5=80%;datasheet 管道 ULN2003A 16/16 脚双通道校验;含库外器件的 showcase 零手工 2 轮收敛。

## 安装

前置:Python 3.12+、[uv](https://docs.astral.sh/uv/)、Windows + EasyEDA Pro(开启「设置 → 系统 → 允许外部交互」)、[easyeda-agent](https://github.com/zhoushoujianwork/easyeda-agent) 四件套(本项目钉 **v0.25.1**,见 `pyproject.toml [tool.edaloop]`)。

```powershell
uv sync
Copy-Item .env.example .env   # 填入 EDALOOP_LLM_KEY / EDALOOP_EMBED_KEY
uv run edaloop seed           # 种子块库入库(含向量索引)
uv run edaloop --help
```

环境变量(.env):`EDALOOP_LLM_*`(GLM/DeepSeek/任意 OpenAI 兼容端点)、`EDALOOP_EMBED_*`(硅基流动 BGE-M3,接口抽象可换本地)、`EDALOOP_PROJECT`(EasyEDA 工程名路由)。

## 使用

```powershell
# 知识库
uv run edaloop seed                              # 种子块重建
uv run edaloop retrieve "TP4056 锂电充电"         # 混合检索调试

# datasheet 入库(M6:证据页→双通道提取→交叉校验)
uv run edaloop ingest path/to/datasheet.pdf

# 设计闭环(M1→M5:需求文件 → 迭代落图 → gate)
uv run edaloop run evals/showcase/showcase-uln2003.md

# 分步
uv run edaloop plan evals/requirements/req-01-....md   # 只出 BlockPlan
uv run edaloop apply runs/plan-<id>.json               # 落图 + gate

# 弱门禁:需求歧义/未覆盖项确认队列
uv run edaloop questions evals/requirements/req-04-....md --plan runs/plan-<id>.json

# 评测
uv run edaloop eval --subset w1-retrieval   # 检索 recall@5
uv run edaloop eval --subset w3-loop        # 迭代收敛(真机,断点续跑)
```

每轮迭代的输入 IR/检索命中/BlockPlan/findings/修复动作全量落 `runs/run-<id>/audit.jsonl`。

## 仓库结构

```
src/edaloop/
  intent/    # M1 需求 → DesignIR
  knowledge/ # M2 四通道混合检索 + 案例回写
  generate/  # M3 planner/compiler/adapter/audit
  validate/  # M4 强/弱门禁 → 结构化 Finding
  loop/      # M5 归因 + 迭代控制器(防震荡)
  ingest/    # M6 datasheet 管道
seeds/        # 种子块库(含 LCSC 回读验证的 pinout)+ 案例库
evals/        # 金标准集(5 需求 × 10 datasheet)+ 评测脚本
```

## 边界与非目标

- 只到原理图+网表,PCB 布局布线交棒 easyeda-agent;
- 扫描件 datasheet 不支持(文字型 PDF);规则通道解析不了的排版降级为 low-confidence 单通道入库;
- 平台墙(无 undo 等)沿用 easyeda-agent 踩明的 workaround,失败补偿 = 显式删除残留 + 重放。

## 致谢

站在 [easyeda-agent](https://github.com/zhoushoujianwork/easyeda-agent)(CLI/daemon/连接器/电路块库/sch gate)与嘉立创 EDA Pro 官方 `eda.*` API 的肩膀上;本项目只做薄而规矩的消费方。License: 见 [LICENSE](LICENSE)。
