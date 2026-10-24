"""Windows terminal launcher; keeps CLI errors visible and repairs stale Codex providers."""

import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time


def windows_command(argv, *, keep_open=False, env=None):
    """Expand quoted arguments once in CMD, without CALL's second expansion.

    Passing paths through environment variables preserves literal %, !, &, and
    parentheses. Newlines and quotes cannot occur in Windows paths or CLI IDs.
    """
    child_env = dict(os.environ if env is None else env)
    for key in list(child_env):
        if key.startswith("_CLI_SESSION_ARG_"):
            del child_env[key]
    arguments = []
    for index, value in enumerate(argv):
        value = os.fspath(value)
        if any(c in value for c in ('"', '\r', '\n', '\0')):
            raise ValueError("启动参数包含不支持的引号或换行")
        key = f"_CLI_SESSION_ARG_{index}"
        child_env[key] = value
        arguments.append(f'"%{key}%"')
    comspec = child_env.get("COMSPEC", "cmd.exe")
    switch = "/k" if keep_open else "/c"
    command = f'{subprocess.list2cmdline([comspec])} /d /v:off /s {switch} "{" ".join(arguments)}"'
    return command, child_env


def launch_windows_session(*, argv, cwd, source, rollout_path=None, git_bash=""):
    """Open a dedicated CMD. Configuration checks run there, outside Tk's thread."""
    python = Path(sys.executable)
    if python.name.lower() == "pythonw.exe":
        python = python.with_name("python.exe")
    if not python.is_file():
        raise FileNotFoundError(f"找不到控制台 Python：{python}")
    if source not in ("codex", "claude") or not argv:
        raise ValueError("无效的 CLI 来源或启动参数")
    # Validate before creating a console, including parameters used by its child.
    windows_command(argv)
    request_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="cli_session_",
                                         suffix=".json", delete=False) as request:
            request_path = Path(request.name)
            json.dump({"argv": argv, "cwd": cwd, "source": source,
                       "rollout_path": str(rollout_path) if rollout_path else None,
                       "git_bash": git_bash}, request, ensure_ascii=False)
        command, env = windows_command(
            [python, "-X", "utf8", Path(__file__).resolve(), request_path], keep_open=True)
        for key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR"):
            if env.get(key):
                env[key] = str(Path(env[key]).expanduser().resolve())
        subprocess.Popen(command, env=env, creationflags=subprocess.CREATE_NEW_CONSOLE)
        return request_path
    except (OSError, ValueError):
        if request_path:
            request_path.unlink(missing_ok=True)
        raise


def _stop_process(process):
    """Stop only this launcher's process tree if a configuration query gets stuck."""
    if process.poll() is None:
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=5)
        process.wait(timeout=5)


def read_codex_config(executable, cwd, timeout=10):
    """Ask Codex to resolve its own config layers (also works on Python 3.10).

    Only initialize/config/read/model/list are used; no thread or model turn is
    started. Keep stdin open until the response arrives, then let the server exit.
    """
    command, env = windows_command([executable, "app-server"])
    process = subprocess.Popen(command, env=env, cwd=cwd, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, encoding="utf-8",
                               creationflags=subprocess.CREATE_NO_WINDOW)
    responses = queue.Queue()

    def read_responses():
        try:
            for line in process.stdout:
                try:
                    responses.put(json.loads(line))
                except ValueError:
                    continue
        finally:
            responses.put(None)

    reader = threading.Thread(target=read_responses, daemon=True)
    reader.start()

    def send(message):
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    def request(request_id, method, params):
        send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            try:
                response = responses.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise RuntimeError("读取 Codex 配置超时") from exc
            if response is None:
                raise RuntimeError(f"Codex 配置查询进程提前退出（退出码：{process.poll()}）")
            if not isinstance(response, dict) or response.get("id") != request_id:
                continue
            if "error" in response:
                error = response["error"]
                raise RuntimeError(error.get("message", "Codex 配置查询失败")
                                   if isinstance(error, dict) else "Codex 配置查询失败")
            if not isinstance(response.get("result"), dict):
                raise RuntimeError("Codex 配置查询返回了无效响应")
            return response["result"]

    try:
        request(1, "initialize", {"clientInfo": {"name": "cli-session-manager",
                                               "version": "1.0"}})
        send({"method": "initialized", "params": {}})
        config = request(2, "config/read", {"includeLayers": False, "cwd": cwd})["config"]
        validate_codex_config(config)
        if not config.get("model"):
            models = request(3, "model/list", {})
            config["model"] = next((m["model"] for m in models.get("data", [])
                                    if m.get("isDefault")), None)
        return config
    finally:
        try:
            # Stop the tree while CMD still owns it. Closing stdin first can let
            # CMD exit while a descendant retains stdout, making close() hang.
            _stop_process(process)
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass
            reader.join(timeout=2)
            if not reader.is_alive():
                process.stdout.close()


