# Codex Multi-Agent Workflow

本项目定义一个可复用的 Codex multi-agent 协作工作流，用于处理文献检索、数学问题、算法实现、实验验证与 LaTeX 论文报告。工作流由 Leader 负责目标拆解、最终仲裁和验收，但专业 agent 可以直接互相请求证据、资源和局部验证，不必把所有沟通都绕回 Leader。

- Literature Collector：负责检索文献、梳理主流方法、研究路线、baseline、数据集和评价指标。
- Mathematician：负责数学建模、证明、复杂度推导与边界条件分析。
- Code Expert：负责算法实现、测试、实验脚本、性能验证与可复现实验记录。
- LaTeX Writer：负责将问题、方法、证明、实验与结论组织成可编译的 LaTeX 报告。

五个角色提示词位于 `agents/` 目录，可复制到不同 Codex 会话中使用，也可由 Leader 在同一会话中模拟多轮协作。每个实际任务都会生成独立的 `tasks/<slug>/logs/` 和 `tasks/<slug>/notes/`，用于保存 agent 请求、运行日志、资源登记和阶段总结。

## 目录结构

当前项目仅包含工作流文档、角色提示词、任务模板和脚手架工具；实际任务产物默认不纳入仓库：

```text
multiagent
├── README.md
├── agents
│   ├── leader.md
│   ├── literature_collector.md
│   ├── mathematician.md
│   ├── code_expert.md
│   └── latex_writer.md
├── docs
│   ├── inter_agent_dialogue.md
│   ├── workflow_checklist.md
│   └── super_admin_override.md
├── templates
│   ├── task_brief.md
│   ├── agent_interactions.md
│   ├── inter_agent_dialogue.md
│   ├── literature_review.md
│   ├── leader_summary.md
│   ├── override_directive.md
│   └── resource_registry.md
├── tools
│   ├── create_task.py
│   ├── multiagent_gui.py
│   ├── multiagent_web.py
│   └── validate_workflow.py
└── workflow
    └── protocol.md
```

建议每次实际运行任务时，用脚手架在项目内创建独立任务目录保存产物：

```bash
python tools/create_task.py "Shortest Path Proof" --slug shortest-path-proof
```

```text
tasks
└── <slug>
    ├── README.md
    ├── src
    │   ├── __init__.py
    │   └── solution.py
    ├── tests
    │   └── test_solution.py
    ├── experiments
    │   ├── run_experiment.py
    │   ├── analysis.md
    │   └── outputs
    ├── report
    │   ├── main.tex
    │   ├── references.bib
    │   └── figures
    ├── notes
    │   ├── task_brief.md
    │   ├── literature_review.md
    │   ├── leader_summary.md
    │   ├── open_questions.md
    │   └── override_directive.md
    └── logs
        ├── agent_interactions.md
        ├── inter_agent_dialogue.md
        ├── override_log.md
        └── run_log.md
```

`<slug>` 推荐使用短横线命名，例如 `shortest-path-proof`。如果不传 `--slug`，工具会根据任务标题自动生成。

## 工作流

1. Leader 读取用户问题，提炼目标、输入输出、约束、验收标准和风险点。
2. Leader 先将文献检索与研究路线梳理任务交给 Literature Collector。
3. Literature Collector 返回主流方法、代表论文、baseline、数据集、评价指标、研究空白和推荐路线。
4. Leader 将数学建模、证明或推导任务交给 Mathematician，并传入文献中的理论假设和路线图。
5. Mathematician 返回定义、引理、证明草稿、反例检查和仍需验证的问题。
6. Leader 将算法规格、baseline、边界条件与数学结论交给 Code Expert。
7. Code Expert 实现算法、编写测试、运行实验，并返回复现命令、结果摘要和失败案例。
8. Leader 对照文献、数学结论与实验结果做一致性检查；必要时安排返工。
9. 任一 agent 在执行中可以向其他 agent 直接发起 `resource_request`、`evidence_request`、`theory_check`、`baseline_request` 等请求，并用 Parent 字段记录依赖链。
10. LaTeX Writer 基于已闭环的证据链组织报告；缺少证据的 claim 必须继续请求资源或标记为未验证。
11. Leader 汇总最终产物，检查 open/blocked 请求、资源状态和验收清单，并按验收标准给出通过/未通过结论。

