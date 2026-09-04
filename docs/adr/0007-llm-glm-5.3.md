# ADR-0007:文本 LLM 由 DeepSeek 切换为 GLM-5.3

| 项 | 值 |
|---|---|
| 状态 | 已接受(2026-08-17,用户决策) |
| 影响模块 | `llm/` provider 实现选择、`.env.example`、§8.1 环境清单 |
| 上游关联 | DEVELOPMENT.md §6 技术选型;ADR-0006(同属"接口抽象强制"纪律) |

## 背景

- §6 原选型 DeepSeek 官方 API,理由:嘉立创官方扩展同款(生态对齐)、便宜、中文强。
- 2026-08-17 用户决策:文本 LLM 改用智谱 **GLM-5.3**。

## 决策

1. 文本 LLM 默认 **GLM-5.3**(智谱 OpenAI 兼容端点 `https://open.bigmodel.cn/api/paas/v4`),`.env` 中 `EDALOOP_LLM_BASE`/`EDALOOP_LLM_MODEL` 配置。
2. **接口纪律不变**:业务代码仍只依赖 `LLMProvider` 抽象;DeepSeek/本地 vLLM/Ollama 均为一个 env 切换即可回切,无代码改动。
3. 附带收益:智谱 key 同时覆盖多模态候选(GLM-V 系列),ADR-0003 的 W1 评测可少申请一个 key。

## 后果与权衡

- 放弃的点:与嘉立创官方扩展同模型的"生态对齐"叙事(弱化,不影响功能)。
- 保留的点:OpenAI 兼容、中文能力、可换本地端点(支柱1 不受影响)。
- 风险:无新增(端点仍单一抽象位,切换成本≈0)。

## 备选与否决理由

| 备选 | 理由 |
|---|---|
| 维持 DeepSeek | 原选型;被用户决策覆盖,保留为回切选项 |
| 本地 vLLM/Ollama | PoC 阶段算力/运维成本高;Phase 1 本地化项(R11) |
