"""Regression tests using real Tk widgets and isolated session/settings files."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli_session_manager as manager


def fixtures(directory, count=180):
    transcript = directory / "demo.jsonl"
    lines = []
    for i in range(15):
        lines.append(json.dumps({"type": "user", "message": {"content": f"Review request {i}: session layout and persistence."}}))
        lines.append(json.dumps({"type": "assistant", "message": {"content": f"Review result {i}: verify resize, restart, and selection."}}))
    transcript.write_text("\n".join(lines), encoding="utf-8")
    sessions = []
    for i in range(count):
        session = manager.SessionInfo(session_id=f"session-{i:04}", pid=0,
                                      cwd=str(directory / f"project-{i % 5}"),
                                      started_at=1788730000000 - i * 3600000,
                                      jsonl_path=transcript,
                                      source="codex" if i % 2 else "claude")
        session.title = f"Session {i:04}: inspect list widths, session notes, and restore the workspace"
        session.note = f"Follow up {i}" if i % 3 == 0 else ""
        session.group = f"Group {i % 4}"
        session.alive = i in (2, 4)
        sessions.append(session)
    return sessions


class TkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.patches = []
        for attr, name in (("SESSION_SETTINGS_FILE", "settings.json"), ("SESSION_NOTES_FILE", "notes.json"),
                           ("SESSION_GROUPS_FILE", "groups.json"), ("SESSION_PATHS_FILE", "paths.json")):
            p = patch.object(manager, attr, self.directory / name)
            p.start()
            self.patches.append(p)
        for method in ("_refresh", "_schedule_auto_refresh"):
            p = patch.object(manager.SessionManagerApp, method)
            p.start()
            self.patches.append(p)
        self.sessions = fixtures(self.directory)
        self.errors = []
        self.app = None
        self.root = None

    def open(self, settings=None):
        if settings is not None:
            manager.save_settings(settings)
        self.root = tk.Tk()
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.app = manager.SessionManagerApp(self.root)
        self.pump(0.08)
        self.app._on_scan_done(self.sessions, True)
        self.pump(0.08)
        return self.app

    def pump(self, seconds=0.1):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.root.update()
            time.sleep(0.005)
        self.assertEqual(self.errors, [])

    def close(self):
        self.app._on_close()
        self.app = None
        self.root = None

    def tearDown(self):
        if self.app and not self.app._closed:
            self.close()
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def test_fresh_layout_has_readable_list_and_reachable_columns(self):
        app = self.open({})
        width, lower, upper = app._sash_bounds()
        self.assertAlmostEqual(app.paned.sashpos(0) / width, .68, delta=.01)
        self.assertGreater(app.tree.winfo_width(), app._px(480))
        self.assertTrue(app.hscroll.winfo_ismapped())
        self.assertEqual(app.tree["columns"][0], "title")
        for c, minimum in app._MIN_WIDTHS.items():
            self.assertGreaterEqual(int(app.tree.column(c, "width")), app._px(minimum))
        self.assertLess(len(app.tree.get_children()), len(self.sessions))

    def test_layout_autosaves_and_survives_restart(self):
        app = self.open({})
        app.root.geometry("1280x760+40+50")
        self.pump()
        app.paned.sashpos(0, app._px(700))
        app._dragging_sash = True
        app._on_sash_release()
        app.tree.column("title", width=app._px(385))
        app._resizing_column = "title"
        app._on_column_release()
        app.source_filter_var.set("codex")
        app._selected_group_real = "Group 1"
        app._update_group_combo()
        app.notes_only_var.set(True)
        app.search_var.set("Session")
        app.density_var.set("compact")
        app._set_density()
        app._apply_filter()
        self.pump(.75)
        saved = manager.load_settings()
        self.assertEqual(saved["col_widths"]["title"], app._px(385))
        self.assertEqual(saved["group_filter"], "Group 1")
        self.assertEqual(saved["source_filter"], "codex")
        self.assertTrue(saved["notes_only"])
        self.assertEqual(saved["density"], "compact")
        self.assertTrue(saved["search"])
        ratio = saved["sash_ratio"]
        self.close()
        app = self.open()
        self.assertAlmostEqual(app._preferred_ratio, ratio)
        self.assertEqual(app._column_widths["title"], saved["col_widths"]["title"])
        self.assertEqual(app._selected_group_real, "Group 1")
        self.assertEqual(len(app.filtered_sessions), 15)
        self.assertEqual(app._density, "compact")

    def test_preview_toggle_keeps_preferred_split_across_resize_restart(self):
        app = self.open({"sash_ratio": .71})
        app._toggle_preview()
        self.pump()
        self.assertEqual(len(app.paned.panes()), 1)
        app.root.geometry("940x620")
        self.pump()
        self.close()
        app = self.open()
        self.assertFalse(app.preview_visible_var.get())
        self.assertEqual(len(app.paned.panes()), 1)
        app._toggle_preview()
        self.pump()
        self.assertAlmostEqual(app._preferred_ratio, .71)
        width, lower, upper = app._sash_bounds()
        self.assertTrue(lower <= app.paned.sashpos(0) <= upper)

    def test_invalid_and_legacy_settings_recover(self):
        app = self.open({"geometry": "1x1+-9000+-200", "sash": 0,
                         "col_widths": None, "sort_col": [], "sash_ratio": "NaN",
                         "search": {}, "selected_sids": 12, "window_start": "bad"})
        self.assertEqual(app._sort_col, "time")
        self.assertEqual(app._preferred_ratio, .68)
        self.assertGreaterEqual(app.root.winfo_x(), 0)
        self.assertGreater(app.root.winfo_width(), 800)
        self.assertGreater(app.paned.sashpos(0), 400)

    def test_view_and_selection_restore_and_refresh_anchor_stays(self):
        app = self.open({})
        app._yscroll("scroll", 40, "units")
        selected = app.filtered_sessions[45].session_id
        top = app.filtered_sessions[app._window_start].session_id
        app._selected_sids = {selected}
        app._on_select_preview()
        app._save_ui_state()
        self.close()
        app = self.open()
        self.assertEqual(app._selected_sids, {selected})
        self.assertEqual(app.filtered_sessions[app._window_start].session_id, top)
        added = manager.SessionInfo(session_id="new", pid=0, cwd="", started_at=9999999999999)
        app.sessions = [added] + app.sessions
        app._apply_filter(preserve_scroll=True)
        self.assertEqual(app.filtered_sessions[app._window_start].session_id, top)

    def test_header_double_click_never_resumes(self):
        app = self.open({})
        app._selected_sids = {self.sessions[0].session_id}
        with patch.object(app, "_resume_selected") as resume:
            event = SimpleNamespace(x=40, y=8)
            app._on_double_click(event)
            resume.assert_not_called()

    def test_note_sort_keeps_notes_first_and_recent_first_both_directions(self):
        app = self.open({})
        app._sort_col = "note"
        for reverse in (False, True):
            app._sort_reverse = reverse
            app._apply_filter()
            note_rows = [s for s in app.filtered_sessions if s.note]
            self.assertEqual(app.filtered_sessions[:len(note_rows)], note_rows)
            self.assertEqual([s.started_at for s in note_rows], sorted((s.started_at for s in note_rows), reverse=True))

    def test_clear_selection_and_stale_preview_are_consistent(self):
        app = self.open({})
        app._selected_sids = {self.sessions[0].session_id}
        app._on_select_preview()
        old_generation = app._preview_generation
        app._clear_selection()
        app._publish_preview(old_generation, "old", "stale")
        self.pump(.3)
        self.assertEqual(app.preview_text.get("1.0", "end-1c"), "未选择会话")
        self.assertIn("disabled", app.resume_btn.state())
        app._select_all()
        self.assertEqual(len(app._selected_sids), len(self.sessions))
        self.assertIsNone(app._get_selected_session())
        self.assertNotIn("disabled", app.group_btn.state())

    def test_new_session_and_edit_dialogs_fit(self):
        app = self.open({})
        app._show_new_session()
        self.pump()
        dialog = app._new_dialog
        self.assertGreaterEqual(dialog.winfo_height(), dialog.winfo_reqheight())
        self.assertTrue(app._new_session_source() in manager.ALL_SOURCES)
        dialog.destroy()
        app._selected_sids = {self.sessions[0].session_id}
        app._edit_note()
        self.pump()
        dialogs = [w for w in app.root.winfo_children() if isinstance(w, tk.Toplevel)]
        self.assertEqual(len(dialogs), 1)
        self.assertGreaterEqual(dialogs[0].winfo_height(), dialogs[0].winfo_reqheight())
        dialogs[0].destroy()

    def test_preview_worker_does_not_block_event_loop(self):
        app = self.open({})
        def slow(*args):
            time.sleep(.25)
            return "loaded asynchronously"
        with patch.object(manager, "extract_preview", side_effect=slow):
            app._selected_sids = {self.sessions[0].session_id}
            app._on_select_preview()
            ticks = []
            app.root.after(130, lambda: ticks.append(True))
            self.pump(.2)
            self.assertEqual(ticks, [True])
            self.assertNotIn("loaded asynchronously", app.preview_text.get("1.0", "end"))
            self.pump(.3)
            self.assertIn("loaded asynchronously", app.preview_text.get("1.0", "end"))

    def test_native_column_drag_hidden_columns_and_restart(self):
        app = self.open({})
        app._column_vars["status"].set(False)
        app._on_columns_changed()
        self.pump()
        columns = app._display_columns()
        self.assertEqual(columns[1], "time")
        boundary = int(app.tree.column("title", "width")) + int(app.tree.column("time", "width"))
        x = next(n for n in range(boundary - 3, boundary + 4) if app.tree.identify_region(n, 8) == "separator")
        original = app._column_widths["time"]
        app.tree.event_generate("<ButtonPress-1>", x=x, y=8)
        app.tree.event_generate("<B1-Motion>", x=x + 60, y=8)
        app.tree.event_generate("<ButtonRelease-1>", x=x + 60, y=8)
        self.pump(.45)
        width = int(app.tree.column("time", "width"))
        self.assertAlmostEqual(width, original + 60, delta=4)
        self.assertEqual(app._column_widths["time"], width)
        self.close()
        app = self.open()
        self.assertEqual(app._display_columns(), columns)
        self.assertEqual(app._column_widths["time"], width)

    def test_maximized_and_minimized_window_keep_normal_geometry(self):
        app = self.open({})
        app.root.geometry("1190x710+40+50")
        self.pump(.08)
        geometry = app.root.geometry()
        app.root.state("zoomed")
        self.pump(.1)
        app._save_ui_state()
        saved = manager.load_settings()
        self.assertEqual(saved["geometry"], geometry)
        self.assertEqual(saved["window_state"], "zoomed")
        app.root.iconify()
        self.pump(.1)
        app._save_ui_state()
        self.assertEqual(manager.load_settings()["geometry"], geometry)
        self.assertEqual(manager.load_settings()["window_state"], "zoomed")
        self.close()
        app = self.open()
        self.assertEqual(app.root.state(), "zoomed")
        app.root.state("normal")
        self.pump(.15)
        self.assertEqual(app.root.geometry(), geometry)

    def test_read_failure_and_save_failure_do_not_erase_preferences(self):
        manager.SESSION_SETTINGS_FILE.write_bytes(b'\xef\xbb\xbf{"source_filter":"codex"}')
        app = self.open()
        self.assertEqual(app.source_filter_var.get(), "codex")
        app._save_ui_state()
        before = manager.SESSION_SETTINGS_FILE.read_bytes()
        app.source_filter_var.set("claude")
        with patch.object(manager.os, "replace", side_effect=OSError("disk unavailable")):
            self.assertFalse(app._save_ui_state())
        self.assertEqual(manager.SESSION_SETTINGS_FILE.read_bytes(), before)
        self.assertIn("保存失败", app.bottom_label.cget("text"))

    def test_monitor_geometry_preserves_connected_negative_monitor(self):
        app = self.open({"layout_version": 2})
        app._areas = [(0, 0, 1920, 1040), (-1600, 0, 0, 900)]
        self.assertEqual(app._safe_geometry("1000x600+-1500+50"), "1000x600+-1500+50")
        app._areas = [(0, 0, 1920, 1040)]
        self.assertEqual(app._safe_geometry("1000x600+-1500+50"), "1000x600+8+50")

    def test_batch_group_and_notes_update_filter_and_disk(self):
        app = self.open({})
        app._selected_sids = {self.sessions[0].session_id, self.sessions[1].session_id}
        app._change_group()
        self.pump()
        dialog = next(w for w in app.root.winfo_children() if isinstance(w, tk.Toplevel))
        combo = next(w for w in dialog.winfo_children() if isinstance(w, manager.ttk.Combobox))
        combo.set("Reviewed")
        combo.event_generate("<Return>")
        self.pump()
        self.assertFalse(dialog.winfo_exists())
        self.assertEqual(manager.load_groups()["assignments"], {"session-0000": "Reviewed", "session-0001": "Reviewed"})
        app.notes_only_var.set(True)
        app._apply_filter()
        app._selected_sids = {self.sessions[0].session_id}
        app._edit_note()
        self.pump()
        dialog = next(w for w in app.root.winfo_children() if isinstance(w, tk.Toplevel))
        entry = next(w for w in dialog.winfo_children() if isinstance(w, manager.ttk.Entry))
        entry.delete(0, tk.END)
        entry.event_generate("<Return>")
        self.pump()
        self.assertNotIn(self.sessions[0], app.filtered_sessions)
        self.assertEqual(app._selected_sids, set())
        self.assertNotIn("session-0000", manager.load_notes())

    def test_browsed_new_session_path_saved_with_independent_source(self):
        app = self.open({"source_filter": "claude"})
        app.new_source_var.set("codex")
        with patch("tkinter.filedialog.askdirectory", return_value=str(self.directory)), \
                patch.object(manager, "launch_windows_session") as launch, \
                patch.object(manager, "_find_git_bash_path", return_value=""):
            self.assertTrue(app._open_new_session(app.new_session_path_var1))
            self.assertEqual(manager.load_new_session_paths()[0], str(self.directory))
            self.assertIn("codex", launch.call_args.kwargs["argv"][0])
            self.assertEqual(launch.call_args.kwargs["cwd"], str(self.directory))
            self.assertEqual(app.source_filter_var.get(), "claude")

    def test_icons_have_standard_library_fallback(self):
        with patch.object(manager.tk, "PhotoImage", side_effect=tk.TclError("missing atlas")):
            app = self.open({})
        self.assertEqual(app._icons, {})
        self.assertTrue(app.preview_btn.cget("text"))
        app.preview_btn.invoke()
        self.assertFalse(app.preview_visible_var.get())

    def test_horizontal_position_and_reset_layout(self):
        app = self.open({})
        app.root.geometry("940x620+40+50")
        self.pump()
        app._xscroll("moveto", .25)
        self.pump()
        xview = app.tree.xview()[0]
        self.assertGreater(xview, .1)
        self.close()
        app = self.open()
        self.assertAlmostEqual(app.tree.xview()[0], xview, delta=.01)
        app._reset_layout()
        self.pump()
        self.assertEqual(app.tree.xview()[0], 0)
        self.assertAlmostEqual(app._preferred_ratio, .68)
        self.assertEqual(app._column_widths, {col: app._px(width) for col, width in app._DEFAULT_WIDTHS.items()})

    def test_closing_cancels_pending_callbacks(self):
        app = self.open({})
        app._selected_sids = {self.sessions[0].session_id}
        app._on_select_preview()
        interpreter = self.root.tk
        self.close()
        self.assertEqual(interpreter.call("after", "info"), "")


class SettingsTests(unittest.TestCase):
    def test_non_finite_and_booleans(self):
        for value in (None, {}, [], True, "NaN", "inf", float("nan")):
            self.assertEqual(manager._number(value, .68, .2, .9), .68)

    def test_small_claude_transcript_contains_latest_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = fixtures(Path(directory), 1)
            preview = manager.extract_preview(rows[0].jsonl_path)
            self.assertIn("Review request 0:", preview)
            self.assertIn("Review result 14:", preview)
            self.assertIn("中间省略", preview)

    @unittest.skipUnless(os.name == "nt", "Windows single-instance protocol")
    def test_second_process_signals_without_terminating_owner(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(manager, "HOME", Path(directory)), \
                patch.object(manager, "_lock_path", return_value=str(Path(directory) / "instance.lock")):
            self.assertTrue(manager._acquire_single_instance())
            try:
                code = ("import cli_session_manager as m; from pathlib import Path; "
                        f"m.HOME = Path({directory!r}); "
                        "print(m._acquire_single_instance())")
                result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=15,
                                        cwd=Path(manager.__file__).parent)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "False")
                import ctypes
                from ctypes import wintypes
                wait = ctypes.windll.kernel32.WaitForSingleObject
                wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                self.assertEqual(wait(manager._instance_event, 0), 0)
            finally:
                manager._release_single_instance()


if __name__ == "__main__":
    unittest.main(verbosity=2)
