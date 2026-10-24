"""Isolated real Codex startup/resume checks; never sends a model turn."""

import json
import hashlib
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli_session_manager as manager
import session_launcher as launcher


def rpc_probe(directory, arguments, method, params):
    env = dict(os.environ, CODEX_HOME=str(directory))
    command, env = launcher.windows_command(
        [manager._find_codex_cmd(), *arguments, "app-server"], env=env)
    process = subprocess.Popen(command, env=env, cwd=directory,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                               creationflags=subprocess.CREATE_NO_WINDOW)
    responses = queue.Queue()

    def read():
        try:
            for line in process.stdout:
                try:
                    responses.put(json.loads(line))
                except ValueError:
                    pass
        finally:
            responses.put(None)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()

    def request(identifier, name, values):
        process.stdin.write(json.dumps({"id": identifier, "method": name, "params": values}) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + 20
        while True:
            value = responses.get(timeout=max(0, deadline - time.monotonic()))
            if value is None:
                raise RuntimeError("Server exited before responding")
            if value.get("id") == identifier:
                return value

    try:
        init = request(1, "initialize", {"clientInfo": {"name": "launch-smoke", "version": "1"},
                                         "capabilities": {"experimentalApi": True}})
        assert "result" in init, init
        return request(2, method, params)
    finally:
        try:
            launcher._stop_process(process)
        finally:
            process.stdin.close()
            reader.join(timeout=2)
            if not reader.is_alive():
                process.stdout.close()


def main():
    with isolated_directory() as temp:
        directory = Path(temp)
        config_path = directory / "config.toml"
        config_path.write_text(
            'model_provider = "current"\nmodel = "test-model"\n'
            '[model_providers.current]\nname = "current"\n'
            'base_url = "http://127.0.0.1:1/v1"\nwire_api = "responses"\n', encoding="utf-8")
        result = rpc_probe(directory, [], "thread/start", {"cwd": temp, "persistExtendedHistory": True})
        assert "result" in result, result
        thread = result["result"]["thread"]
        assert result["result"]["modelProvider"] == "current", result
        report = {"new_thread": "passed", "model_turns_sent": 0}
        path = Path(thread["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [json.dumps({"timestamp": "2026-10-01T00:00:00Z", "type": "session_meta",
                            "payload": {"id": thread["id"], "timestamp": "2026-10-01T00:00:00Z",
                                        "cwd": temp, "originator": "codex_cli_rs", "cli_version": "0.159.2",
                                        "source": "cli", "model_provider": "custom"}})]
        for index, line in enumerate(rows):
            obj = json.loads(line)
            if obj.get("type") == "session_meta":
                obj["payload"]["model_provider"] = "custom"
                rows[index] = json.dumps(obj)
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        params = {"threadId": thread["id"], "path": str(path), "modelProvider": "custom"}
        result = rpc_probe(directory, [], "thread/resume", params)
        assert "error" in result and "custom" in result["error"]["message"], result
        report["missing_custom_reproduced"] = result["error"]["message"]
        config = rpc_probe(directory, [], "config/read", {"cwd": temp})["result"]["config"]
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        config_before = config_path.read_bytes()
        arguments, _ = launcher.codex_resume_overrides(path, config)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before
        # app-server takes model through -c; the TUI takes --model.
        index = arguments.index("--model")
        arguments[index:index + 2] = ["-c", "model=" + arguments[index + 1]]
        result = rpc_probe(directory, arguments, "thread/resume", params)
        assert "result" in result, result
        assert result["result"]["modelProvider"] == "custom", result
        report["historical_alias_real_resume"] = "passed"
        result = rpc_probe(directory, arguments, "thread/resume", dict(params, modelProvider="current"))
        assert "result" in result and result["result"]["modelProvider"] == "current", result
        assert config_path.read_bytes() == config_before
        report.update(current_route_real_resume="passed", config_unchanged=True,
                      preparation_keeps_history_unchanged=True)
        output = Path(__file__).resolve().parents[1] / "output" / "qa" / "codex-protocol-checks.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(report, indent=2, ensure_ascii=False))


@contextmanager
def isolated_directory():
    directory = tempfile.TemporaryDirectory(prefix="codex-launch-smoke-")
    try:
        yield directory.name
    finally:
        # Windows may finish releasing a killed descendant's SQLite handles
        # just after taskkill returns. Retry only this test-owned directory.
        target = Path(directory.name).resolve()
        assert target.parent == Path(tempfile.gettempdir()).resolve()
        assert target.name.startswith("codex-launch-smoke-")
        deadline = time.monotonic() + 5
        while True:
            try:
                directory.cleanup()
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.1)


if __name__ == "__main__":
    main()
