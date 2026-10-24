<!-- codex-experience: {"schema":2,"id":"claude-tool-retry-wire-normalization","title":"Claude 工具重试须验证真实 HTTP 消息结构","summary":"Claude 工具重试须验证真实 HTTP 消息结构","keywords":["Claude","Hub","tool_use","工具解析","重试","thinking","消息合并","wire","scope:project"],"scope":"project","kind":"project","status":"draft"} -->
# Claude 工具重试须验证真实 HTTP 消息结构

## 背景

会话管理器调查 Claude 经 Hub 调用上游时的工具解析失败，并维护条件恢复补丁和验收脚本。

## 问题

本地会话 JSONL 记录不等于实际 HTTP 消息序列。Claude 可能删除只有思考的助手消息，随后重试提示成为相邻用户消息，或与上一条含工具结果的用户消息合并。只手工构造独立重试提示的测试无法证明客户端恢复有效。

## 方案

同时检查客户端 JSONL、网关实际请求结构与恢复分支日志。用真实客户端连接本地模拟接口，先返回正常工具调用，再返回 `stop_reason: tool_use` 但不带工具块的流，捕获客户端真实重试请求。把已确认的独立、合并、相邻消息结构纳入回归。

恢复识别只检查精确顶层重试文本；对历史工具调用和结果校验 ID 对应关系，保留所有历史结果。普通请求仍走原链路。原始生产内容无需写入测试夹具。

## 代码或命令

本项目复现入口：`python -X utf8 tests/claude_tool_retry_wire_probe.py`。

## 关键结论

上线前需要验证真实客户端构造的恢复请求，不能只验证手工模拟请求。镜像已部署和恢复开关已启用，并不代表每种实际重试结构都会命中补丁。HTTP 200 也不能证明工具协议有效。