def _codex_saved_provider(rollout_path):
    """Return the last provider persisted by a Codex rollout."""
    saved_provider = None
    try:
        with Path(rollout_path).open(encoding="utf-8-sig") as rollout:
            for line in rollout:
                try:
                    obj = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("type") == "session_meta":
                    payload = obj.get("payload")
                    if isinstance(payload, dict):
                        provider = payload.get("model_provider")
                        if isinstance(provider, str) and provider:
                            saved_provider = provider
                elif obj.get("type") == "thread_settings_applied":
                    settings = obj.get("thread_settings") or {}
                    provider = settings.get("model_provider_id") if isinstance(settings, dict) else None
                    if isinstance(provider, str) and provider:
                        saved_provider = provider
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"无法读取历史会话的 provider：{exc}") from exc
    return saved_provider


def _toml_key(key):
    if key and all(ch.isascii() and (ch.isalnum() or ch in "_-") for ch in key):
        return key
    return _toml_value(key)


def _toml_value(value):
    # Literal strings survive CMD/npm shims without embedded double quotes.
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        if any(c in value for c in ('"', "'", '\n', '\r', '\0')):
            raise ValueError("提供方配置含引号或换行，请先在 Codex 配置中设置历史 provider 的兼容项")
        return "'" + value + "'"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_toml_key(k)} = {_toml_value(v)}"
                                for k, v in value.items() if v is not None) + "}"
    raise ValueError(f"不支持的提供方配置类型：{type(value).__name__}")


def validate_codex_config(config):
    if not isinstance(config, dict):
        raise ValueError("Codex 返回的配置格式无效")
    providers = config.get("model_providers") or {}
    if not isinstance(providers, dict):
        raise ValueError("model_providers 必须是配置表")
    provider = config.get("model_provider") or "openai"
    if not isinstance(provider, str) or provider not in {"openai", "ollama", "lmstudio"} | set(providers):
        raise ValueError(f"当前 provider {provider!r} 未配置，请检查 CODEX_HOME 和 config.toml")
    return provider


def codex_resume_overrides(rollout_path, config):
    """Supply an invocation-only alias; never edit shared config or history."""
    provider = validate_codex_config(config)
    saved_provider = _codex_saved_provider(rollout_path)
    available = {"openai", "ollama", "lmstudio"} | set(config.get("model_providers") or {})
    if not saved_provider or saved_provider in available:
        return [], ""
    model = config.get("model")
    if not isinstance(model, str) or not model:
        raise ValueError("当前配置缺少恢复会话所需的默认模型")
    arguments = ["-c", f"model_provider={_toml_value(provider)}", "--model", model]
    provider_config = (config.get("model_providers") or {}).get(provider)
    if provider_config:
        # Older clients can resolve the historical provider before overrides.
        alias = dict(provider_config, name=saved_provider)
        arguments.extend(["-c", f"model_providers.{_toml_key(saved_provider)}={_toml_value(alias)}"])
    notice = (f"历史 provider {saved_provider} 已移除；本次使用当前 provider {provider} / {model}。"
              "兼容配置仅对本次进程生效，不修改配置文件或历史会话。")
    return arguments, notice


def run_session(request_path):
    request_path = Path(request_path)
    log_path = request_path.with_suffix(".log")
    exit_code = 1
    with log_path.open("a", encoding="utf-8") as log:
        def report(message):
            print(message, flush=True)
            log.write(message + "\n")
            log.flush()

        try:
            payload = json.loads(request_path.read_text(encoding="utf-8"))
            request_path.unlink()
            cwd = payload["cwd"]
            argv = list(payload["argv"])
            log.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] " +
                      json.dumps({"argv": argv, "cwd": cwd}, ensure_ascii=False) + "\n")
            if not os.path.isdir(cwd):
                raise FileNotFoundError(f"项目目录不存在：{cwd}")
            report(f"CLI：{payload['source']}    目录：{cwd}")
            report(f"启动日志：{log_path}")
            if payload["source"] == "codex":
                report("正在检查 Codex 配置…")
                try:
                    config = read_codex_config(argv[0], cwd)
                    report(f"配置检查完成：provider={validate_codex_config(config)}，model={config.get('model') or 'CLI 默认'}")
                    if payload.get("rollout_path"):
                        overrides, notice = codex_resume_overrides(payload["rollout_path"], config)
                        argv.extend(overrides)
                        if notice:
                            report(notice)
                except (OSError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
                    report(f"配置预检不可用，将交由 CLI 显示实际错误：{exc}")
            child_env = os.environ.copy()
            if payload.get("git_bash"):
                child_env["CLAUDE_CODE_GIT_BASH_PATH"] = payload["git_bash"]
            command, child_env = windows_command(argv, env=child_env)
            report("正在启动 CLI；后续 CLI 输出直接显示在终端，不写入启动日志。")
            # Let the CLI handle Ctrl+C itself without interrupting its Python parent.
            previous_handler = signal.signal(signal.SIGINT, lambda *_: None)
            try:
                exit_code = subprocess.call(command, cwd=cwd, env=child_env)
            finally:
                signal.signal(signal.SIGINT, previous_handler)
        except KeyboardInterrupt:
            exit_code = 130
            report("启动已取消。")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            report(f"启动失败：{exc}")
        hint = "请查看上方错误；" if exit_code else ""
        report(f"会话已结束（退出码：{exit_code}）。{hint}关闭此窗口即可退出。")
    return exit_code


if __name__ == "__main__":
    sys.exit(run_session(sys.argv[1]))
