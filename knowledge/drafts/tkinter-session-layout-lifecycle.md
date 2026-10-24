<!-- codex-experience: {"schema":2,"id":"tkinter-session-layout-lifecycle","title":"Tkinter 布局记忆应按窗口生命周期保存和恢复","summary":"Tkinter 布局记忆应按窗口生命周期保存和恢复","keywords":["Tkinter","PanedWindow","布局记忆","列宽","最大化","重启","单实例","scope:project"],"scope":"project","kind":"project","status":"draft"} -->
# Tkinter 布局记忆应按窗口生命周期保存和恢复

## 背景

Tkinter 桌面工具需要在重启后恢复窗口尺寸、列表与预览分隔比例、列宽和筛选状态，并能应对最大化、最小化和副屏变化。

## 问题

只在正常关闭时保存布局会漏掉强制退出或重复启动场景；把最大化/最小化几何直接当作普通几何会污染下一次启动；PanedWindow 在首次布局后还会被 Tk 重新计算，过早设置 sash 位置不可靠。Treeview 的渲染宽度也不应反向覆盖用户保存的基础列宽。

## 方案

保存正常几何和 window state 两个独立字段，根窗口 Configure 时立即记录 normal geometry，调整后使用短 debounce 原子写入，关闭时补写。恢复时按当前工作区过滤并夹紧尺寸和坐标，在 PanedWindow 与左侧 pane 配置后通过 idle 回调设置比例；拖动释放后才提交新比例。将基础列宽作为模型保存，剩余空间只补给标题列。重复启动使用 named mutex 加 event 唤回已有窗口，避免强杀导致布局丢失。

## 代码或命令

相关实现位于 cli_session_manager.py 的 SessionManagerApp 布局方法和单实例方法。可用以下命令验证：

    python -m unittest discover -s tests -v
    python tests/runtime_smoke.py
    python tests/visual_smoke.py

测试应覆盖正常尺寸/最大化/最小化、坏设置、列宽拖动、隐藏列、分隔条比例、横向位置、选中项和独立进程重启。

## 关键结论

布局记忆是一个窗口生命周期问题，不是单个 sash 或 geometry 字段的问题。只有区分用户意图（normal geometry）与窗口状态，并把恢复放在 Tk 完成布局之后，重启和显示器变化才会稳定。
