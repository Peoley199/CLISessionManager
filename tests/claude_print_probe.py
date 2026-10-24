"""Opt-in direct CLI model request, recording only non-secret result fields."""
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli_session_manager as manager
import session_launcher as launcher

argv = [manager._cmd_for_source("claude"), "-p", "--output-format", "json",
        "--max-budget-usd", "1", "Reply exactly CLAUDE_NEW_OK. Do not use tools."]
command, env = launcher.windows_command(argv)
env["CLAUDE_CODE_GIT_BASH_PATH"] = manager._find_git_bash_path()
process = subprocess.Popen(command, env=env, cwd=Path(__file__).resolve().parent.parent / "output/qa/claude-project",
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW)
try:
    stdout, stderr = process.communicate(timeout=90)
    result = json.loads(stdout)
    report = {k: result.get(k) for k in ("result", "is_error", "subtype", "session_id", "num_turns")}
    report["exit_code"] = process.returncode
    report["stderr_present"] = bool(stderr)
    out = Path(__file__).resolve().parent.parent / "output/qa/claude-direct-check.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
finally:
    launcher._stop_process(process)
