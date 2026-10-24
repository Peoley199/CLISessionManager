<!-- codex-experience: {"schema":2,"id":"claude-resume-file-after-project-move","title":"Claude 迁移工作目录后按文件恢复会话","summary":"Claude 迁移工作目录后按文件恢复会话","keywords":["Claude","resume","SessionID","JSONL","目录迁移","CLAUDE_CONFIG_DIR","真实终端","scope:project"],"scope":"project","kind":"project","status":"draft"} -->
# Claude 迁移工作目录后按文件恢复会话

## 背景

会话管理器显示 Claude 历史记录，并允许原项目目录失效后选择新目录恢复。

## 问题

Claude 的交互 Session ID 查找受当前项目目录约束。管理器拿着已知 JSONL，却只传 ID，在新的工作目录中可能得到 No conversation found。CLAUDE_CONFIG_DIR 决定 CLI 数据来源；管理器硬编码默认目录会进一步造成列表和实际恢复不一致。

## 方案

在已验证支持文件恢复的 Claude 版本上，向交互 --resume 传入所选 JSONL 的绝对路径，工作目录仍使用用户所选目录，不复制或重写历史。统一管理器数据路径与 CLAUDE_CONFIG_DIR，并在切换工作目录前规范化相对配置目录。

## 代码或命令

`python -X utf8 tests/claude_console_smoke.py --moved --resume <测试会话ID>`

本项目已在 Claude Code 2.1.220 Windows 上验证。测试使用新建的专用会话，先保存口令，关闭，再换目录恢复并询问未在新提示里提供的口令片段。验证实际上下文，不只判断窗口是否打开。

## 关键结论

会话 ID、文件路径、工作目录和配置根目录是不同概念，应分别验证。测试 print 模式不能代替交互模式的配置错误检查，因为 CLI 可能在非交互模式跳过坏配置。真实请求的输出异常要用历史 JSONL 和直接 CLI 对照确认，不应仅凭终端截图归因于启动器。
