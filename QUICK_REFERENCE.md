# CLI 会话管理器快速参考

## 启动与布局

双击 `CLI会话管理器.bat`。首次打开会使用当前显示器的大工作区，列表默认约占 68%。窗口大小、最大化状态、分隔条、列宽、可见列、行距、筛选、排序、选中项和滚动位置会自动记忆。调整停止约 350ms 后保存；最小化不会污染正常尺寸。

- 拖动分隔条：改变列表/预览比例
- 双击分隔条：恢复默认比例
- 拖动表头边界：调整单列宽度
- 双击表头边界：自动适配该列
- “适应窗口”：把可见列压入当前列表宽度
- “重置布局”：恢复默认列、比例、预览和舒适行距
- `Ctrl+P`：隐藏预览以获得最大列表宽度
- `Shift+滚轮`：横向滚动列表

## 日常操作

顶部从左到右是搜索、来源、分组、备注筛选和运行筛选。搜索覆盖标题、项目、路径、Session ID、备注和分组，输入暂停后自动过滤。列表选中一条时可以恢复、编辑备注、打开目录和打开 JSONL；选中多条时可以批量分组、复制 ID 或移入回收站。

新建会话打开独立窗口，先选 CLI，再从两个常用目录中启动；CLI 选择不会被列表来源筛选器偷偷替换。

## 快捷键

| 按键 | 操作 |
|---|---|
| `Enter` | 恢复单个选中会话 |
| `Delete` | 删除选中会话到回收站 |
| `F5` | 刷新 |
| `Ctrl+F` | 聚焦搜索框 |
| `Ctrl+P` | 显示/隐藏预览 |
| `Ctrl+N` | 新建会话 |
| `Ctrl+A` | 选择筛选结果 |
| `Ctrl+C` | 复制 Session ID |
| `Ctrl+D` | 编辑备注 |
| `Esc` | 清除选择；搜索框内清空搜索 |

## 文件位置

- 会话数据：`~/.claude/projects/`、`~/.codex/sessions/`
- 标题历史：`~/.claude/history.jsonl`、`~/.codex/session_index.jsonl`、`~/.codex/history.jsonl`
- 工具设置：`~/.claude-internal/session_manager_settings.json`
- 备注/分组：`~/.claude-internal/session_notes.json`、`session_groups.json`
- 删除回收站：`~/.claude-internal/.session_trash/`

## 验证命令

```powershell
python -m py_compile cli_session_manager.py
python -m unittest discover -s tests -v
python tests/runtime_smoke.py
python tests/visual_smoke.py
```
