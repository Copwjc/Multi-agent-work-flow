# User Super Admin Override

用户是 multi-agent 工作流的 Super Admin。只要用户认为任务方向、目标解释、优先级、产物形式或执行路线出现偏差，就可以发出强制纠偏指令。Leader 必须立即暂停当前调度，重定向后续工作，并把纠偏记录写入任务日志。

## 触发方式

用户可以使用自然语言，也可以使用明确前缀：

```text
SUPER ADMIN OVERRIDE:
原因：
新的方向：
必须停止：
必须继续：
验收标准：
```

中文简写也有效：

```text
超级管理员纠偏：
原因：
新方向：
停止：
继续：
验收：
```

只要语义清楚表达“当前方向错了，需要强制修正”，Leader 都应视为 Super Admin Override。

## 优先级

1. 系统、平台、安全和工具权限约束。
2. 用户 Super Admin Override。
3. 用户原始任务说明。
4. Leader 的计划和子 agent 的建议。
5. 既有模板、默认实践和历史偏好。

Super Admin Override 可以改变目标、范围、产物、优先级、角色分工和验收标准；但不能要求 agent 绕过系统限制、伪造结果、隐藏失败或删除未授权内容。

## Leader 响应流程

收到 Override 后，Leader 必须：

1. 暂停当前未完成的任务调度。
2. 用一到三句话重述用户的新方向。
3. 标记哪些旧结论、代码、实验或报告段落被废弃、保留或需要复核。
4. 更新 `tasks/<slug>/notes/override_directive.md`。
5. 追加记录到 `tasks/<slug>/logs/override_log.md` 和 `tasks/<slug>/logs/agent_interactions.md`。
6. 向相关 agent 下发修正后的任务包。
7. 在最终交付中说明 Override 对结果的影响。

## 子 Agent 响应规则

子 agent 收到 Leader 转发的 Override 后必须：

- 立即停止沿旧方向扩展。
- 明确列出受影响的产物。
- 只保留与新方向一致的结论和文件。
- 如果新方向与已证明事实、真实测试结果或环境限制冲突，必须报告冲突，而不是沉默执行。
- 输出“已采纳 Override / 仍需 Leader 决策 / 无法执行原因”。

## 日志原则

Override 日志应记录决策摘要，不记录冗长内部推理：

- 触发时间。
- 用户纠偏内容摘要。
- 被停止的方向。
- 新方向和新验收标准。
- 受影响 agent。
- 被废弃、保留、返工的产物。
- Leader 的恢复计划。

