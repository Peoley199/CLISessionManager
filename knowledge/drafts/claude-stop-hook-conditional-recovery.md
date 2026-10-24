<!-- codex-experience: {"schema":2,"id":"claude-stop-hook-conditional-recovery","title":"Claude Stop hook 条件恢复与真实客户端验收","summary":"Claude Stop hook 条件恢复与真实客户端验收","keywords":["Claude","Stop","hook","JSON","validation","流式","条件恢复","CLI","验收","scope:project"],"scope":"project","kind":"project","status":"draft"} -->
# Claude Stop hook 条件恢复与真实客户端验收

## 背景

本项目通过真实 Claude CLI、隔离 Hub 容器与合成上游验收条件恢复补丁。普通请求须保持实时流式，恢复仅针对已损坏的特定协议响应。

## 问题

Stop prompt hook 是单独的模型请求，使用 ok/reason/impossible JSON 协议。仅保存系统提示第一句会漏掉明确的 JSON 输出要求；有些上游忽略 output_config，导致合成测试失真。CLI 退出码正常也不能证明 hook 通过。

普通流式首段若只含 metadata，会被 Hub 既有正文门控等待；不能据此认定新补丁整段缓冲。极短答案被上游截空时，甚至无法进入要验证的 Stop 阶段。

## 方案

1. 用真实客户端捕获合成 hook 请求，保留完整系统指令、末条条件与 schema；不采集业务会话或凭据。
2. 恢复识别联合检查客户端、路径、工具列表、原生提示和 schema，使用独立默认关闭的供应商白名单。只对该后台判断完整校验，健康结果不补发。
3. 损坏判断最多补发一次非流式，校验后交付；不猜测 ok:true，保留 false 和 impossible。外围仍受既有失败预算约束。
4. 验收同时覆盖健康不重发、损坏恢复、恢复仍坏、非白名单、客户端取消、超时、大小上限、普通实时流与已有工具恢复。
5. CLI 日志必须出现有效 Model response 与条件结果。故意让首次判断 false、后续 true，证明未完成目标仍会继续。
6. 时序测试先发正文再延迟结束，比较客户端首字节与上游完成时间。若上游截断，绕过 Hub 做流式/非流式对照；把补丁作用和上游残留缺陷分别记入报告。

## 代码或命令

```text
健康 Stop 上游 stream: [true]
损坏 Stop 上游 stream: [true, false]
未完成恢复后下一条普通请求 stream: true
日志: Hooks: Model response:
日志: Hooks: Prompt hook condition was met:
```

## 关键结论

条件恢复可以修复结构化判断而保留普通流式路径，但不能据此承诺上游普通文本完整。有效验收必须包含真实 wire 形状、正文到达时序和客户端实际执行效果，失败证据也应保存。
