"""Real Claude TUI startup check, no model request; --keep-open permits manual probing."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tkinter as tk
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli_session_manager as manager
import session_launcher as launcher
from windows_console_probe import Console


def main():
    workspace = Path(__file__).resolve().parents[1] / "output" / "qa" / "claude-project"
    workspace.mkdir(parents=True, exist_ok=True)
    processes = []
    requests = []
    popen = subprocess.Popen

    def capture(command, **options):
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        options["startupinfo"] = startup
        candidate = options.get("env", {}).get("_CLI_SESSION_ARG_4")
        if candidate and candidate.endswith(".json"):
            requests.append(candidate)
        process = popen(command, **options)
        processes.append(process)
        return process

    direct = "--direct" in sys.argv
    keep_open = "--keep-open" in sys.argv
    moved = "--moved" in sys.argv
    invalid = "--invalid-settings" in sys.argv
    if invalid:
        workspace = workspace.with_name("claude-invalid-project")
        (workspace / ".claude").mkdir(parents=True, exist_ok=True)
        (workspace / ".claude/settings.json").write_text('{"permissions":', encoding="utf-8")
    if moved:
        workspace = workspace.with_name("claude-project-moved 中文 & !")
        workspace.mkdir(exist_ok=True)
    arguments = [arg for arg in sys.argv[1:]
                 if arg not in ("--direct", "--moved", "--invalid-settings", "--keep-open")]
    argv = [manager._cmd_for_source("claude"), *arguments]
    with patch.object(launcher.subprocess, "Popen", side_effect=capture):
        if direct:
            command, env = launcher.windows_command(argv, keep_open=True)
            env["CLAUDE_CODE_GIT_BASH_PATH"] = manager._find_git_bash_path()
            capture(command, env=env, cwd=str(workspace), creationflags=subprocess.CREATE_NEW_CONSOLE)
            request = None
        elif "--resume" in arguments:
            sid = arguments[arguments.index("--resume") + 1]
            paths = list(manager.CLAUDE_PROJECTS.glob(f"*/{sid}.jsonl"))
            session = manager.SessionInfo(session_id=sid, pid=0,
                                          cwd=str(workspace / "missing") if moved else str(workspace),
                                          started_at=0, source="claude", jsonl_path=paths[0])
            with patch.object(manager.messagebox, "showinfo"), \
                    patch("tkinter.filedialog.askdirectory", return_value=str(workspace)):
                assert manager.resume_session(session, skip_permissions="--dangerously-skip-permissions" in arguments)
            request = None
        else:
            root = tk.Tk()
            root.withdraw()
            app = SimpleNamespace(root=root, _new_session_source=lambda: "claude",
                                  _save_paths=lambda: True,
                                  skip_perms_var=tk.BooleanVar(root, value=False))
            try:
                assert manager.SessionManagerApp._open_new_session(app, tk.StringVar(root, value=str(workspace)))
            finally:
                root.destroy()
            request = requests[-1]
    process = processes[-1]
    time.sleep(2)
    console = Console(process.pid)
    state = {"pid": process.pid, "request": requests[-1] if requests else None, "cwd": str(workspace)}
    suffix = "-direct" if direct else ""
    (workspace.parent / f"claude-console-state{suffix}.json").write_text(json.dumps(state), encoding="utf-8")
    with (workspace.parent / "claude-test-consoles.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(state) + "\n")
    try:
        for _ in range(1):
            text = console.read()
            print(text[-7000:], flush=True)
            # A separate probe can attach to this test PID to supply input.
            if process.poll() is not None:
                break
            time.sleep(2)
    finally:
        if not keep_open:
            text = console.read()
            if "Settings Error" in text:
                console.send("\x1b")
            elif "会话已结束" not in text:
                console.send("/exit")
                time.sleep(.8)
                console.send("\r")
            time.sleep(2)
            console.send("exit\r")
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                launcher._stop_process(process)
        console.close()


if __name__ == "__main__":
    main()