## Agent 直接协作与资源索取

默认允许以下点对点交互：

- Code Expert 向 Literature Collector 请求 baseline、数据集、指标或开源实现。
- Code Expert 向 Mathematician 请求假设、可辨识性、边界条件或反例。
- Mathematician 向 Code Expert 请求数值反例、测试样例或实验现象。
- LaTeX Writer 向 Literature Collector 请求引用、BibTeX 和来源边界。
- LaTeX Writer 向 Code Expert 请求实验命令、表格、图和代码来源。
- 任一 agent 在目标冲突、资源缺失或结论会影响最终交付时升级给 Leader。

请求必须写入 `tasks/<slug>/logs/inter_agent_dialogue.md`，资源必须登记到
`tasks/<slug>/notes/resource_registry.md`。Leader 不必转述每个请求，但最终必须检查所有 `open` / `blocked` 请求和被 `invalidated` 的资源。

## 网页可视化界面

本项目提供一个本地网页 dashboard，用于查看 agent 对话链、协作流程和共享资源，并允许用户随时介入：

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python tools/multiagent_web.py --port 8765
```

然后打开：

```text
http://127.0.0.1:8765/
```

界面会读取 `tasks/<slug>/logs/inter_agent_dialogue.md`、`tasks/<slug>/logs/agent_interactions.md` 和 `tasks/<slug>/notes/resource_registry.md`。用户输入框支持普通指令、资源请求、证据请求、理论检查、实现检查和 Super Admin Override；提交后会写回对应任务日志。

### Agent Runner / 外部模型接口

网页端提供 `Agent 调度` 面板，支持调用国内主流大模型 API 生成 coding plan 或执行任务。

支持的开箱即用提供商：

| 提供商 | 环境变量 | 模型示例 |
|--------|----------|----------|
| **火山引擎 · 豆包** | `ARK_API_KEY` | 接入点 ID（如 `ep-2025xxx`） |
| **DeepSeek** | `DEEPSEEK_API_KEY` | `deepseek-chat` |
| **通义千问 Qwen** | `DASHSCOPE_API_KEY` | `qwen3-coder-plus` |
| **Moonshot Kimi** | `MOONSHOT_API_KEY` | `kimi-k2-0711-preview` |
| **智谱 GLM** | `ZHIPU_API_KEY` | `glm-4-plus` |
| **自定义 API** | 自定义 | 任意 OpenAI 兼容端点 |

所有提供商均使用 OpenAI 兼容的 `/v1/chat/completions` API。

安全约定：

- Base URL、模型名称和协议会保存在本地浏览器，便于重复使用。
- API key 默认只临时使用；勾选“记住 API Key”后仅保存到本地浏览器 localStorage，不写入项目文件或仓库。
- `plan_only` 只生成计划并写入 `tasks/<slug>/notes/runner_<run_id>.md`。
- `execute` 允许 runner 在任务目录内修改产物，并可通过显式 `command` 代码块运行受控本地命令。命令执行采用白名单，不支持任意 shell、重定向、包安装、网络下载或破坏性命令。
- 运行日志写入 `tasks/<slug>/logs/agent_runs/<run_id>.log`，并同步记录到
  `tasks/<slug>/logs/agent_interactions.md`。

本地执行命令格式：

````text
```command cwd="."
python3 -m unittest discover -s tests -v
python3 experiments/run_experiment.py
```
````

LaTeX 报告可在 `report/` 下运行：

````text
```command cwd="report"
pdflatex -interaction=nonstopmode -halt-on-error main.tex
```
````

## 用户超级管理员权限

用户拥有 Super Admin Override 权限。当用户认为任务方向、目标解释、优先级、产物形式或执行路线出现偏差时，可以强制修正后续方向。Leader 必须立即暂停当前调度，重述新方向，标记哪些旧产物废弃/保留/返工，并把纠偏记录写入日志。

推荐指令格式：

```text
超级管理员纠偏：
原因：
新方向：
停止：
继续：
验收：
```

执行规则：

- Super Admin Override 的工作流优先级高于 Leader 计划、子 agent 建议和既有模板。
- Override 不绕过系统、安全、工具权限和事实约束；如果新方向与真实测试、数学事实或环境限制冲突，Leader 必须说明冲突。
- Leader 应更新 `tasks/<slug>/notes/override_directive.md`、`tasks/<slug>/logs/override_log.md` 和 `tasks/<slug>/logs/agent_interactions.md`。
- 子 agent 收到 Override 后必须停止沿旧方向扩展，并报告受影响产物。

详细协议见 `docs/super_admin_override.md`。

## Leader 调度协议

Leader 每轮调度都应给出明确任务包：

```text
To: <agent-name>
Round: <n>
Context:
- 用户目标：
- 当前已知结论：
- 相关文件或产物：

