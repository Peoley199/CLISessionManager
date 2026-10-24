<!-- codex-experience: {"schema":2,"id":"windows-session-launch-provider-recovery","title":"Windows 会话恢复的 CMD 控制流与失效提供方修复","summary":"Windows 会话恢复的 CMD 控制流与失效提供方修复","keywords":["Windows","CMD","Codex","resume","闪退","model_provider","配置恢复","shell","转义","scope:project"],"scope":"project","kind":"project","status":"draft"} -->
# Windows 会话恢复的 CMD 控制流与失效提供方修复

## 背景

会话管理器通过 Windows 终端启动 Claude Code 或 Codex。恢复入口必须保留交互终端、启动参数及原始会话记录。

## 问题

- 批处理直接执行另一个 `.cmd` 会转移控制流，后续错误提示和 `pause` 不会执行；仅检查启动脚本文本容易漏掉此问题。
- Codex 历史会话保存了 `model_provider`。用户切换或移除提供方后，旧会话仍可能在恢复阶段请求不存在的提供方。
- `call` 会进行第二轮展开；将包含百分号、感叹号、连接符等字符的路径直接插入 shell 字符串可能改变参数。
- 对 app-server 使用 `communicate(input=全部请求)` 会立即关闭 stdin，可能导致服务器在处理请求前退出，得不到响应。

## 方案

- Windows 新建与恢复入口共用 `session_launcher.py`。CMD 使用 `/d /v:off /s /k`，Python 在独立子进程中运行 CLI，保留 stdout/stderr 的终端属性和退出后的窗口。
- 参数存入本次子进程的环境变量，以双引号包围后仅展开一次；工作目录通过 `cwd` 传递。真实 CMD 测试覆盖中文、空格、百分号、感叹号、连接符和括号路径。
- 在终端进程中向 Codex app-server 发送 initialize、initialized、config/read，按响应 ID 等待结果后才关闭 stdin。设置等待超时并回收本次查询的进程树，不阻塞 Tk 线程。
- 仅当历史提供方已从当前配置移除时，追加当前提供方与当前模型的启动参数。已注册提供方和内置提供方保留原恢复行为。默认模型由 Codex 自己解析，避免引入独立 TOML 解析器或硬编码模型。
- 保留 JSONL 和配置，提供方迁移只影响本次恢复；控制台显示迁移信息，日志记录命令、目录与退出码。

## 代码或命令

```powershell
python -m py_compile cli_session_manager.py session_launcher.py
python -m unittest discover -s tests -v
python tests/runtime_smoke.py
```

`tests/test_session_launcher.py` 使用真正的 CMD 与模拟 npm shim 验证退出码、stderr 可见性、窗口保留和参数完整性。配置检查只发送读取 RPC，不创建模型对话。真实会话验收应额外检查可输入状态、退出后终端存活及历史文件哈希。

## 关键结论

终端窗口闪退和 CLI 初始化失败可能是两个叠加问题，必须分别修复。原生 shell 控制流和恢复配置需要真实进程测试，单纯 mock 启动函数或断言脚本文本不足以证明可用。
