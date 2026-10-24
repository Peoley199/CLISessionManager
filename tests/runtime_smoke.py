"""Read real session data through main(); isolate all settings and instance state."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli_session_manager as manager


def child(directory, mode):
    manager.SESSION_SETTINGS_FILE = directory / "settings.json"
    manager.SESSION_PATHS_FILE = directory / "paths.json"
    manager._lock_path = lambda: str(directory / "instance.lock")
    acquire = manager._acquire_single_instance
    def isolated_acquire():
        original = manager.HOME
        try:
            manager.HOME = directory
            return acquire()
        finally:
            manager.HOME = original
    manager._acquire_single_instance = isolated_acquire

    class Probe(manager.SessionManagerApp):
        def __init__(self, root):
            self.started = time.perf_counter()
            self.phase = "loading"
            self.errors = []
            self.ticks = 0
            super().__init__(root)
            root.report_callback_exception = lambda *args: self.errors.append(str(args))
            root.after(50, self.tick)

        def snapshot(self):
            return {"geometry": self.root.geometry(), "normal_geometry": self._normal_geometry,
                    "window_state": self.root.state(), "ratio": self._preferred_ratio,
                    "list_width": self.tree.winfo_width(), "widths": self._column_widths,
                    "source": self.source_filter_var.get(), "columns": list(self._display_columns()),
                    "selected": sorted(self._selected_sids),
                    "top": self.filtered_sessions[self._window_start].session_id if self.filtered_sessions else None,
                    "count": len(self.sessions), "errors": self.errors}

        def tick(self):
            self.ticks += 1
            if time.perf_counter() - self.started > 25:
                raise RuntimeError("Runtime smoke timed out")
            if self.phase == "loading" and self._view_restored and not self._scanning:
                assert self.sessions, "No real sessions were loaded"
                assert self.ticks >= 2, "Scan was not observed asynchronously"
                if mode == "restart":
                    expected = json.loads((directory / "configured.json").read_text(encoding="utf-8"))
                    actual = self.snapshot()
                    for key in ("geometry", "normal_geometry", "ratio", "widths", "source", "columns", "selected", "top"):
                        assert actual[key] == expected[key], (key, actual[key], expected[key])
                    assert not self.errors, self.errors
                    manager._atomic_write_json(directory / "restart.json", actual)
                    self._on_close()
                    return
                self.phase = "configuring"
                self.load_seconds = time.perf_counter() - self.started
                self.root.geometry("1280x760+40+50")
                self._preferred_ratio = .64
                self._restore_sash()
                self._column_widths["note"] = self._px(210)
                self._column_vars["group"].set(True)
                self._on_columns_changed()
                self.source_filter_var.set("codex")
                self._apply_filter()
                self._yscroll("scroll", 40, "units")
                self._selected_sids = {self.filtered_sessions[self._window_start + 2].session_id}
                self._on_select_preview()
                self.root.after(900, self.ready)
            if self.phase == "ready" and self.root.state() != "iconic":
                assert not self.errors, self.errors
                manager._atomic_write_json(directory / "activated.json", {"pid": os.getpid(), "state": self.root.state()})
                self.phase = "activated"
            if (directory / "stop.json").exists():
                self._on_close()
                return
            self.root.after(50, self.tick)

        def ready(self):
            saved = manager.load_settings()
            assert saved["col_widths"]["note"] == self._px(210), saved
            assert saved["sash_ratio"] == .64, saved
            snapshot = self.snapshot()
            snapshot["load_seconds"] = round(self.load_seconds, 3)
            snapshot["responsive_ticks"] = self.ticks
            manager._atomic_write_json(directory / "configured.json", snapshot)
            self.root.iconify()
            self.phase = "ready"
            manager._atomic_write_json(directory / "ready.json", {"pid": os.getpid()})

    manager.SessionManagerApp = Probe
    manager.main()


def wait_file(path, process, timeout=25):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        if process.poll() is not None:
            raise AssertionError(process.communicate())
        time.sleep(.05)
    raise TimeoutError(str(path))


def main():
    with tempfile.TemporaryDirectory() as temp:
        directory = Path(temp)
        def start(mode):
            return subprocess.Popen([sys.executable, __file__, "--child", str(directory), mode],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        process = start("first")
        processes = [process]
        try:
            owner = wait_file(directory / "ready.json", process)
            duplicate = start("duplicate")
            processes.append(duplicate)
            stdout, stderr = duplicate.communicate(timeout=10)
            assert duplicate.returncode == 0 and not stderr, (stdout, stderr)
            activation = wait_file(directory / "activated.json", process)
            assert activation["pid"] == owner["pid"] and process.poll() is None
            (directory / "stop.json").write_text("{}", encoding="utf-8")
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == 0 and not stderr, (stdout, stderr)
            (directory / "stop.json").unlink()
            restarted = start("restart")
            processes.append(restarted)
            result = wait_file(directory / "restart.json", restarted)
            stdout, stderr = restarted.communicate(timeout=10)
            assert restarted.returncode == 0 and not stderr, (stdout, stderr)
            configured = json.loads((directory / "configured.json").read_text(encoding="utf-8"))
            report = {"real_session_count": result["count"], "initial_load_seconds": configured["load_seconds"],
                      "responsive_ticks": configured["responsive_ticks"], "duplicate_reused_process": True,
                      "restart_preserved_layout_filters_selection_scroll": True,
                      "normal_geometry": result["normal_geometry"], "list_width": result["list_width"],
                      "callback_errors": result["errors"]}
            output = Path(__file__).resolve().parents[1] / "output" / "qa" / "runtime-checks.json"
            manager._atomic_write_json(output, report)
            print(json.dumps(report, indent=2))
        finally:
            for proc in processes:
                if proc.poll() is None:
                    proc.terminate()
                    proc.communicate(timeout=10)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        child(Path(sys.argv[2]), sys.argv[3])
    else:
        main()
