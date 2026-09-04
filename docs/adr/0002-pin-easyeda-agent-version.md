# ADR-0002:easyeda-agent 依赖版本钉死 v0.25.1

| 项 | 值 |
|---|---|
| 状态 | 已接受(2026-08-17) |
| 影响模块 | M3 generate(typed actions)、M4 validate(sch gate)、M2(LCSC 查询,ADR-0004) |
| 上游关联 | DEVELOPMENT.md §4 原则6、§10(依赖钉死)、R3;research-datasheet-extraction-feasibility.md |

## 背景

- 本项目经 CLI 子进程消费 easyeda-agent(CLI/daemon/连接器.eext/skill 四件套),不引入代码级依赖,但**行为面强耦合**:typed actions 语法、`sch gate` 门禁规则、`resolve-lcsc` 输出、daemon 协议。
- R3:上游破坏性变更风险,缓解措施即版本钉死 + 独立 PR 升级。

## 决策

1. **easyeda-agent 钉死 `v0.25.1`**(2026-08-17 真机安装并验证:daemon health 非 stale,`sch read` 最小命令跑通)。
2. 钉死位置:`pyproject.toml` `[tool.edaloop]` 段(`easyeda-agent = "0.25.1"`,声明性元数据,非安装依赖)+ 本 ADR。
3. **升级纪律**:版本升级 = 独立 PR + 全量 evals 回归;禁止顺手升级。
4. 运行时探测:`edaloop` 启动时可选执行 `easyeda version` 比对(实现于 M7,不匹配仅告警不阻断,PoC 简化)。

## 后果

- 正:上游变更被 PR 粒度隔离;复现性有据可查。
- 负:需人工跟进上游 release 节奏(缓解:R3 触发信号=升级回归失败)。