Task:
- 本轮要完成什么：
- 不要做什么：
- 输出格式：

Acceptance:
- 本轮结果必须满足的条件：
```

专业 agent 回复时应包含：

```text
From: <agent-name>
Round: <n>
Result:
- 主要结论：
- 关键推理或实现：
- 产物路径：

Checks:
- 已验证：
- 未验证：
- 风险：

Need from Leader:
- 需要补充的信息或决策：
```

## 交互日志

每个任务工作区应维护 `tasks/<slug>/logs/agent_interactions.md`。日志不需要记录完整长篇推理，但必须保留足够的信息，使其他人能复盘角色之间的决策过程。

对于 agent 之间的具体请求和证据链，应维护 `tasks/<slug>/logs/inter_agent_dialogue.md`。它记录 Literature Collector、LaTeX Writer、Code Expert、Mathematician 等角色之间的请求、回复、资源索取、依赖和阻塞点，例如：

```text
leader -> literature_collector: literature_request
literature_collector -> leader: reply
latex_writer -> code_expert: evidence_request
code_expert -> mathematician: theory_check
mathematician -> code_expert: reply
code_expert -> latex_writer: reply
code_expert -> literature_collector: baseline_request
latex_writer -> code_expert: resource_request
```

详细协议见 `docs/inter_agent_dialogue.md`。

推荐格式：

```markdown
# Interaction Log: <slug>

## Metadata

- Task:
- Started:
- Leader:
- Agents:

## Round 1

### Leader -> Mathematician

- Request:
- Acceptance:

### Mathematician -> Leader

- Summary:
- Key equations / claims:
- Risks:

### Leader Decision

- Accepted:
- Follow-up:
- Artifacts:

## Round 2

### Leader -> Code Expert

- Request:
- Acceptance:

### Code Expert -> Leader

- Summary:
- Commands:
- Results:
- Risks:

### Leader Decision

