# Inter-Agent Dialogue Protocol

`agent_interactions.md` 记录全局事件摘要；`inter_agent_dialogue.md` 记录 agent 之间的具体请求、回复、证据链、资源索取和阻塞点。普通证据交换不必全部经过 Leader；Leader 负责最终裁决、验收和对外结论边界。

## 适用场景

使用 inter-agent dialogue 记录这些情况：

- LaTeX Writer 发现报告 claim 缺少证据，向 Code Expert 请求实验结果、代码路径或表格来源。
- LaTeX Writer 发现 Related Work 或方法 claim 缺少出处，向 Literature Collector 请求来源核查。
- Code Expert 需要 baseline、数据集、评价指标或开源实现，向 Literature Collector 请求可复现资料。
- Code Expert 发现实现条件不明确，向 Mathematician 请求定理假设、边界情况或复杂度推导。
- Mathematician 需要 Code Expert 提供反例、测试失败样例或数值实验现象。
- 任一 agent 发现冲突，需要 Leader 做取舍或重定向。
- 任一 agent 需要数据、脚本、结果、图表、引用、证明或报告材料，直接向资源 owner 或最可能的专业 agent 索取。

## 请求类型

- `evidence_request`: 索取支持报告 claim 的代码、实验、数据或图表证据。
- `literature_request`: 索取文献路线、代表论文、研究空白或引用条目。
- `source_check`: 核查某个论述是否有可靠文献支持。
- `baseline_request`: 索取 baseline、数据集、指标、协议或开源实现。
- `theory_check`: 请求数学定义、证明、假设、边界条件或反例分析。
- `implementation_check`: 请求代码路径、算法行为、复杂度、测试或失败样例。
- `writing_gap`: 报告撰写中缺少论据、定义、图表或引用。
- `reply`: 对某个请求给出答复和证据。
- `blocked`: 无法继续，需要 Leader 决策。
- `leader_decision`: Leader 对冲突或阻塞做最终裁决。
- `resource_request`: 索取具体资源，例如数据、脚本、结果文件、图、BibTeX、证明草稿或报告段落。
- `resource_reply`: 回复资源请求，必须给出路径、命令、来源、不可用原因或替代资源。

## 记录格式

每条记录必须包含：

- Request ID: 稳定编号，例如 `REQ-20260518-001`。
- Parent: 上游请求编号；没有则写 `none`。
- From / To: 发起和接收 agent。
- Type: 请求类型。
- Need: 具体需要什么。
- Why: 为什么需要它，以及会影响哪个产物。
- Artifact: 相关文件、命令或结果路径。
- Status: `open`、`answered`、`blocked`、`accepted`、`invalidated`。
- Leader Review Required: 是否需要 Leader 审核后才能进入最终报告或用户结论。

共享资源应同步登记在 `notes/resource_registry.md`。如果资源被废弃、过期或受 Super Admin Override 影响，应把相关请求标记为 `invalidated`，资源状态也同步更新。

## 示例链路

```markdown
## REQ-20260518-001

- Parent: none
- From: latex_writer
- To: code_expert
- Type: evidence_request
- Status: open
- Need: Provide experiment table supporting the convergence claim in Section 4.
- Why: The report currently states convergence improvement without a result file.
- Artifact: report/main.tex

## REQ-20260518-002

- Parent: REQ-20260518-001
- From: code_expert
- To: mathematician
- Type: theory_check
- Status: open
- Need: Confirm the theorem assumptions under which the convergence metric is meaningful.
- Why: The experiment table should not support a claim outside the theorem assumptions.
- Artifact: experiments/analysis.md
```

## Leader 审核规则

Leader 最终验收时应检查：

1. 报告中的关键 claim 是否能追溯到 literature source、proof、test、experiment 或 Leader decision。
2. `open` 和 `blocked` 请求是否已处理，或在最终风险中明确列出。
3. 被 Super Admin Override 废弃的请求是否标记为 `invalidated`。
4. agent 之间的回复是否指向真实产物，而不是只写“已确认”。

## Agent 自由协作规则

1. agent 可以直接向其他 agent 发起请求，不需要等待 Leader 转述。
2. 请求必须足够小，能由接收方在一个明确产物中回答。
3. 接收方可以继续向第三个 agent 发起子请求，并用 `Parent` 记录依赖链。
4. 如果请求改变任务目标、引入新 baseline、改变数学假设、影响最终 claim，必须升级为 `leader_decision`。
5. 若请求只是索取路径、命令、表格、引用、图或局部证明，可以直接完成并在 dialogue log 中闭环。
