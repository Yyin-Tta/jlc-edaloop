# ADR-0006:Embedding 通道 PoC 期采用硅基流动托管 BGE-M3

| 项 | 值 |
|---|---|
| 状态 | 已接受(2026-08-17) |
| 影响模块 | M2 knowledge、`llm/` provider 抽象层、M0 环境清单(§8) |
| 上游关联 | DEVELOPMENT.md §1 支柱1、§4 原则4、§6、§8;ADR-0003(多模态选型,同属接口抽象纪律) |

## 背景

- M2 混合检索需要 embedding 通道,选型已定 BGE-M3(中英双语,原计划本地部署,对齐支柱1「本地部署/数据不出域」)。
- 本地部署 BGE-M3 权重(GPU/CPU 推理、环境配置)在 PoC 期成本不低;硅基流动(SiliconFlow)提供同款 BGE-M3 的 OpenAI 兼容 API,模型一致、接入成本≈0。

## 决策

1. **PoC 期 embedding 走硅基流动 API**(dense,1024 维;与本地 BGE-M3 同维度,后续可无缝切换)。
2. **provider 抽象强制**:与 LLM 层同纪律——业务代码禁止直连任何 embedding SDK/HTTP,统一经 `src/edaloop/llm/` 抽象层;endpoint/key 走 `.env`(`EDALOOP_EMBED_KEY` / `EDALOOP_EMBED_BASE`)。
3. **稀疏通道本地补齐**:API 只返回 dense 向量,BGE-M3 的 sparse 输出拿不到;M2 混合检索的关键字通道用本地 BM25 实现,不依赖云端。
4. **精排引入 reranker**:检索层采用两段式——粗排(dense + BM25 混合召回)→ 精排 BGE-reranker-v2-m3(硅基流动,同一 key 复用;Phase 1 可换本地)。
5. **Phase 1 前落本地兜底**:本地 BGE-M3 权重下载 + provider 本地实现 = Phase 1 硬性项。PoC 期数据仅公开 datasheet,出域风险已接受并登记 R11。

## 后果

- 正:PoC 环境清单简化(无需权重下载/推理硬件),检索链路当天可跑;reranker 精排显著提升检索质量(直接利好 W1 Go 指标 Top-5 ≥80%)。
- 负:datasheet 文本出域,偏离支柱1(已登记 R11);新增一个外部依赖与 key。
- 中性:向量维度锁定 1024(与本地同款,无迁移成本);reranker 增加一次 API 往返时延。

## 备选与否决理由

| 备选 | 理由 |
|---|---|
| 本地 BGE-M3 权重(FlagEmbedding) | PoC 期环境成本高;不否决,降级为 Phase 1 硬性项 |
| 其他 embedding API(OpenAI/text-embedding 等) | 中文弱或维度不同,切本地有迁移成本 |
| 纯向量检索(不引入 reranker) | 召回质量低于混合+精排,放弃 |
