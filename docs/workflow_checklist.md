# Multi-Agent Workflow 验收清单

日期: 2026-05-18
验收负责人: Leader agent
范围: 文献检索、数学问题求解、算法实现、实验验证、LaTeX 论文报告。

## 最终验收门槛

- [ ] 项目内存在清晰的 multi-agent 工作流说明，覆盖角色、输入、输出和交接顺序。
- [ ] 工作流明确区分 Leader、workflow-designer、tooling、实验/验证、LaTeX/reporting、audit/logging 的职责。
- [ ] Literature Collector 在数学、代码和论文写作前完成文献路线图，覆盖主流方法、代表论文、baseline、数据集和评价指标。
- [ ] 数学问题入口定义了假设、约束、推导/证明格式，以及歧义处理方式。
- [ ] 算法实现路径包含伪代码、实现位置、复杂度说明和失败场景。
- [ ] 实验验证路径包含数据来源或生成方式、可复现实验命令、预期指标和结果解释。
- [ ] LaTeX 论文报告路径包含问题陈述、方法、算法、实验、讨论和可复现附录。
- [ ] 内部交互日志概述 agent 协作过程，且不暴露敏感凭据或不必要的原始草稿。
- [ ] agent 间请求、回复、证据链和阻塞点记录在 `logs/inter_agent_dialogue.md`，包含稳定 Request ID 和 Parent 依赖。
- [ ] agent 可直接互相请求资源或验证，不必全部经过 Leader；会影响最终 claim 的冲突再升级给 Leader。
- [ ] 共享资源记录在 `notes/resource_registry.md`，包括 owner、路径、状态和可复用范围。
- [ ] 用户 Super Admin Override 机制已定义，包含触发方式、优先级、日志记录和恢复执行流程。
- [ ] tooling 说明记录如何运行测试、实验和 LaTeX 编译。
- [ ] 产物位于项目目录内，复现时不依赖未记录的本地隐藏状态。
- [ ] Leader 最终摘要说明已完成内容、未决事项和验收验证方式。

## 角色级检查

- [ ] Leader 确认任务范围，并处理 agent 之间的冲突或重复工作。
- [ ] Literature Collector 确认检索策略、来源可信度、研究路线、baseline、指标和未覆盖风险。
- [ ] workflow-designer 确认 agent 图、交接契约和产物命名约定。
- [ ] tooling 确认脚本、命令、目录约定、测试和实验执行方式。
- [ ] audit/logging 确认交互摘要、验收清单和实施决策记录。
- [ ] validation/experiment 角色确认报告结果有可运行证据支撑。
- [ ] reporting 角色确认 LaTeX 输出可以编译，或提供明确编译路径。

## Leader 需核对的证据

- [ ] 工作流设计文档或等价项目入口。
- [ ] `notes/literature_review.md` 中的文献综述、研究路线、关键来源、baseline、数据集和指标。
- [ ] 算法实现源码。
- [ ] 测试或实验命令输出。
- [ ] 生成的结果文件、表格或图形。
- [ ] LaTeX 源文件，以及编译出的 PDF 或编译命令。
- [ ] `logs/agent_interactions.md` 中的简要协作记录。
- [ ] `logs/inter_agent_dialogue.md` 中的证据请求链，尤其是报告 claim、实验结果和数学假设之间的链路。
- [ ] `notes/resource_registry.md` 中的数据、脚本、结果、引用、证明和报告资源状态。
- [ ] 文献相关 claim 能追溯到 Literature Collector 的来源记录或明确标记为未验证。
- [ ] 若发生 Super Admin Override，`logs/override_log.md` 和 `notes/override_directive.md` 已记录纠偏内容、受影响产物和新验收标准。
- [ ] `notes/leader_summary.md` 中的本轮决策摘要。

## 签收备注

- 只有当每个必要产物都有负责人、验证路径和明确工作流位置时，才建议通过验收。
- 未勾选项应进入 Leader 后续任务列表，并标明负责 agent 与下一步动作。
- 如果用户发出 Super Admin Override，验收应以最新纠偏后的目标为准，旧目标只能作为历史背景。
- 如果存在 `open` 或 `blocked` 的 inter-agent request，最终交付必须把它列为剩余风险，不能沉默签收。
