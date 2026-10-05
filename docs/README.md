# 文档索引

## 从这里开始

- [架构与实现](design.md)：角色工具权限、持久化状态、幂等与设计取舍。
- [单 / 多 Agent 对照方法](comparison-method.md)：架构差异、评分维度与限制。
- [验证历史](validation.md)：各阶段测试与真实模型验证记录。

## 当前版本与最近完整对照

| 日期 | 内容 | 范围 |
|---|---|---|
| 2026-10-04 | [权威事实归属修复](evidence-authority-20261004.md) | 本地 / Docker 100 项测试；3 场景 × 2 组真实回归，6/6 |
| 2026-10-03 | [Kimi 完整对照](comparison-kimi-24-20261003.md) | 当日版本 12 场景 × 2 组，单 12/12、多 11/12 |
| 2026-10-03 | [Kimi 失败分析](kimi-failure-analysis-20261003.md) | 专家混淆事实来源导致误转人工 |
| 2026-10-03 | [Kimi 评测协议](kimi-evaluation-protocol-20261003.md) | 冻结模型、场景与累计预算 |

不同日期、源码或模型配置的报告不能合并为同一版本的成功率。最新修复仅做针对性回归，未重跑完整 12 场景。所有真实评测使用临时演示库、脚本模拟人工审批，原始报告保存在 [evals/reports](../evals/reports/)。

## 流程改进记录

- [人工退回与快照过期修复](workflow-fixes-20261002.md)
- [审核意见持久化交接](review-handoff-20261002.md)
- [审核交接真实回归](comparison-review-gate-20261002.md)
- [Flash 首轮结果](comparison-flash-initial-20261002.md)及[中性编号后的结果](comparison-flash-neutral-20261002.md)
- [确定性对照：10 月 1 日](comparison-scripted-20261001.md)、[10 月 2 日](comparison-scripted-20261002.md)

## 历史模型与服务试验

- [Pro 完整对照](comparison-results-20261001.md)及[失败分析](comparison-analysis-20261001.md)：单 / 多 Agent 均 11/12，没有观察到通过率优势。
- [72 次重复试验协议](evaluation-protocol-20261003.md)、[中断结果](comparison-flash-72-20261003.md)及[发现与修复](evaluation-findings-20261003.md)：模型额度耗尽导致大量服务失败，不能作为有效完整稳定性评测。
- [NVIDIA 接入验证](nvidia-validation-20261003.md)及[候选模型探测](nvidia-model-selection-20261003.md)：接口兼容性、超时和服务可用性记录，不作为完整流程成功证据。

历史记录用于解释设计变化和评测局限，当前启动方式以根目录 [README](../README.md) 为准。
