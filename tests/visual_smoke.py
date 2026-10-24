"""Capture the real Tk UI with synthetic data; requires Pillow for QA only."""

import ctypes
import json
from pathlib import Path
import tkinter as tk
from PIL import ImageGrab

from test_session_manager import TkTests


def capture(test, path, window=None):
    root = window or test.root
    root.lift()
    root.update()
    test.pump(.15)
    w, h = root.winfo_width(), root.winfo_height()
    # Capture the client HWND so desktop notifications cannot obscure the UI.
    screenshot = ImageGrab.grab(window=root.winfo_id())
    assert screenshot.size == (w, h), (screenshot.size, (w, h))
    assert all(low < high for low, high in screenshot.getextrema()), "Blank window capture"
    screenshot.save(path)
    problems = []
    def inspect(widget):
        for child in widget.winfo_children():
            if isinstance(child, tk.Toplevel) or not child.winfo_ismapped():
                continue
            # Panes and overlays can intentionally be unmapped or clipped.
            if child.winfo_manager() in ("grid", "pack"):
                if (child.winfo_x() < 0 or child.winfo_y() < 0 or
                    child.winfo_x() + child.winfo_width() > widget.winfo_width() + 2 or
                    child.winfo_y() + child.winfo_height() > widget.winfo_height() + 2):
                    problems.append(str(child))
            inspect(child)
    inspect(root)
    return {"width": w, "height": h, "out_of_bounds": problems,
            "list_width": test.app.tree.winfo_width(), "row_count": len(test.app.tree.get_children()),
            "split": test.app.paned.sashpos(0) if test.app.preview_visible_var.get() else None}


def main():
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    out = Path(__file__).resolve().parents[1] / "output" / "qa"
    out.mkdir(parents=True, exist_ok=True)
    report = {}
    test = TkTests()
    test.setUp()
    try:
        app = test.open({})
        titles = ["修复窗口重启后列表宽度丢失", "增加会话搜索和分组筛选", "检查异步预览的加载性能",
                  "优化会话列表的布局与状态恢复，核对长标题显示和筛选结果", "整理项目目录和常用启动路径",
                  "核对批量分组后的会话归属", "验证高 DPI 字体与图标显示", "回归测试会话恢复命令"]
        for i, s in enumerate(test.sessions):
            s.title = f"{i + 1:03d} · {titles[i % len(titles)]}"
            s.cwd = "D:\\Projects\\" + ("会话管理工具" if i % 2 else "资料工作台")
            s.group = "日常开发" if i % 2 else "待处理"
            s.note = "核实列表宽度在重新启动之后可以恢复" if i % 3 == 0 else ""
        app._update_group_combo()
        app._apply_filter()
        app._selected_sids = {test.sessions[3].session_id}
        app._reapply_tree_selection()
        app._on_select_preview()
        test.pump(.5)
        report["desktop"] = capture(test, out / "desktop.png")
        assert len(app._icons) == 12
        app.preview_tabs.select(1)
        report["details"] = capture(test, out / "details.png")
        app.preview_tabs.select(0)

        # Exercise the native Tk paned-window drag bindings, not just the setter.
        x = app.paned.sashpos(0) + 2
        app.paned.event_generate("<ButtonPress-1>", x=x, y=180)
        app.paned.event_generate("<B1-Motion>", x=850, y=180)
        app.paned.event_generate("<ButtonRelease-1>", x=850, y=180)
        test.pump(.5)
        assert abs(app.paned.sashpos(0) - 848) < 15, app.paned.sashpos(0)
        report["drag_ratio"] = app._preferred_ratio

        app.root.geometry("940x620+20+20")
        test.pump(.2)
        report["narrow"] = capture(test, out / "narrow.png")
        app.search_var.set("极长搜索条件" * 200)
        test.pump(.25)
        report["long_filter"] = capture(test, out / "long-filter.png")
        assert app.empty_hint.winfo_y() + app.empty_hint.winfo_height() < app.empty_action.winfo_y()
        app._clear_filters()
        app._toggle_preview()
        test.pump(.2)
        report["list_only"] = capture(test, out / "list-only.png")
        app.density_var.set("compact")
        app._set_density()
        report["compact"] = capture(test, out / "compact.png")
        assert report["compact"]["row_count"] > report["list_only"]["row_count"]
        app.search_var.set("没有匹配的搜索词")
        test.pump(.25)
        report["empty"] = capture(test, out / "empty.png")
        app._clear_filters()
        app._show_new_session()
        report["new_dialog"] = capture(test, out / "new-session.png", app._new_dialog)
        app._new_dialog.destroy()
        app._selected_sids = {test.sessions[3].session_id}
        app._edit_note()
        note = next(w for w in app.root.winfo_children() if isinstance(w, tk.Toplevel))
        report["note_dialog"] = capture(test, out / "note.png", note)
        note.destroy()
        app._change_group()
        group = next(w for w in app.root.winfo_children() if isinstance(w, tk.Toplevel))
        report["group_dialog"] = capture(test, out / "group.png", group)
        group.destroy()
        test.close()

        # Tk's scaling reproduces 150% fonts/metrics on the same monitor.
        test.root = tk.Tk()
        test.root.tk.call("tk", "scaling", 2.0)
        import cli_session_manager as manager
        manager.save_settings({})
        test.root.report_callback_exception = lambda *args: test.errors.append(args)
        test.app = manager.SessionManagerApp(test.root)
        test.pump(.1)
        test.app._on_scan_done(test.sessions, True)
        test.app._selected_sids = {test.sessions[3].session_id}
        test.app._on_select_preview()
        test.pump(.5)
        report["dpi150"] = capture(test, out / "dpi150.png")
        test.app._show_new_session()
        report["dpi150_dialog"] = capture(test, out / "dpi150-new.png", test.app._new_dialog)
        test.app._new_dialog.destroy()
        test.close()
    finally:
        test.tearDown()
    (out / "visual-checks.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    assert not any(v.get("out_of_bounds") for v in report.values() if isinstance(v, dict)), report


if __name__ == "__main__":
    main()
