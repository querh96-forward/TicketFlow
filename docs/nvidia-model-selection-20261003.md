# NVIDIA候选模型探测 · 2026-10-03

目的：为TicketFlow寻找响应更快、可使用现有NVIDIA密钥的工具调用模型。这是少量接口诊断，不是完整工单能力或生产延迟基准。

## 观察

| 模型 | 本次观察 |
|---|---|
| z-ai/glm-5.3 | 此前工具调用53.2秒，结果回传成功；单Agent工单300秒总超时 |
| z-ai/glm-5.3-flash | 一条工具调用在60秒超时，无自动重试 |
| nvidia/nemotron-3.5-lightning-30b-a3b | 一条工具调用在60秒超时，无自动重试 |
| nvidia/nemotron-3-super-120b-a12b | 实际请求HTTP410；网页仍显示免费端点可用，不据网页声称能调用 |

GLM Flash和Lightning的诊断并行发出，请求同一个lookup_ticket工具、同一个TF-1001提示、温度0、输出上限1500。前者reasoning_effort=low，后者chat_template_kwargs.enable_thinking=false。只有各一条失败请求，不能给出平均或P95延迟，更不能推断模型本身能力。因首条请求超时，没有继续第二条或结果回传。

最早Super探测曾因本地网络权限未放行而连接失败；取得权限后独立请求才返回410。连接失败单独保存，不计作服务性能结果。

[候选请求结果](../evals/reports/nvidia-candidate-probes-20261003.json) · [Super接口结果](../evals/reports/nemotron-interface-probe-20261003.json) · [此前GLM完整诊断](nvidia-validation-20261003.md)

## 决策

本次没有找到已验证更快的免费候选，主.env和网页模型继续保留用户选择的GLM-5.3，未自行开启付费、未启动72次评测。若更换接口，先验证工具调用和正常/退回/过期三个场景，再冻结单/多Agent相同配置执行完整评测。可以把简单工具调用5—10秒作为项目体验目标，但该目标不是服务承诺。不同模型、不同服务结果分批保留。

当前事实是NVIDIA端点调用较慢；没有足够证据将原因确定为模型计算、排队或网络中的某一项。若希望尽快完成大量回归和现场演示，应优先选择实测响应稳定的API服务，不能仅根据Flash或Lightning命名判断速度。

## 官方页面

- https://build.nvidia.com/z-ai/glm-5-3-flash
- https://build.nvidia.com/nvidia/nemotron-3.5-lightning-30b-a3b
- https://build.nvidia.com/nvidia/nemotron-3-super-120b-a12b

页面信息与实时调用结果可能不一致，以本次已记录的实际调用为准。
