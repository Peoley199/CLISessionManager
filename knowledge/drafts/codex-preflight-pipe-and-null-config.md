<!-- codex-experience: {"schema":2,"id":"codex-preflight-pipe-and-null-config","title":"Codex 配置预检的管道回收与 null 字段","summary":"Codex 配置预检的管道回收与 null 字段","keywords":["Codex","Windows","CMD","app-server","stdout","超时","provider","null","配置预检","scope:project"],"scope":"project","kind":"project","status":"draft"} -->
# Codex 配置预检的管道回收与 null 字段

## 背景

Windows 会话启动器使用 CMD 包装 npm shim，通过 Codex app-server 查询配置，再启动交互 CLI。

## 问题

关闭 stdin 并等待 CMD 退出，不代表其后代已关闭 stdout。若后台读线程仍持有 TextIOWrapper 的读取锁，主线程调用 stdout.close 可以永久阻塞，抵消前面的 RPC 超时。另一个真实协议差异是 provider 对象包含 null 可选字段，直接序列化 TOML 会失败。

## 方案

在查询响应返回或超时后、CMD 仍拥有后代树时回收查询进程树；有界等待读取线程，仅在线程结束后从主线程关闭 stdout。只处理本次启动的进程，不清理用户已有 daemon。provider 兼容参数递归跳过空字段，保留嵌套类型；采用本次调用覆盖，避免修改共享配置。

## 代码或命令

`python -X utf8 -m unittest discover -s tests -p test_session_launcher.py -v`

`python -X utf8 tests/codex_protocol_smoke.py`

真实协议回归使用临时 CODEX_HOME、不可连接的本地模型端点，不发送 turn/start。分别验证缺失 provider 的错误和添加兼容参数后的恢复，不能把普通 provider 恢复成功误作缺失 provider 修复的证据。

## 关键结论

超时需要覆盖清理阶段。Windows 子进程退出、管道 EOF、后代句柄释放是不同事件；真实进程测试要包含继承 stdout 的后代。RPC 返回值应取自真实客户端验证，不能只用省略可选字段的手写 fixture。
