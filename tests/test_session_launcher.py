"""Exercise actual Windows CMD and npm-style shims, without contacting a model."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import session_launcher as launcher
import cli_session_manager as manager


SID = "01a043e8-01fe-7f10-b1ac-a4bde123c594"
CONFIG = {"model_provider": "current", "model": "current-model",
          "model_providers": {"current": {
              "name": "current", "base_url": "http://localhost:23000/v1",
              "wire_api": "responses", "requires_openai_auth": True}}}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rollout = Path(self.temp.name) / "rollout.jsonl"

    def write_meta(self, provider):
        self.rollout.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": SID, "model_provider": provider}}) + "\n", encoding="utf-8")

    def test_missing_provider_has_current_route_and_historical_alias(self):
        self.write_meta("custom")
        original = self.rollout.read_bytes()
        args, notice = launcher.codex_resume_overrides(self.rollout, CONFIG)
        self.assertEqual(args[:4], ["-c", "model_provider='current'", "--model", "current-model"])
        import tomllib
        alias = tomllib.loads(args[5])["model_providers"]["custom"]
        self.assertEqual(alias, dict(CONFIG["model_providers"]["current"], name="custom"))
        self.assertIn("custom", notice)
        self.assertEqual(self.rollout.read_bytes(), original)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [self.rollout])

    def test_rpc_null_fields_are_omitted_recursively(self):
        self.write_meta("custom")
        config = dict(CONFIG, model_providers={"current": dict(
            CONFIG["model_providers"]["current"], env_key=None,
            http_headers={"X-Test": "test", "unset": None})})
        args, _ = launcher.codex_resume_overrides(self.rollout, config)
        import tomllib
        alias = tomllib.loads(args[5])["model_providers"]["custom"]
        self.assertNotIn("env_key", alias)
        self.assertEqual(alias["http_headers"], {"X-Test": "test"})

    def test_existing_builtin_and_unset_providers_keep_original_settings(self):
        for provider in ("current", "openai", "ollama", "lmstudio", None):
            with self.subTest(provider=provider):
                self.write_meta(provider)
                self.assertEqual(launcher.codex_resume_overrides(self.rollout, CONFIG), ([], ""))

    def test_incomplete_current_config_is_not_used_for_migration(self):
        self.write_meta("custom")
        for config in (dict(CONFIG, model=None), dict(CONFIG, model_provider="missing")):
            with self.subTest(config=config), self.assertRaises(ValueError):
                launcher.codex_resume_overrides(self.rollout, config)

    def test_latest_thread_provider_and_malformed_records(self):
        self.rollout.write_text("\n".join([
            "null", "[]", "{", '{"type":"session_meta","payload":null}',
            json.dumps({"type": "session_meta", "payload": {"model_provider": "current"}}),
            json.dumps({"type": "thread_settings_applied", "thread_settings": {
                "model_provider_id": "custom"}}),
            '{"type":"thread_settings_applied","thread_settings":[]}',
            '{"type":"thread_settings_applied","thread_settings":{"model_provider_id":42}}',
        ]) + "\n", encoding="utf-8")
        args, notice = launcher.codex_resume_overrides(self.rollout, CONFIG)
        self.assertIn("model_providers.custom=", args[5])
        self.assertIn("custom", notice)

    def test_unreadable_rollout_does_not_silently_skip_check(self):
        with self.assertRaisesRegex(ValueError, "provider"):
            launcher.codex_resume_overrides(self.rollout, CONFIG)

    def test_invalid_new_session_configuration_is_rejected(self):
        for config in (None, [], {"model_providers": ["custom"]},
                       dict(CONFIG, model_provider="missing")):
            with self.subTest(config=config), self.assertRaises(ValueError):
                launcher.validate_codex_config(config)

    def test_unsupported_alias_values_fail_without_exposing_the_value(self):
        self.write_meta("custom")
        secret = 'secret"value'
        config = dict(CONFIG, model_providers={"current": {"name": "current", "env_key": secret}})
        with self.assertRaises(ValueError) as raised:
            launcher.codex_resume_overrides(self.rollout, config)
        self.assertNotIn(secret, str(raised.exception))

    def test_non_ascii_provider_key_is_valid_toml(self):
        self.write_meta("旧.provider")
        args, _ = launcher.codex_resume_overrides(self.rollout, CONFIG)
        import tomllib
        self.assertIn("旧.provider", tomllib.loads(args[5])["model_providers"])

    def test_command_is_resolved_again_for_each_launch(self):
        with patch.object(manager, "_find_codex_cmd", side_effect=["first.cmd", "second.cmd"]):
            self.assertEqual(manager._cmd_for_source("codex"), "first.cmd")
            self.assertEqual(manager._cmd_for_source("codex"), "second.cmd")
        with self.assertRaises(ValueError):
            manager._cmd_for_source("unknown")

    def test_path_cli_precedes_old_install_locations(self):
        with patch.object(manager.shutil, "which", return_value="path-cli.cmd"):
            self.assertEqual(manager._find_codex_cmd(), "path-cli.cmd")
            self.assertEqual(manager._find_claude_official_cmd(), "path-cli.cmd")

    def test_codex_home_controls_all_codex_data_sources(self):
        home = Path(self.temp.name)
        (home / "session_index.jsonl").write_text(
            json.dumps({"id": SID, "thread_name": "isolated title"}) + "\n", encoding="utf-8")
        (home / "history.jsonl").write_text(
            json.dumps({"session_id": SID, "text": "isolated history"}) + "\n", encoding="utf-8")
        script = ("import json, cli_session_manager as m; print(json.dumps(["
                  "str(m.CODEX_SESSIONS_DIR), m._load_codex_thread_titles(), m._load_codex_history_titles()]))")
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", script],
                                cwd=Path(__file__).resolve().parents[1],
                                env=dict(os.environ, CODEX_HOME=str(home)),
                                capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         [str(home / "sessions"), {SID: "isolated title"}, {SID: "isolated history"}])

    def test_claude_config_dir_controls_all_claude_data_sources(self):
        home = Path(self.temp.name)
        script = ("import json, cli_session_manager as m; print(json.dumps("
                  "{k:str(v) for k,v in m.SOURCE_DIRS['claude'].items()}))")
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", script],
                                cwd=Path(__file__).resolve().parents[1],
                                env=dict(os.environ, CLAUDE_CONFIG_DIR=str(home)),
                                capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"sessions": str(home / "sessions"),
                         "projects": str(home / "projects"), "history": str(home / "history.jsonl")})


@unittest.skipUnless(os.name == "nt", "Windows console launcher")
class WindowsLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cli launch %CLI_TEST_EXPAND% & ! (中文) ")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.cwd = self.directory / "project %CLI_TEST_EXPAND% & ! (中文)"
        self.cwd.mkdir()
        self.capture = self.directory / "capture.py"
        self.capture.write_text('''import json, os, pathlib, subprocess, sys, time
if sys.argv[1:] == ["app-server"]:
    for line in sys.stdin:
        request = json.loads(line)
        method = request["method"]
        with open(os.environ["CLI_TEST_TRACE"], "a", encoding="utf-8") as f:
            f.write(method + "\\n")
        if "id" not in request:
            continue
        if method == "initialize" and os.environ.get("CLI_TEST_DESCENDANT"):
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            pathlib.Path(os.environ["CLI_TEST_DESCENDANT"]).write_text(str(child.pid))
        if os.environ.get("CLI_TEST_STALL"):
            time.sleep(60)
        if method == "config/read" and os.environ.get("CLI_TEST_CONFIG_ERROR"):
            result = {"error": {"code": -1, "message": "config unavailable"}}
        elif method == "config/read":
            result = {"result": {"config": json.loads(os.environ["CLI_TEST_CONFIG"])}}
        elif method == "model/list":
            result = {"result": {"data": [{"model": "cli-default", "isDefault": True}]}}
        else:
            result = {"result": {}}
        print(json.dumps(dict(result, id=request["id"])), flush=True)
else:
    pathlib.Path(os.environ["CLI_TEST_RECORD"]).write_text(json.dumps({
        "argv": sys.argv[1:], "cwd": os.getcwd(),
        "git_bash": os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")}), encoding="utf-8")
    print("CLI_STDOUT", flush=True)
    print("CLI_STDERR", file=sys.stderr, flush=True)
    sys.exit(int(os.environ.get("CLI_TEST_EXIT", "17")))
''', encoding="utf-8")
        self.shim = self.directory / "fake cli %CLI_TEST_EXPAND% & ! (中文).cmd"
        self.shim.write_text('@echo off\n"%CLI_TEST_PYTHON%" "%CLI_TEST_CAPTURE%" %*\n'
                             'exit /b %errorlevel%\n', encoding="utf-8")
        self.record = self.directory / "record.json"
        self.trace = self.directory / "rpc.txt"
        self.env = {"CLI_TEST_PYTHON": sys.executable, "CLI_TEST_CAPTURE": str(self.capture),
                    "CLI_TEST_RECORD": str(self.record), "CLI_TEST_TRACE": str(self.trace),
                    "CLI_TEST_CONFIG": json.dumps(CONFIG), "CLI_TEST_EXPAND": "MUST_NOT_EXPAND"}
        self.env_patch = patch.dict(os.environ, self.env)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.processes = []
        self.requests = []
        self.addCleanup(self.cleanup_launches)

    def cleanup_launches(self):
        for process in self.processes:
            if process.poll() is None:
                launcher._stop_process(process)
            process.communicate(timeout=5)
        for path in self.requests:
            path.unlink(missing_ok=True)
            path.with_suffix(".log").unlink(missing_ok=True)

    def launch(self, **kwargs):
        popen = subprocess.Popen

        def hidden_console(command, **options):
            options.update(creationflags=subprocess.CREATE_NO_WINDOW, stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            process = popen(command, **options)
            self.processes.append(process)
            return process

        defaults = {"argv": [str(self.shim)], "cwd": str(self.cwd), "source": "claude"}
        defaults.update(kwargs)
        with patch.object(launcher.subprocess, "Popen", side_effect=hidden_console):
            path = launcher.launch_windows_session(**defaults)
        self.requests.append(path)
        return path, self.processes[-1]

    def finish(self, path, process, code=17):
        deadline = time.monotonic() + 15
        log = ""
        while time.monotonic() < deadline:
            if path.with_suffix(".log").exists():
                log = path.with_suffix(".log").read_text(encoding="utf-8")
                if "会话已结束" in log:
                    break
            if process.poll() is not None:
                self.fail(f"CMD closed before showing the result: {process.communicate()!r}")
            time.sleep(.02)
        self.assertIn(f"退出码：{code}", log)
        self.assertIsNone(process.poll(), "CMD must remain open after the CLI exits")
        output, _ = process.communicate(b"exit\r\n", timeout=5)
        self.assertFalse(path.exists(), "Consumed launch requests should be removed")
        return output, log

    def test_cmd_shim_returns_keeps_stderr_and_preserves_special_paths(self):
        argv = [str(self.shim), "--resume", SID, "--dangerously-skip-permissions",
                "literal %CLI_TEST_EXPAND% & ! (中文)"]
        bash = str(self.directory / "git bash.exe")
        path, process = self.launch(argv=argv, git_bash=bash)
        output, _ = self.finish(path, process)
        actual = json.loads(self.record.read_text(encoding="utf-8"))
        self.assertEqual(actual, {"argv": argv[1:], "cwd": str(self.cwd), "git_bash": bash})
        self.assertIn(b"CLI_STDOUT", output)
        self.assertIn(b"CLI_STDERR", output)

    def test_codex_resume_migrates_provider_through_real_cmd_and_rpc(self):
        rollout = self.directory / "rollout.jsonl"
        rollout.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": SID, "model_provider": "current"}}), encoding="utf-8")
        original = rollout.read_bytes()
        argv = [str(self.shim), "resume", SID, "--dangerously-bypass-approvals-and-sandbox"]
        path, process = self.launch(argv=argv, source="codex", rollout_path=rollout)
        _, log = self.finish(path, process)
        actual = json.loads(self.record.read_text(encoding="utf-8"))
        self.assertEqual(actual["argv"], argv[1:])
        self.assertEqual(rollout.read_bytes(), original)
        self.assertEqual(self.trace.read_text(encoding="utf-8").splitlines(),
                         ["initialize", "initialized", "config/read"])

    def test_new_session_success_also_keeps_terminal_open(self):
        with patch.dict(os.environ, {"CLI_TEST_EXIT": "0"}):
            path, process = self.launch(source="codex")
        self.finish(path, process, code=0)
        self.assertEqual(json.loads(self.record.read_text(encoding="utf-8"))["argv"], [])
        self.assertEqual(self.trace.read_text(encoding="utf-8").splitlines(),
                         ["initialize", "initialized", "config/read"])

    def test_missing_custom_alias_reaches_cli_through_cmd(self):
        rollout = self.directory / "rollout.jsonl"
        rollout.write_text('{"type":"session_meta","payload":{"model_provider":"custom"}}', encoding="utf-8")
        original = rollout.read_bytes()
        path, process = self.launch(source="codex", argv=[str(self.shim), "resume", SID],
                                    rollout_path=rollout)
        _, log = self.finish(path, process)
        actual = json.loads(self.record.read_text(encoding="utf-8"))["argv"]
        expected, _ = launcher.codex_resume_overrides(rollout, CONFIG)
        self.assertEqual(actual, ["resume", SID, *expected])
        self.assertEqual(rollout.read_bytes(), original)
        self.assertIn("custom", log)
        self.assertNotIn("http://localhost", log)

    def test_invalid_new_session_provider_never_runs_cli(self):
        with patch.dict(os.environ, {"CLI_TEST_CONFIG": json.dumps(dict(CONFIG, model_provider="missing"))}):
            path, process = self.launch(source="codex")
        _, log = self.finish(path, process, code=1)
        self.assertIn("missing", log)
        self.assertFalse(self.record.exists())

    def test_missing_directory_never_runs_cli(self):
        path, process = self.launch(cwd=str(self.cwd / "missing"))
        _, log = self.finish(path, process, code=1)
        self.assertIn("项目目录不存在", log)
        self.assertFalse(self.record.exists())

    def test_missing_cli_error_is_visible_and_window_remains_open(self):
        path, process = self.launch(argv=[str(self.directory / "missing cli.cmd")])
        output, _ = self.finish(path, process, code=1)
        self.assertIn(b"missing cli.cmd", output)

    def test_default_model_is_resolved_by_codex(self):
        config = dict(CONFIG, model=None)
        with patch.dict(os.environ, {"CLI_TEST_CONFIG": json.dumps(config)}):
            result = launcher.read_codex_config(str(self.shim), str(self.cwd))
        self.assertEqual(result["model"], "cli-default")
        self.assertIn("model/list", self.trace.read_text(encoding="utf-8"))

    def test_configuration_failure_is_reported_before_original_cli_launch(self):
        rollout = self.directory / "rollout.jsonl"
        rollout.write_text('{"type":"session_meta","payload":{"model_provider":"cch"}}', encoding="utf-8")
        with patch.dict(os.environ, {"CLI_TEST_CONFIG_ERROR": "1"}):
            path, process = self.launch(source="codex", argv=[str(self.shim), "resume", SID],
                                        rollout_path=rollout)
        _, log = self.finish(path, process)
        self.assertIn("config unavailable", log)
        self.assertEqual(json.loads(self.record.read_text(encoding="utf-8"))["argv"], ["resume", SID])

    def test_configuration_timeout_cleans_up_its_process(self):
        with patch.dict(os.environ, {"CLI_TEST_STALL": "1"}):
            with self.assertRaisesRegex(RuntimeError, "超时"):
                launcher.read_codex_config(str(self.shim), str(self.cwd), timeout=.2)

    def test_configuration_cleanup_handles_inherited_stdout_and_kills_descendant(self):
        pid_file = self.directory / "descendant.pid"
        started = time.monotonic()
        with patch.dict(os.environ, {"CLI_TEST_DESCENDANT": str(pid_file)}):
            config = launcher.read_codex_config(str(self.shim), str(self.cwd), timeout=2)
        self.assertEqual(config, CONFIG)
        self.assertLess(time.monotonic() - started, 4)
        pid = int(pid_file.read_text())
        result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertNotIn(f'"{pid}"'.encode(), result.stdout)

    def test_repeated_launches_use_distinct_requests_and_logs(self):
        first, p1 = self.launch()
        second, p2 = self.launch()
        self.assertNotEqual(first, second)
        self.finish(first, p1)
        self.finish(second, p2)

    def test_failed_terminal_creation_cleans_request(self):
        with patch.object(launcher.tempfile, "tempdir", str(self.directory)), \
                patch.object(launcher.subprocess, "Popen", side_effect=OSError("cannot open console")):
            with self.assertRaises(OSError):
                launcher.launch_windows_session(argv=[str(self.shim)], cwd=str(self.cwd), source="claude")
        self.assertEqual(list(self.directory.glob("cli_session_*.json")), [])

    def test_resume_entry_passes_selected_directory_source_and_permissions(self):
        session = manager.SessionInfo(session_id=SID, pid=0, cwd=str(self.cwd / "missing"),
                                      started_at=0, source="codex", jsonl_path=self.directory / "rollout.jsonl")
        with patch.object(manager.messagebox, "showinfo"), \
                patch("tkinter.filedialog.askdirectory", return_value=str(self.cwd)), \
                patch.object(manager, "launch_windows_session") as launch:
            self.assertTrue(manager.resume_session(session, skip_permissions=True))
        args = launch.call_args.kwargs
        self.assertEqual(args["cwd"], str(self.cwd))
        self.assertEqual(args["source"], "codex")
        self.assertEqual(args["argv"][1:], ["resume", SID, "--dangerously-bypass-approvals-and-sandbox"])
        self.assertEqual(args["rollout_path"], session.jsonl_path)

    def test_claude_moved_directory_resumes_selected_file(self):
        transcript = self.directory / f"{SID}.jsonl"
        transcript.write_text('{}\n', encoding="utf-8")
        session = manager.SessionInfo(session_id=SID, pid=0, cwd=str(self.cwd / "missing"),
                                      started_at=0, source="claude", jsonl_path=transcript)
        for skip in (False, True):
            with self.subTest(skip=skip), patch.object(manager.messagebox, "showinfo"), \
                    patch("tkinter.filedialog.askdirectory", return_value=str(self.cwd)), \
                    patch.object(manager, "launch_windows_session") as launch:
                self.assertTrue(manager.resume_session(session, skip_permissions=skip))
            arguments = launch.call_args.kwargs
            self.assertEqual(arguments["cwd"], str(self.cwd))
            expected = ["--resume", str(transcript.resolve())]
            if skip:
                expected.append("--dangerously-skip-permissions")
            self.assertEqual(arguments["argv"][1:], expected)

    def test_claude_resume_canceled_directory_does_not_launch(self):
        session = manager.SessionInfo(session_id=SID, pid=0, cwd=str(self.cwd / "missing"),
                                      started_at=0, source="claude")
        with patch.object(manager.messagebox, "showinfo"), \
                patch("tkinter.filedialog.askdirectory", return_value=""), \
                patch.object(manager, "launch_windows_session") as launch:
            self.assertFalse(manager.resume_session(session))
        launch.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