- Accepted:
- Follow-up:
- Artifacts:
```

日志原则：

- 记录“谁请求了什么、谁返回了什么、Leader 做了什么决定”。
- 对 agent 间请求使用稳定 Request ID，并记录 Parent 依赖，确保能追踪“报告 claim -> 实验证据 -> 数学假设”的链路。
- 对文献结论记录来源、年份、方法路线、baseline 和适用假设，而不是只写“已有相关工作”。
- 如果发生 Super Admin Override，记录用户纠偏摘要、废弃/保留/返工产物、新方向和新验收标准。
- 对数学结论记录定理名、公式编号或关键假设，而不是只写“已证明”。
- 对代码与实验记录命令、随机种子、环境假设和结果文件。
- 对 LaTeX 报告记录章节结构、图表来源和未解决的排版/引用问题。
- 对失败和返工如实记录，避免只保留成功路径。

## 验收标准

一次完整任务交付前，Leader 应检查：

- 问题定义清楚：输入、输出、约束、目标函数或命题均已明确。
- 文献综述可信：主流方法、代表论文、baseline、数据集、评价指标和研究空白均有来源记录。
- 数学部分可信：关键假设、定理、证明步骤、边界情况和反例检查均有记录。
- 算法部分可运行：实现路径清晰，包含测试，能复现核心实验或样例。
- 实验部分可解释：实验配置、数据来源、指标、结果和异常均有说明。
- 报告部分可编译：LaTeX 文件结构清楚，公式、图表、引用和结论一致。
- 跨角色一致：论文中的算法、符号、复杂度和实验数据与数学/代码产物一致。
- 日志完整：`interaction_log.md` 覆盖主要调度、反馈、决策和返工。
- 证据链可追踪：关键报告 claim 能在 `inter_agent_dialogue.md` 中追溯到 proof、test、experiment 或 Leader decision。
- 用户纠偏已执行：若发生 Super Admin Override，旧方向的影响已标记，新的验收标准已同步到所有相关 agent。

## 推荐使用方式

1. 运行 `python tools/create_task.py "<task title>" --slug <slug>` 创建 `tasks/<slug>`。
2. 将用户原始问题写入 `tasks/<slug>/README.md` 或 `tasks/<slug>/notes/task_brief.md`。
3. 使用 `agents/leader.md` 初始化 Leader。
4. Leader 根据任务复杂度调用或模拟其他四个 agent。
5. 每轮结束后追加更新 `tasks/<slug>/logs/agent_interactions.md`。
6. 只有当验收标准全部满足时，Leader 才输出最终答案或论文报告。

## GUI 控制台

可以启动一个本地 Tkinter GUI 来创建/打开任务、输入用户指令和任务目标、追加 agent 交互日志，并查看当前任务的日志内容：

```bash
python tools/multiagent_gui.py
```

无界面环境下可先做导入检查：

```bash
python tools/multiagent_gui.py --check
```

GUI 当前负责记录和查看工作流状态，不会自动启动真实子 agent。真正执行 multi-agent 工作仍由 Codex Leader 在会话中调度；GUI 写入的 `tasks/<slug>/logs/agent_interactions.md`、`tasks/<slug>/logs/inter_agent_dialogue.md`、`tasks/<slug>/logs/override_log.md` 和 `tasks/<slug>/notes/task_brief.md` 可作为调度上下文。

在 GUI 的 `Agent Handoff / Dialogue Entry` 区域，可以记录更细的 agent 交互：

- `evidence_request`: LaTeX Writer 向 Code Expert 索取实验或代码证据。
- `literature_request`: Leader 或其他 agent 向 Literature Collector 索取文献路线、baseline 或引用。
- `source_check`: LaTeX Writer 向 Literature Collector 核对某个 claim 是否有可靠文献支持。
- `baseline_request`: Code Expert 向 Literature Collector 索取可复现 baseline、数据集或指标。
- `theory_check`: Code Expert 向 Mathematician 索取理论推导或边界条件。
- `implementation_check`: Mathematician 或 LaTeX Writer 向 Code Expert 确认实现细节。
- `reply`: 回复某个 Request ID。
- `blocked`: 标记需要 Leader 决策的阻塞。

## 文件角色

- `agents/leader.md`：总调度、质量控制、日志维护和最终交付。
- `agents/literature_collector.md`：文献检索、主流方法梳理、baseline 和研究路线分析。
- `agents/mathematician.md`：数学形式化、证明、推导和反例检查。
- `agents/code_expert.md`：算法实现、测试、实验和复现说明。
- `agents/latex_writer.md`：论文结构、LaTeX 正文、图表说明和编译检查。
- `tools/multiagent_gui.py`：本地任务控制台，用于输入目标、记录交互和查看 agent 日志。
