# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

开源智能原理图设计 agent,面向嘉立创 EDA 专业版(EasyEDA Pro):

```
需求文档 → DesignIR(M1 intent) → 混合检索(M2 knowledge:五通道 dense+BM25+型号+rerank)
  → BlockPlan(M3a LLM 规划) → typed actions(M3b 确定性编译) → easyeda-agent CLI 真机落图
  → 校验门禁(M4 validate:强=fail/block,弱=人工确认队列)
  → 不满足 → 归因反馈(M5 loop) → 迭代重生成(上限 5 轮,同错 2 轮 HALT 升级人工)
  → 满足 → 交付(BOM C 号/网表/审计日志)+ 案例回写知识库
```

Python 3.12 + uv;easyeda-agent(Go,经 CLI 子进程消费,零代码级耦合)负责 daemon/连接器/`eda.*` API 交互。本项目只做「薄而规矩的消费方」。

**语言约定:文档、提交信息、DEVELOPMENT.md 全部用中文。**

## 多仓布局(易踩坑,先读)

- `docs/` 和 `runs/` 是**嵌套私有 git 仓**(主仓 .gitignore 掉,绝入不了 PUBLIC 主仓),双机同步用 `bash docs/sync.sh "提交说明"`(幂等,开工/收工各跑一次)。
- `docs/DEVELOPMENT.md` 是**唯一开发依据(living document)**:任何影响架构/接口/里程碑的决策,**先改它再写代码**,并在文末变更记录追加一行。ADR 在 `docs/adr/`。
- `easyeda-agent/` 是上游的本地克隆(gitignored,永不入库);其自带 CLAUDE.md 只管那个仓。
- `seeds/`(块库/案例/标准件)与 `evals/`(金标准)是回归基线,**不回退**。

## 常用命令

```bash
uv sync                          # 安装(core 依赖零 UI 框架;UI 走 uv sync --extra ui)
uv run pytest                    # 全量测试(350 个,离线,用 llm/fake.py 假 provider)
uv run pytest tests/test_loop.py -k test_name    # 单文件/单测
uv run pytest tests/test_loop.py::test_x        # 精确单测

uv run edaloop seed              # 种子块库入库(含向量索引)——首次必跑,产物 runs/knowledge.db
uv run edaloop retrieve "查询"    # 混合检索调试
uv run edaloop plan <需求.md>     # M3a:只出 BlockPlan
uv run edaloop apply runs/plan-<id>.json   # M3b:落图 + gate(真机)
uv run edaloop run <需求.md>      # 全链路迭代闭环(真机);--dry-run 无 EasyEDA 也可跑
uv run edaloop questions <需求.md>          # 弱门禁确认队列
uv run edaloop replay runs/run-<id>         # 按审计日志重放落图(不重算 LLM)

uv run edaloop eval --subset w1-retrieval    # 检索 recall(离线)
uv run edaloop eval --subset w3-loop --tier smoke   # 真机回归;tier: easy/medium/hard/smoke(3个)/daily(8个)/rest/all;electrical/params/refine 三 tier 离线不走 E2E

uv run --extra ui edaloop ui     # Chainlit Web UI(127.0.0.1:8000)
```

真机路径前置:Windows + EasyEDA Pro 开启「设置→系统→允许外部交互」+ 目标工程已打开 + easyeda 四件套(CLI=daemon=connector 同版)。

## 架构(src/edaloop/)

| 模块 | 职责 |
|---|---|
| `intent/` | M1:需求 → `DesignIR`(全链路意图真值;歧义产 open_questions) |
| `knowledge/` | M2:sqlite-vec 单文件库;混合检索五通道(blocks/datasheet/case…);案例回写带三护栏 |
| `generate/` | M3:`plan.py`(LLM 出 BlockPlan)→ `compile.py`(确定性编译 typed actions,布局/分页/走线几何全在这)→ `adapter.py`(easyeda CLI 子进程 + 版本门)→ `pipeline.py`(编排);`freeform.py` 无块兜底;`sizing.py`/`stdparts.py` 参数选值;`pcb.py`/`bomcost.py` M8/M9 编排 |
| `validate/` | M4:`checks.py` 全部强/弱门禁(check_voltage_compat / check_current_budget / check_rails / check_func_covered / check_param_off_spec / check_gauge…)→ 结构化 `Finding` |
| `loop/` | M5:`controller.py` 迭代控制器(MAX_ROUNDS=5,同错 2 轮 HALT;几何修复如 _route_pin_pair 也在 controller);`attribution.py` 归因;`critic.py` 复核 |
| `ingest/` | M6:datasheet PDF → 证据页 → 双通道提取 → 交叉校验入库 |
| `llm/` | provider 强制抽象层:`base.py` 协议 / `openai_compat.py` 真实现 / `fake.py` 测试假件 |
| `ui/` | Chainlit 会话层:`session.py` 纯逻辑(不 import chainlit,可独立测)+ `app.py` 薄适配(ADR-0012) |
| `cli.py` | M7:argparse 子命令编排 + 审计 |
| `evals_*.py` | 评测脚本(w1 检索 / w3 真机闭环 / electrical / params / refine) |

关键数据契约(字段名冻结,见 DEVELOPMENT.md §5.3):`DesignIR` / `BlockPlan` / `Finding` / `CaseRecord`。每轮迭代的 IR/检索命中/plan/findings/修复动作全量落 `runs/run-<id>/audit.jsonl`(审计是一等公民,replay 靠它)。

## 硬纪律(违反会破坏项目根基)

1. **LLM/embedding 一律走 `llm/` 抽象层**,业务代码禁止直连任何 SDK/HTTP;测试用 `llm/fake.py`。
2. **easyeda-agent 版本钉死,两处**:`pyproject.toml [tool.edaloop]`(声明)与 `generate/adapter.py _PINNED_VERSION`(运行时硬门,以此为准)。升级=独立批+全量 evals 回归;**升级批必须清 w3-loop 的 resume state 再真跑**(state 里 done 会让旧 PASS 混进新回归)。
3. **每个 PR 必跑 pytest + evals 子集**,金标准不回退;门禁零豁免——任何「先跳过校验」的代码路径禁止合入。
4. 环境变量走 `.env`(`.env.example` 为模板):`EDALOOP_LLM_*`(任意 OpenAI 兼容端点)/ `EDALOOP_EMBED_*`(硅基流动 BGE-M3)/ `EASYEDA_BIN` / `EDALOOP_PROJECT`。
5. 平台墙沿用上游 workaround:无 undo → 失败补偿 = 显式删除残留 + 重放;落图后须显式 save。
