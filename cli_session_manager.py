#!/usr/bin/env pythonw
# -*- coding: utf-8 -*-
"""
CLI 会话管理器
双击运行，浏览所有 AI CLI（claude / codex）会话，双击恢复。
零依赖：仅使用 Python 标准库 + tkinter。
"""

import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime
from pathlib import Path
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

from session_launcher import launch_windows_session


# ─── 常量 ──────────────────────────────────────────────────────────

HOME = Path.home()
# 数据源：claude（官方 Claude Code）与 codex（OpenAI Codex CLI）
# claude 走 sessions/ + projects/ + history.jsonl，codex 走 sessions/ 下的 rollout
CLAUDE_CONFIG_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude").expanduser().resolve()
CLAUDE_SESSIONS = CLAUDE_CONFIG_DIR / "sessions"
CLAUDE_PROJECTS = CLAUDE_CONFIG_DIR / "projects"
CLAUDE_HISTORY = CLAUDE_CONFIG_DIR / "history.jsonl"

# source 标识
SOURCE_CLAUDE = "claude"
SOURCE_CODEX = "codex"

# 各 source 的数据目录
SOURCE_DIRS = {
    SOURCE_CLAUDE: {
        "sessions": CLAUDE_SESSIONS,
        "projects": CLAUDE_PROJECTS,
        "history": CLAUDE_HISTORY,
    },
}

# codex（OpenAI Codex CLI）结构与 Claude Code 不同：
# 走 sessions/<年>/<月>/<日>/rollout-*.jsonl，无 pid 文件，
# rollout schema 独立，扫描与解析单独适配。
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or HOME / ".codex").expanduser().resolve()
CODEX_SESSIONS_DIR = CODEX_HOME / "sessions"
CODEX_STYLE_DIRS = {
    SOURCE_CODEX: CODEX_SESSIONS_DIR,
}

# 全部可用 source 列表（用于来源筛选器）
ALL_SOURCES = [SOURCE_CLAUDE, SOURCE_CODEX]
UI_FONT = "Microsoft YaHei UI" if os.name == "nt" else "TkDefaultFont"


# 标题截断长度
TITLE_MAX_LEN = 80
# 读取 JSONL 提取标题时最多读多少行
TITLE_SCAN_LINES = 50
# 自动刷新间隔（毫秒）
AUTO_REFRESH_INTERVAL_MS = 10000
# 备注持久化文件
SESSION_NOTES_FILE = HOME / ".claude-internal" / "session_notes.json"
# 分组持久化文件
SESSION_GROUPS_FILE = HOME / ".claude-internal" / "session_groups.json"
# 新建会话快捷路径持久化文件
SESSION_PATHS_FILE = HOME / ".claude-internal" / "session_manager_paths.json"
# UI 设置持久化（勾选项等）
SESSION_SETTINGS_FILE = HOME / ".claude-internal" / "session_manager_settings.json"
# 删除的会话移入此回收站目录（可恢复），而非永久删除
SESSION_TRASH_DIR = HOME / ".claude-internal" / ".session_trash"
# 默认快捷路径（首次启动 / 文件不存在时使用），按需自行填写
DEFAULT_NEW_SESSION_PATHS = ["", ""]

# claude 命令 — 自动检测
def _find_claude_official_cmd() -> str:
    """查找 claude（Claude Code）命令，找不到回退 PATH"""
    resolved = shutil.which("claude.cmd") or shutil.which("claude.exe")
    if resolved:
        return resolved
    candidates = [
        HOME / ".local" / "bin" / "claude.exe",
        HOME / "AppData" / "Roaming" / "npm" / "claude.cmd",
        HOME / ".claude" / "local" / "claude.cmd",
    ]
    try:
        result = subprocess.run(
            ["npm.cmd" if os.name == "nt" else "npm", "prefix", "-g"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        prefix = result.stdout.strip()
        if prefix:
            candidates.insert(0, Path(prefix) / "claude.cmd")
    except Exception:
        pass

    for c in candidates:
        if c.is_file():
            return str(c)
    # 找不到时回退 PATH 中的 claude
    return "claude"

CLAUDE_OFFICIAL_CMD = _find_claude_official_cmd()


def _find_codex_cmd() -> str:
    """查找 codex（OpenAI Codex CLI）命令，找不到回退 PATH"""
    resolved = shutil.which("codex.cmd") or shutil.which("codex.exe")
    if resolved:
        return resolved
    candidates = [
        HOME / "AppData" / "Roaming" / "npm" / "codex.cmd",
    ]
    try:
        result = subprocess.run(
            ["npm.cmd" if os.name == "nt" else "npm", "prefix", "-g"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        prefix = result.stdout.strip()
        if prefix:
            candidates.insert(0, Path(prefix) / "codex.cmd")
    except Exception:
        pass

    for c in candidates:
        if c.is_file():
            return str(c)
    # 找不到时回退 PATH 中的 codex
    return "codex"

CODEX_CMD = _find_codex_cmd()


def _cmd_for_source(source: str) -> str:
    """根据 source 返回对应的 CLI 命令"""
    if source == SOURCE_CLAUDE:
        return _find_claude_official_cmd()
    if source == SOURCE_CODEX:
        return _find_codex_cmd()
    raise ValueError(f"不支持的 CLI 来源：{source}")


def _skip_flag_for_source(source: str) -> str:
    """根据 source 返回对应的跳过授权参数"""
    if source in (SOURCE_CODEX,):
        return " --dangerously-bypass-approvals-and-sandbox"
    return " --dangerously-skip-permissions"


def _find_git_bash_path() -> str:
    """查找 git bash 路径，找不到返回空字符串。"""
    bash_candidates = [
        Path(os.environ.get("CLAUDE_CODE_GIT_BASH_PATH", "")),
        Path(r"C:\Program Files\Git\bin\bash.exe"),
    ]
    ugit_base = Path(os.environ.get("LOCALAPPDATA", "")) / "UGit"
    if ugit_base.is_dir():
        for d in sorted(ugit_base.iterdir(), reverse=True):
            if d.name.startswith("app-"):
                bash_candidates.append(d / "resources" / "app" / "git" / "usr" / "bin" / "bash.exe")
    for bp in bash_candidates:
        if bp and bp.is_file():
            return str(bp)
    return ""


def _find_git_bash_env() -> str:
    """兼容 WSL 的批处理启动路径。"""
    path = _find_git_bash_path()
    return f'set "CLAUDE_CODE_GIT_BASH_PATH={path}"\n' if path else ""


# ─── 原子写 ────────────────────────────────────────────────────────

def _atomic_write_json(path: Path, data) -> bool:
    """原子写 JSON：先写同目录临时文件，再 os.replace 覆盖目标。

    直接 write_text 覆盖是「先截断再写」，中途崩溃/断电会留下半截文件，
    下次 load 解析失败就整份回退成空——备注和分组会被静默清空。
    os.replace 在同一文件系统上是原子替换，目标要么是旧内容要么是新内容。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".tmp{os.getpid()}")
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())   # 元数据落盘，防掉电只留空文件
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError):
        try:
            tmp.unlink(missing_ok=True)
        except (OSError, UnboundLocalError, NameError):
            pass
        return False


# ─── 备注持久化 ──────────────────────────────────────────────────────

def load_notes() -> dict[str, str]:
    """从磁盘加载备注 {sessionId: note}"""
    if not SESSION_NOTES_FILE.is_file():
        return {}
    try:
        return json.loads(SESSION_NOTES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_notes(notes: dict[str, str]) -> bool:
    """持久化备注到磁盘（原子写）"""
    return _atomic_write_json(SESSION_NOTES_FILE, notes)


# ─── 分组持久化 ──────────────────────────────────────────────────────

def load_groups() -> dict:
    """加载分组数据 {groups: [...], assignments: {sid: group_name}}"""
    if not SESSION_GROUPS_FILE.is_file():
        return {"groups": [], "assignments": {}}
    try:
        data = json.loads(SESSION_GROUPS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"groups": [], "assignments": {}}
        data.setdefault("groups", [])
        data.setdefault("assignments", {})
        return data
    except (json.JSONDecodeError, OSError):
        return {"groups": [], "assignments": {}}


def save_groups(data: dict) -> bool:
    """持久化分组数据到磁盘（原子写）"""
    return _atomic_write_json(SESSION_GROUPS_FILE, data)


# ─── 新建会话快捷路径持久化 ──────────────────────────────────────

def load_new_session_paths() -> list[str]:
    """加载新建会话的快捷路径列表。
    文件不存在或损坏时回退到 DEFAULT_NEW_SESSION_PATHS。
    加载后会把「已不存在的目录」清空为空字符串（跨电脑迁移时，
    原电脑硬编码的默认路径在新电脑上通常不存在，清空后新建会话再让用户现选目录）。
    """
    def _clean(paths: list[str]) -> list[str]:
        # 目录不存在的槽位清空，避免残留指向别的电脑的无效路径
        return [p if (p and os.path.isdir(p)) else "" for p in paths]

    if not SESSION_PATHS_FILE.is_file():
        return _clean(list(DEFAULT_NEW_SESSION_PATHS))
    try:
        data = json.loads(SESSION_PATHS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list) and all(isinstance(p, str) for p in data):
            # 至少保留两个槽位（不足补默认值），多余的截断
            paths = list(data)[:len(DEFAULT_NEW_SESSION_PATHS)]
            while len(paths) < len(DEFAULT_NEW_SESSION_PATHS):
                paths.append(DEFAULT_NEW_SESSION_PATHS[len(paths)])
            return _clean(paths)
    except (json.JSONDecodeError, OSError):
        pass
    return _clean(list(DEFAULT_NEW_SESSION_PATHS))


def save_new_session_paths(paths: list[str]) -> bool:
    """持久化新建会话的快捷路径列表（原子写）"""
    return _atomic_write_json(SESSION_PATHS_FILE, paths)


def load_settings() -> dict:
    """加载 UI 设置"""
    if not SESSION_SETTINGS_FILE.is_file():
        return {}
    try:
        data = json.loads(SESSION_SETTINGS_FILE.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError, UnicodeError):
        pass
    return {}


def save_settings(settings: dict) -> bool:
    """持久化 UI 设置（原子写）"""
    return _atomic_write_json(SESSION_SETTINGS_FILE, settings)


def _number(value, default, low, high):
    """Settings are untrusted input; reject booleans and non-finite numbers."""
    try:
        n = float(value) if not isinstance(value, bool) else float("nan")
        return max(low, min(high, n)) if math.isfinite(n) else default
    except (ValueError, TypeError, OverflowError):
        return default


def _work_areas(root):
    """Monitor work areas in Tk's screen coordinates, including negative origins."""
    areas = []
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class MonitorInfo(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                            ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

            callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR,
                                               wintypes.HDC, ctypes.c_void_p, wintypes.LPARAM)
            user32 = ctypes.windll.user32
            user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MonitorInfo)]
            user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.c_void_p, callback_type, wintypes.LPARAM]

            @callback_type
            def collect(handle, _dc, _rect, _data):
                info = MonitorInfo()
                info.cbSize = ctypes.sizeof(info)
                if user32.GetMonitorInfoW(handle, ctypes.byref(info)):
                    r = info.rcWork
                    area = (r.left, r.top, r.right, r.bottom)
                    areas.insert(0, area) if info.dwFlags & 1 else areas.append(area)
                return True

            user32.EnumDisplayMonitors(None, None, collect, 0)
        except (AttributeError, OSError, ValueError):
            pass
    return areas or [(0, 0, root.winfo_screenwidth(), root.winfo_screenheight())]


# ─── 时间显示 ──────────────────────────────────────────────────────

def _fmt_time(ts_ms: int, now: Optional[datetime] = None) -> str:
    """会话时间的人类可读文案。

    列表里最常问的是「这个会话多久之前的」，而 "08-27 23:47" 要心算。
    近 7 天用相对时间（刚刚 / N 分钟前 / 今天 HH:MM / 昨天 HH:MM / N 天前），
    更早的回落到绝对日期；跨年补上年份，避免 "01-05" 分不清哪一年。
    """
    if not ts_ms:
        return "—"
    try:
        dt = datetime.fromtimestamp(ts_ms / 1000)
    except (ValueError, OSError, OverflowError):
        return "—"
    now = now or datetime.now()
    delta = (now - dt).total_seconds()

    if delta < 0:           # 时钟漂移/未来时间戳，别显示「-3 分钟前」
        return dt.strftime("%m-%d %H:%M")
    if delta < 10:
        return "刚刚"
    if delta < 60:
        return f"{int(delta)} 秒前"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:       # 24 小时内
        return f"{int(delta // 3600)} 小时前"

    today = now.date()
    d = dt.date()
    if d == today:
        return f"今天 {dt:%H:%M}"
    days = (today - d).days
    if days == 1:
        return f"昨天 {dt:%H:%M}"
    if days < 7:
        return f"{days} 天前"
    if d.year == today.year:
        return dt.strftime("%m-%d %H:%M")
    return dt.strftime("%Y-%m-%d")


# ─── 数据模型 ──────────────────────────────────────────────────────

class SessionInfo:
    """一条会话记录"""
    __slots__ = (
        "session_id", "pid", "cwd", "started_at", "kind",
        "entrypoint", "title", "alive", "jsonl_path", "note", "group", "source",
        "rollout_paths",
    )

    def __init__(self, *, session_id: str, pid: int, cwd: str,
                 started_at: int, kind: str = "", entrypoint: str = "",
                 jsonl_path: Optional[Path] = None,
                 source: str = SOURCE_CLAUDE):
        self.session_id = session_id
        self.pid = pid
        self.cwd = cwd
        self.started_at = started_at  # 毫秒时间戳
        self.kind = kind
        self.entrypoint = entrypoint
        self.title = ""
        self.alive = False
        self.jsonl_path = jsonl_path
        self.note = ""
        self.group = ""  # 自定义分组名，空则按 project_name 自动分组
        self.source = source  # claude / codex
        self.rollout_paths: list[Path] = []  # codex 会话的全部 rollout 文件（按文件名时间戳升序）

    @property
    def started_dt(self) -> datetime:
        return datetime.fromtimestamp(self.started_at / 1000)

    @property
    def time_text(self) -> str:
        """列表里显示的时间文案（近期用相对时间，见 _fmt_time）"""
        return _fmt_time(self.started_at)

    @property
    def project_name(self) -> str:
        """从 cwd 提取项目名（最后一级目录名）"""
        return Path(self.cwd).name if self.cwd else ""

    @property
    def effective_group(self) -> str:
        """显示用的有效分组：自定义分组优先，其次项目名，最后回落到 '未分组'。"""
        return self.group or self.project_name or "未分组"


# ─── 扫描逻辑 ──────────────────────────────────────────────────────

def _get_session_pid_map(sessions_dir: Path) -> dict[str, int]:
    """从指定 sessions/ 目录获取 sessionId -> pid 映射（用于检测存活状态）"""
    pid_map: dict[str, int] = {}
    if not sessions_dir.is_dir():
        return pid_map
    for fp in sessions_dir.glob("*.json"):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            sid = data.get("sessionId", "")
            pid = data.get("pid", 0)
            if sid and pid:
                pid_map[sid] = pid
        except (json.JSONDecodeError, OSError):
            continue
    return pid_map


def _extract_session_meta_from_jsonl(jsonl_path: Path) -> Optional[dict]:
    """从 JSONL 第一条消息提取 sessionId、cwd、timestamp"""
    try:
        # 先尝试 UTF-8，失败回退到系统默认编码（Windows 下通常是 GBK / CP936）
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # 从任意消息中提取 sessionId 和 cwd
                sid = obj.get("sessionId", "")
                cwd = obj.get("cwd", "")
                ts = obj.get("timestamp", "")
                if sid:
                    # 将 ISO 时间戳转为毫秒
                    started_at = 0
                    if ts:
                        try:
                            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            started_at = int(dt.timestamp() * 1000)
                        except (ValueError, OSError):
                            pass
                    if not started_at:
                        # 用文件修改时间回退
                        started_at = int(jsonl_path.stat().st_mtime * 1000)
                    return {"sessionId": sid, "cwd": cwd, "startedAt": started_at}
    except OSError:
        pass
    return None


def _scan_source(source: str, sessions: dict[str, SessionInfo]) -> None:
    """扫描单个 source（claude），合并到 sessions 字典中。
    主数据源：projects/ 下的 JSONL 文件（不会被覆盖）
    辅助数据：sessions/ 下的 PID 文件（用于存活检测）
    辅助数据：history.jsonl（用于时间戳和 cwd 补充）
    """
    dirs = SOURCE_DIRS[source]
    sessions_dir = dirs["sessions"]
    projects_dir = dirs["projects"]
    history_file = dirs["history"]

    pid_map = _get_session_pid_map(sessions_dir)

    # 从 history.jsonl 提取每个 session 的元信息（最早一条记录的 timestamp 和 project）
    history_meta: dict[str, dict] = {}  # sid -> {timestamp, project}
    if history_file.is_file():
        try:
            with open(history_file, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        sid = obj.get("sessionId", "")
                        ts = obj.get("timestamp", 0)
                        project = obj.get("project", "")
                        if sid and sid not in history_meta:
                            history_meta[sid] = {"timestamp": ts, "project": project}
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass

    if projects_dir.is_dir():
        # 遍历所有项目目录下的 JSONL 文件
        for project_dir in projects_dir.iterdir():
            # 跳过 memory；跳过 "projects"——claude 早期路径 bug 会多算一层
            # projects/，形成 projects/projects/ 嵌套，其下会话对本工具不可见。
            # 直接跳过该异常目录名，避免无谓下探（真实项目目录名恒为 cwd 编码，
            # 形如 E--... / C--...，不会正好叫 "projects"）。
            if not project_dir.is_dir() or project_dir.name in ("memory", "projects"):
                continue

            for item in project_dir.iterdir():
                sid = None
                jsonl_path = None

                if item.suffix == ".jsonl" and item.is_file():
                    sid = item.stem
                    jsonl_path = item
                elif item.is_dir() and not item.name.startswith("."):
                    # 目录形式（含 subagents/）
                    # 必须是 UUID 格式才是会话目录，跳过 memory 等非会话目录
                    name = item.name
                    if len(name) != 36 or name.count("-") != 4:
                        continue
                    sid = name
                    # 找主对话 JSONL（同级可能有 .jsonl 文件）
                    peer_jsonl = project_dir / f"{sid}.jsonl"
                    if peer_jsonl.is_file():
                        jsonl_path = peer_jsonl
                    else:
                        for f in sorted(item.glob("*.jsonl")):
                            if "subagent" not in f.name:
                                jsonl_path = f
                                break

                if not sid or sid in sessions:
                    continue

                # 从多个来源获取元数据
                cwd = ""
                started_at = 0

                # 来源1：history
                if sid in history_meta:
                    meta = history_meta[sid]
                    started_at = meta["timestamp"]
                    cwd = meta["project"]

                # 来源2：JSONL 文件内容（补充缺失的 cwd）
                if (not cwd) and jsonl_path:
                    jsonl_meta = _extract_session_meta_from_jsonl(jsonl_path)
                    if jsonl_meta:
                        if not cwd:
                            cwd = jsonl_meta["cwd"]
                        if not started_at:
                            started_at = jsonl_meta["startedAt"]

                # 来源3：文件修改时间作为最后回退
                if not started_at and jsonl_path:
                    try:
                        started_at = int(jsonl_path.stat().st_mtime * 1000)
                    except OSError:
                        pass

                sessions[sid] = SessionInfo(
                    session_id=sid,
                    pid=pid_map.get(sid, 0),
                    cwd=cwd,
                    started_at=started_at,
                    jsonl_path=jsonl_path,
                    source=source,
                )

    # 补充：sessions/ 中有记录但 projects/ 中无 JSONL 的会话（刚启动还没产生对话）
    for sid, pid in pid_map.items():
        if sid not in sessions:
            cwd = ""
            started_at = 0
            if sid in history_meta:
                started_at = history_meta[sid]["timestamp"]
                cwd = history_meta[sid]["project"]
            # 从 sessions/ 文件中读取 cwd 和 startedAt
            if not cwd or not started_at:
                for fp in sessions_dir.glob("*.json"):
                    try:
                        data = json.loads(fp.read_text(encoding="utf-8"))
                        if data.get("sessionId") == sid:
                            if not cwd:
                                cwd = data.get("cwd", "")
                            if not started_at:
                                started_at = data.get("startedAt", 0)
                            break
                    except (json.JSONDecodeError, OSError):
                        continue
            sessions[sid] = SessionInfo(
                session_id=sid,
                pid=pid,
                cwd=cwd,
                started_at=started_at,
                source=source,
            )


def _codex_sid_from_name(rollout_path: Path) -> str:
    """从 rollout 文件名末尾取 uuid（rollout-<ts>-<uuid>.jsonl）"""
    name = rollout_path.stem
    tail = name[-36:]
    return tail if len(name) >= 36 and tail.count("-") == 4 else name


def _codex_session_id_from_rollout(rollout_path: Path) -> str:
    """读 rollout 首行 session_meta 的 session_id；异常/非 meta 回退文件名 UUID。"""
    try:
        with open(rollout_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    break
                if obj.get("type") == "session_meta":
                    p = obj.get("payload", {})
                    sid = p.get("session_id") or p.get("id") or ""
                    return sid or _codex_sid_from_name(rollout_path)
                break
    except OSError:
        pass
    return _codex_sid_from_name(rollout_path)


def _extract_codex_meta(rollout_path: Path) -> tuple[str, str, int, str]:
    """从 codex rollout 解析 (session_id, cwd, started_at_ms, title)。
    session_meta 行给 id/cwd/timestamp；首个真实 user 消息（新旧格式通用）给标题。
    """
    session_id = ""
    cwd = ""
    started_at = 0
    title = ""
    try:
        with open(rollout_path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= 400:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                typ = obj.get("type")
                payload = obj.get("payload", {})
                if not isinstance(payload, dict):
                    continue
                if typ == "session_meta":
                    session_id = payload.get("session_id") or payload.get("id") or session_id
                    cwd = payload.get("cwd", cwd) or cwd
                    ts = payload.get("timestamp", "")
                    if ts and not started_at:
                        try:
                            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            started_at = int(dt.timestamp() * 1000)
                        except (ValueError, OSError):
                            pass
                if not title:
                    turn = _codex_turn_from_obj(obj)
                    if turn and turn[0] == "user":
                        title = turn[1]
                if session_id and title:
                    break
    except OSError:
        pass
    return session_id, cwd, started_at, title


def _scan_codex(sessions: dict[str, SessionInfo]) -> None:
    """扫描 codex 系会话：<数据目录>/sessions/<年>/<月>/<日>/rollout-*.jsonl。
    无 pid 文件（存活靠 mtime），无 projects/ 结构，rollout schema 独立。
    同一 session_id 的多个 rollout（恢复/续写生成）聚合为一条会话：
    主文件 = 文件名时间戳最早（Codex 会回写旧文件，mtime 不可靠）。
    """
    for source, sessions_dir in CODEX_STYLE_DIRS.items():
        if not sessions_dir.is_dir():
            continue
        agg: dict[str, dict] = {}
        for rollout in sessions_dir.rglob("rollout-*.jsonl"):
            if not rollout.is_file():
                continue
            session_id, cwd, started_at, title = _extract_codex_meta(rollout)
            sid = session_id or _codex_sid_from_name(rollout)
            if not sid or sid in sessions:
                continue
            a = agg.setdefault(sid, {"cwd": "", "started_at": 0, "title": "", "rollouts": []})
            if not a["cwd"] and cwd:
                a["cwd"] = cwd
            if started_at and (a["started_at"] == 0 or started_at < a["started_at"]):
                a["started_at"] = started_at
            if not a["title"] and title:
                a["title"] = title
            a["rollouts"].append(rollout)
        for sid, a in agg.items():
            rollouts = sorted(a["rollouts"], key=lambda p: os.path.basename(str(p)))
            main = rollouts[0]
            started_at = a["started_at"]
            if not started_at:
                try:
                    started_at = int(main.stat().st_mtime * 1000)
                except OSError:
                    started_at = 0
            si = SessionInfo(
                session_id=sid, pid=0, cwd=a["cwd"], started_at=started_at,
                jsonl_path=main, source=source,
            )
            si.rollout_paths = rollouts
            title = a["title"]
            if title:
                si.title = title[:TITLE_MAX_LEN] + ("…" if len(title) > TITLE_MAX_LEN else "")
            sessions[sid] = si


def scan_all_sessions() -> list[SessionInfo]:
    """扫描所有 CLI 会话（claude + codex）"""
    sessions: dict[str, SessionInfo] = {}
    for source in SOURCE_DIRS:
        _scan_source(source, sessions)
    _scan_codex(sessions)
    return sorted(sessions.values(), key=lambda x: x.started_at, reverse=True)


def list_session_ids() -> set[str]:
    """轻量枚举所有会话 ID：只做目录列举，不读取任何文件内容。
    仅用于自动刷新时判断「会话集合是否变化」，避免每 10 秒做一次
    会读文件元数据的全量 scan_all_sessions()（那份结果原先直接被丢弃）。
    """
    ids: set[str] = set()
    for dirs in SOURCE_DIRS.values():
        projects_dir = dirs["projects"]
        if projects_dir.is_dir():
            for project_dir in projects_dir.iterdir():
                if not project_dir.is_dir() or project_dir.name in ("memory", "projects"):
                    continue
                for item in project_dir.iterdir():
                    if item.suffix == ".jsonl" and item.is_file():
                        ids.add(item.stem)
                    elif item.is_dir() and not item.name.startswith(".") \
                            and len(item.name) == 36 and item.name.count("-") == 4:
                        ids.add(item.name)
        sessions_dir = dirs["sessions"]
        if sessions_dir.is_dir():
            for fp in sessions_dir.glob("*.json"):
                try:
                    data = json.loads(fp.read_text(encoding="utf-8"))
                    sid = data.get("sessionId", "")
                    if sid:
                        ids.add(sid)
                except (json.JSONDecodeError, OSError):
                    continue
    # codex 系：读每个 rollout 首行 session_meta 的 session_id（轻量，与扫描集合一致）
    for sessions_dir in CODEX_STYLE_DIRS.values():
        if sessions_dir.is_dir():
            for rollout in sessions_dir.rglob("rollout-*.jsonl"):
                if not rollout.is_file():
                    continue
                try:
                    with open(rollout, "r", encoding="utf-8", errors="replace") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                obj = json.loads(line)
                                if obj.get("type") == "session_meta":
                                    p = obj.get("payload", {})
                                    sid = p.get("session_id") or p.get("id") or ""
                                    if sid:
                                        ids.add(sid)
                                    break
                                break  # 第一条有效 JSON 就定性了
                            except json.JSONDecodeError:
                                break
                except OSError:
                    continue
    return ids


# ─── 标题提取 ──────────────────────────────────────────────────────

def _load_history_titles() -> dict[str, str]:
    """从两份 history.jsonl 读取 display 字段，按 sessionId 取第一条非命令的 display"""
    titles: dict[str, list[str]] = {}
    for dirs in SOURCE_DIRS.values():
        history_file = dirs["history"]
        if not history_file.is_file():
            continue
        try:
            with open(history_file, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        sid = obj.get("sessionId", "")
                        display = obj.get("display", "")
                        if sid and display and not display.startswith("/"):
                            titles.setdefault(sid, []).append(display)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue
    return {sid: msgs[0] for sid, msgs in titles.items() if msgs}


def _extract_title_from_jsonl(jsonl_path: Path) -> str:
    """从对话 JSONL 文件提取标题：找第一条非 meta 的 user 消息"""
    if not jsonl_path.is_file():
        return ""
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= TITLE_SCAN_LINES:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if obj.get("type") not in ("human", "user"):
                    continue
                if obj.get("isMeta"):
                    continue

                msg = obj.get("message", {})
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content", "")

                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = ""
                    for block in content:
                        if isinstance(block, dict):
                            if block.get("type") == "text":
                                text = block.get("text", "")
                                break
                            if block.get("type") == "tool_result":
                                continue
                        elif isinstance(block, str):
                            text = block
                            break
                else:
                    continue

                text = text.strip()
                # 跳过系统命令、XML 标签、内部消息
                if text and not text.startswith("The user") \
                        and not text.startswith("<") \
                        and not text.startswith("/"):
                    return text
    except OSError:
        pass
    return ""


def _load_codex_thread_titles() -> dict[str, str]:
    """从 ~/.codex/session_index.jsonl 读取 Codex 线程命名（id -> thread_name）"""
    titles: dict[str, str] = {}
    idx = CODEX_HOME / "session_index.jsonl"
    if not idx.is_file():
        return titles
    try:
        with open(idx, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = obj.get("id", "")
                name = obj.get("thread_name", "")
                if sid and name and sid not in titles:
                    titles[sid] = name
    except OSError:
        pass
    return titles


def _load_codex_history_titles() -> dict[str, str]:
    """从 ~/.codex/history.jsonl 取每个 session 首条非命令文本作标题"""
    titles: dict[str, list[str]] = {}
    idx = CODEX_HOME / "history.jsonl"
    if not idx.is_file():
        return {}
    try:
        with open(idx, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = obj.get("session_id", "")
                text = obj.get("text", "")
                if sid and text:
                    text = text.strip()
                    if text and not text.startswith("/") and len(text) >= 4:
                        titles.setdefault(sid, []).append(text)
    except OSError:
        pass
    return {sid: msgs[0] for sid, msgs in titles.items() if msgs}


def populate_titles(sessions: list[SessionInfo]):
    """为所有会话填充标题"""
    history_titles = _load_history_titles()
    codex_thread_titles = _load_codex_thread_titles()
    codex_history_titles = _load_codex_history_titles()

    # claude：命令历史 display（现有行为不变）
    for s in sessions:
        if s.session_id in history_titles:
            title = history_titles[s.session_id]
            s.title = title[:TITLE_MAX_LEN] + ("…" if len(title) > TITLE_MAX_LEN else "")

    # codex：session_index thread_name 优先（Codex 线程命名，当前覆盖率低但保留兜底）
    for s in sessions:
        if s.source in CODEX_STYLE_DIRS and s.session_id in codex_thread_titles:
            title = codex_thread_titles[s.session_id]
            s.title = title[:TITLE_MAX_LEN] + ("…" if len(title) > TITLE_MAX_LEN else "")

    # 其余：JSONL 提取（claude 用旧逻辑；codex 由扫描期 _extract_codex_meta 提供）
    for s in sessions:
        if s.title:
            continue
        if s.source in CODEX_STYLE_DIRS:
            continue
        if s.jsonl_path:
            title = _extract_title_from_jsonl(s.jsonl_path)
            if title:
                s.title = title[:TITLE_MAX_LEN] + ("…" if len(title) > TITLE_MAX_LEN else "")

    # codex：history.jsonl 首条非命令文本兜底
    for s in sessions:
        if s.title:
            continue
        if s.source in CODEX_STYLE_DIRS and s.session_id in codex_history_titles:
            title = codex_history_titles[s.session_id]
            s.title = title[:TITLE_MAX_LEN] + ("…" if len(title) > TITLE_MAX_LEN else "")

    for s in sessions:
        if not s.title:
            if not s.jsonl_path:
                s.title = "(新会话)"
            else:
                s.title = "(无标题)"


# ─── 对话预览 ──────────────────────────────────────────────────────

def _content_to_text(content) -> str:
    """把一条 message 的 content（字符串 / block 列表）拍平成纯文本。
    只取 text block，丢弃 tool_use / tool_result / image 等非文本块。
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p).strip()
    return ""


def _parse_turn(line: str):
    """从一行 JSONL 解析出 (role, text)，非对话消息返回 None"""
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    t = obj.get("type")
    if t not in ("human", "user", "assistant"):
        return None
    if obj.get("isMeta"):
        return None
    msg = obj.get("message", {})
    if not isinstance(msg, dict):
        return None
    text = _content_to_text(msg.get("content", ""))
    if not text:
        return None
    # 跳过工具回灌 / 系统注入
    if text.startswith("The user") or text.startswith("<"):
        return None
    role = "assistant" if t == "assistant" else "user"
    return (role, text)


PREVIEW_HEAD_LINES = 120      # 头部最多扫描的行数
PREVIEW_HEAD_TURNS = 6        # 头部展示的对话轮数
PREVIEW_TAIL_TURNS = 6        # 尾部展示的对话轮数
PREVIEW_TAIL_BYTES = 262144   # 大文件只读末尾这么多字节取尾部
PREVIEW_TURN_MAXLEN = 800     # 单轮文本截断长度


def _fmt_preview_turns(turns: list[tuple]) -> str:
    out = []
    for turn in turns:
        role, text = turn[0], turn[1]
        tag = "【用户】" if role == "user" else "【助手】"
        if len(text) > PREVIEW_TURN_MAXLEN:
            text = text[:PREVIEW_TURN_MAXLEN] + " …（截断）"
        out.append(f"{tag}\n{text}")
    return "\n\n".join(out)


def _codex_turn_from_obj(obj):
    """从一行 codex rollout 解析 (role, text, ts_sec)；非对话/系统注入返回 None。
    兼容旧格式（event_msg:user_message/agent_message）与新格式（response_item:message）。
    跳过：role=developer、< 前缀注入（AGENTS/内部上下文/标签消息）、# AGENTS.md 注入。
    """
    payload = obj.get("payload", {})
    if not isinstance(payload, dict):
        return None
    role = None
    text = ""
    t = obj.get("type")
    if t == "event_msg" and payload.get("type") in ("user_message", "agent_message"):
        msg = payload.get("message", "")
        if isinstance(msg, str):
            role = "user" if payload.get("type") == "user_message" else "assistant"
            text = msg.strip()
    elif t == "response_item" and payload.get("type") == "message":
        r = payload.get("role")
        if r in ("user", "assistant"):
            role = "user" if r == "user" else "assistant"
            parts = []
            for block in payload.get("content", []):
                if isinstance(block, dict) and block.get("type") in ("input_text", "output_text"):
                    parts.append(block.get("text", ""))
            text = "\n".join(p for p in parts if p).strip()
    if not role or not text:
        return None
    if text.startswith("<") or text.startswith("# AGENTS.md"):
        return None
    ts_sec = 0
    ts = obj.get("timestamp", "")
    if ts:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            ts_sec = int(dt.timestamp())
        except (ValueError, OSError):
            pass
    return (role, text, ts_sec)


def _codex_turns_from_lines(lines) -> list[tuple]:
    """把若干 JSONL 行解析为 (role, text, ts_sec) 轮次；相邻重复（双写/回放）去重。"""
    turns: list[tuple] = []
    last_key = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        turn = _codex_turn_from_obj(obj)
        if not turn:
            continue
        key = (turn[0], turn[1], turn[2])
        if key == last_key:
            continue
        last_key = key
        turns.append(turn)
    return turns


def _codex_tail_turns(jsonl_path: Path, tail_bytes: int = PREVIEW_TAIL_BYTES) -> list[tuple]:
    """从文件尾部有界读取轮次：seek 到最后 tail_bytes 字节，丢弃被截断的首行。"""
    try:
        size = jsonl_path.stat().st_size
        if size <= tail_bytes:
            with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
                return _codex_turns_from_lines(f)
        with open(jsonl_path, "rb") as f:
            f.seek(size - tail_bytes)
            chunk = f.read().decode("utf-8", errors="replace")
        return _codex_turns_from_lines(chunk.split("\n")[1:])
    except OSError:
        return []


def _extract_codex_preview(jsonl_path: Path, rollout_paths: Optional[list[Path]] = None) -> str:
    """codex rollout 预览：头 6 轮（主文件前 120 行）+ 真尾部 6 轮（文件尾 256KB）。
    多 rollout 会话若有 mtime 更新的副文件，并读其尾部取时间戳更晚的一块。
    """
    head_lines: list[str] = []
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= PREVIEW_HEAD_LINES:
                    break
                head_lines.append(line)
    except OSError as e:
        return f"（读取失败：{e}）"
    head = _codex_turns_from_lines(head_lines)[:PREVIEW_HEAD_TURNS]
    tail = _codex_tail_turns(jsonl_path)[-PREVIEW_TAIL_TURNS:]
    if not tail:
        # 超大单行可能让 256KB 尾部解析不到轮次，扩大到 1MB 再试
        tail = _codex_tail_turns(jsonl_path, tail_bytes=1048576)[-PREVIEW_TAIL_TURNS:]
    if rollout_paths:
        try:
            main_mtime = jsonl_path.stat().st_mtime
        except OSError:
            main_mtime = 0
        for extra in rollout_paths:
            if extra == jsonl_path:
                continue
            try:
                if extra.stat().st_mtime > main_mtime:
                    extra_tail = _codex_tail_turns(extra)[-PREVIEW_TAIL_TURNS:]
                    if extra_tail and (not tail or extra_tail[-1][2] > tail[-1][2]):
                        tail = extra_tail
            except OSError:
                continue
    if not head and not tail:
        return "（无可展示的对话内容）"
    body = _fmt_preview_turns(head)
    if tail:
        head_keys = {(h[0], h[1], h[2]) for h in head}
        tail_only = [t for t in tail if (t[0], t[1], t[2]) not in head_keys]
        if tail_only:
            body += "\n\n──────── 中间省略 ────────\n\n" + _fmt_preview_turns(tail_only)
    return body


def extract_preview(jsonl_path: Optional[Path], rollout_paths: Optional[list[Path]] = None) -> str:
    """生成会话预览：头部若干轮 + （大文件时）尾部若干轮。
    有界读取——绝不整体加载 19MB 级别的对话文件。
    codex rollout（首行 session_meta）走专属解析。
    """
    if not jsonl_path or not jsonl_path.is_file():
        return "（无对话记录，无法预览）"

    # 探测是否为 codex rollout（只读首个非空行）
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    if json.loads(line).get("type") == "session_meta":
                        return _extract_codex_preview(jsonl_path, rollout_paths)
                except json.JSONDecodeError:
                    pass
                break
    except OSError:
        pass

    head: list[tuple[str, str]] = []
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= PREVIEW_HEAD_LINES or len(head) >= PREVIEW_HEAD_TURNS:
                    break
                turn = _parse_turn(line)
                if turn:
                    head.append(turn)
    except OSError as e:
        return f"（读取失败：{e}）"

    tail: list[tuple[str, str]] = []
    truncated = False
    try:
        size = jsonl_path.stat().st_size
        if size <= PREVIEW_TAIL_BYTES:
            with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
                turns = [turn for line in f if (turn := _parse_turn(line))]
            if not turns:
                return "（无可展示的对话内容）"
            if len(turns) <= PREVIEW_HEAD_TURNS + PREVIEW_TAIL_TURNS:
                return _fmt_preview_turns(turns)
            return (_fmt_preview_turns(turns[:PREVIEW_HEAD_TURNS]) +
                    "\n\n──────── 中间省略 ────────\n\n" +
                    _fmt_preview_turns(turns[-PREVIEW_TAIL_TURNS:]))
        if size > PREVIEW_TAIL_BYTES:
            truncated = True
            with open(jsonl_path, "rb") as f:
                f.seek(size - PREVIEW_TAIL_BYTES)
                chunk = f.read().decode("utf-8", errors="replace")
            # 丢弃可能被截断的首行
            for line in chunk.split("\n")[1:]:
                turn = _parse_turn(line)
                if turn:
                    tail.append(turn)
            tail = tail[-PREVIEW_TAIL_TURNS:]
    except OSError:
        pass

    def fmt(turns: list[tuple[str, str]]) -> str:
        out = []
        for role, text in turns:
            tag = "【用户】" if role == "user" else "【助手】"
            if len(text) > PREVIEW_TURN_MAXLEN:
                text = text[:PREVIEW_TURN_MAXLEN] + " …（截断）"
            out.append(f"{tag}\n{text}")
        return "\n\n".join(out)

    if not head and not tail:
        return "（无可展示的对话内容）"

    body = fmt(head)
    if truncated and tail:
        # 去掉尾部与头部重叠的轮次
        head_set = {t for t in head}
        tail_only = [t for t in tail if t not in head_set]
        if tail_only:
            body += "\n\n──────── 中间省略 ────────\n\n" + fmt(tail_only)
    return body


# ─── 进程状态检测 ──────────────────────────────────────────────────

def check_alive(sessions: list[SessionInfo]):
    """批量检查会话进程是否存活。
    方法1：sessions/ 中登记的 PID 在 tasklist 中存活（claude 启动时都会写
           sessions/<pid>.json，这是唯一能把进程和 sessionId 对上的途径）
    方法2：JSONL 文件最近 60 秒内有写入（兜底，覆盖没有 PID 文件的场景）

    注：早期版本还用过 PowerShell Get-CimInstance 抓命令行匹配 --session-id，
    但 claude 的实际命令行里从不带 --session-id
    （新建是裸命令、恢复是 --resume <sid>），该正则恒不命中，且每次 CIM 调用
    ~0.7s + powershell 冷启动。已移除，存活检测完全由下面两法承担，零损失。
    """
    import time

    # 获取两个 source 的 sessions/ PID 映射，合并查存活
    pid_map: dict[str, int] = {}
    for dirs in SOURCE_DIRS.values():
        pid_map.update(_get_session_pid_map(dirs["sessions"]))

    # 一次性获取所有存活 PID
    alive_pids: set[int] = set()
    pids_to_check = {pid for pid in pid_map.values() if pid > 0}
    if pids_to_check:
        try:
            result = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            for line in result.stdout.splitlines():
                parts = line.split(",")
                if len(parts) >= 2:
                    try:
                        pid = int(parts[1].strip('"'))
                        if pid in pids_to_check:
                            alive_pids.add(pid)
                    except ValueError:
                        continue
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

    now = time.time()

    for s in sessions:
        if s.session_id in pid_map and pid_map[s.session_id] in alive_pids:
            # 方法1：sessions/ 中 PID 存活
            s.alive = True
            s.pid = pid_map[s.session_id]
        elif s.jsonl_path:
            # 方法2：JSONL 最近 60 秒内被写入（codex 多 rollout 取全部文件最新 mtime）
            try:
                paths = s.rollout_paths or [s.jsonl_path]
                mtime = max(p.stat().st_mtime for p in paths)
                s.alive = (now - mtime) < 60
            except OSError:
                s.alive = False
        else:
            s.alive = False


# ─── 会话恢复 ──────────────────────────────────────────────────────

def resume_session(session: SessionInfo, skip_permissions: bool = False):
    """在新终端中恢复会话"""
    cwd = session.cwd
    # cwd 为空或已不存在（如迁移到别的电脑后旧盘符失效）时，让用户现选一个目录，
    # 而非在 bat 里 cd 失败后卡住。用户取消则中止恢复。
    if not cwd or not os.path.isdir(cwd):
        from tkinter import filedialog
        old = cwd or "（无记录）"
        messagebox.showinfo(
            "原目录不可用",
            f"该会话记录的工作目录不存在（可能已迁移或删除）：\n{old}\n\n请选择一个目录用于恢复本会话。")
        picked = filedialog.askdirectory(title="选择恢复会话的工作目录")
        if not picked:
            return  # 用户取消
        cwd = picked.replace("/", "\\")
    sid = session.session_id
    cmd = _cmd_for_source(session.source)
    # codex 系：恢复用子命令 `resume <id>`（无 -- 前缀）；claude 系用 `--resume <id>`
    resume_flag = "resume" if session.source in CODEX_STYLE_DIRS else "--resume"
    # 「跳过授权」按来源映射到各自的 yolo/跳过标志
    extra_args = _skip_flag_for_source(session.source) if skip_permissions else ""

    if sys.platform == "win32" or os.name == "nt":
        # Claude's ID lookup is scoped to the current project. Resuming the
        # selected transcript also works after its original cwd has moved.
        target = str(session.jsonl_path.resolve()) if session.source == SOURCE_CLAUDE \
            and session.jsonl_path and session.jsonl_path.is_file() else sid
        argv = [cmd, resume_flag, target]
        if extra_args:
            argv.append(extra_args.strip())
        try:
            launch_windows_session(
                argv=argv, cwd=cwd, source=session.source, rollout_path=session.jsonl_path,
                git_bash=_find_git_bash_path() if session.source == SOURCE_CLAUDE else "")
            return True
        except (OSError, ValueError) as e:
            messagebox.showerror("恢复失败", f"无法启动会话终端：\n{e}")
            return False

    # WSL 环境检测
    is_wsl = False
    if os.path.exists("/proc/version"):
        try:
            with open("/proc/version", "r") as f:
                is_wsl = "microsoft" in f.read().lower()
        except OSError:
            pass

    if is_wsl:
        # 写临时 bat 避免嵌套引号问题
        import tempfile
        log_path = os.path.join(tempfile.gettempdir(), f"claude_resume_{sid[:8]}.log")

        git_bash_env = _find_git_bash_env()

        bat_content = (
            f'@echo off\n'
            f'chcp 65001 >nul\n'
            f'{git_bash_env}'
            f'echo [%date% %time%] resume {sid} > "{log_path}"\n'
            f'echo cwd={cwd} >> "{log_path}"\n'
            f'echo cmd="{cmd}" >> "{log_path}"\n'
            f'cd /d "{cwd}"\n'
            f'if errorlevel 1 (\n'
            f'  echo [ERROR] cd failed >> "{log_path}"\n'
            f'  echo cd /d "{cwd}" 失败\n'
            f'  pause\n'
            f'  exit /b 1\n'
            f')\n'
            f'echo cd ok >> "{log_path}"\n'
            f'echo.\n'
            f'echo 正在恢复会话 {sid[:8]}...\n'
            f'echo.\n'
            f'call "{cmd}" {resume_flag} {sid}{extra_args}\n'
            f'set "cli_exit=%errorlevel%"\n'
            f'echo [exit=%cli_exit%] >> "{log_path}"\n'
            f'echo.\n'
            f'echo 会话已结束（退出码: %cli_exit%）\n'
            f'echo 日志: {log_path}\n'
            f'pause\n'
        )
        bat_path = os.path.join(tempfile.gettempdir(), f"claude_resume_{sid[:8]}.bat")
        try:
            with open(bat_path, "w", encoding="utf-8") as f:
                f.write(bat_content)
        except OSError as e:
            messagebox.showerror("恢复失败", f"无法写入临时启动脚本（TEMP 目录不可写或磁盘已满）：\n{e}")
            return

        try:
            # WSL -> 用 cmd.exe 启动
            win_bat = bat_path.replace("/tmp", os.popen("wslpath -w /tmp").read().strip()) \
                if bat_path.startswith("/tmp") else bat_path
            subprocess.Popen(f'cmd.exe /c start "" "{win_bat}"', shell=True)
        except OSError as e:
            messagebox.showerror("恢复失败", f"无法启动会话终端：\n{e}")
    else:
        for terminal in ["gnome-terminal", "xterm", "konsole"]:
            try:
                subprocess.Popen(
                    [terminal, "--", "bash", "-c",
                     f"cd '{cwd}' && {cmd} {resume_flag} {sid}{extra_args}; exec bash"],
                )
                return
            except FileNotFoundError:
                continue
        messagebox.showerror("错误", "找不到终端模拟器，请手动执行：\n"
                             f"cd {cwd} && {cmd} {resume_flag} {sid}{extra_args}")


# ─── GUI ───────────────────────────────────────────────────────────

class _Tooltip:
    """轻量悬浮提示（tkinter 无内置 tooltip）。

    用于两处：危险开关的后果说明，以及表格里被列宽截断的单元格全文
    （标题列常被截断，光看前半段分不清是哪个会话）。
    """

    def __init__(self, widget, text: str = "", delay_ms: int = 500, wraplength: int = 420):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self.wraplength = wraplength
        self._after_id = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def set_text(self, text: str):
        self.text = text

    def _schedule(self, _event=None):
        self.hide()
        if not self.text:
            return
        try:
            self._after_id = self.widget.after(self.delay_ms, self._show)
        except tk.TclError:
            self._after_id = None

    def _show(self):
        if self._tip is not None or not self.text:
            return
        try:
            x = self.widget.winfo_pointerx() + 12
            y = self.widget.winfo_pointery() + 18
            tw = tk.Toplevel(self.widget)
            tw.wm_overrideredirect(True)       # 无边框
            tw.attributes("-topmost", True)
            tk.Label(tw, text=self.text, justify=tk.LEFT,
                     background="#ffffe1", relief=tk.SOLID, borderwidth=1,
                     font=(UI_FONT, 9), wraplength=self.wraplength,
                     padx=6, pady=4).pack()
            tw.wm_geometry(f"+{x}+{y}")
            self._tip = tw
        except tk.TclError:
            self._tip = None

    def hide(self, _event=None):
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except (tk.TclError, ValueError):
                pass
            self._after_id = None
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


class SessionManagerApp:
    """主窗口"""

    _LAYOUT_VERSION = 2
    _DEFAULT_RATIO = 0.68
    _COLUMNS = ("title", "status", "time", "source", "project", "group", "note")
    _DEFAULT_WIDTHS = {"title": 330, "status": 100, "time": 120, "source": 76,
                       "project": 130, "group": 120, "note": 150}
    _MIN_WIDTHS = {"title": 220, "status": 90, "time": 110, "source": 68,
                   "project": 90, "group": 80, "note": 90}

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("CLI 会话管理器")
        self._closed = False
        self._layout_ready = False
        self._save_after_id = None
        self._layout_after_id = None
        self._preview_after_id = None
        self._preview_generation = 0
        self._preview_future = None
        self._preview_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="preview")
        self._ui_queue = queue.SimpleQueue()
        self._preview_body = ""
        self._resizing_column = None
        self._dragging_sash = False
        self._scale = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        self._areas = _work_areas(root)
        self.ui_settings = load_settings()
        self._normal_geometry = self._safe_geometry(self.ui_settings.get("geometry"))
        self.root.geometry(self._normal_geometry)
        area = self._areas[0]
        self.root.minsize(min(self._px(900), area[2] - area[0] - 24),
                          min(self._px(540), area[3] - area[1] - 64))
        self._preferred_ratio = _number(self.ui_settings.get("sash_ratio"),
                                        self._DEFAULT_RATIO, 0.2, 0.9)
        self._view_restored = False
        self._density = self.ui_settings.get("density", "comfortable")
        if self._density not in ("comfortable", "compact"):
            self._density = "comfortable"

        self.sessions: list[SessionInfo] = []
        self.filtered_sessions: list[SessionInfo] = []
        self._auto_refresh_id: Optional[str] = None
        # 排序状态：默认按时间倒序
        self._sort_col: str = self.ui_settings.get("sort_col", "time")
        if self._sort_col not in self._COLUMNS:
            self._sort_col = "time"
        self._sort_reverse = self.ui_settings.get("sort_reverse", True) is True

        # ── 虚拟化渲染状态 ──
        # filtered_sessions 是完整数据模型；表格只渲染其中一个可视窗口。
        self._window_start: int = 0            # 窗口顶部在 filtered_sessions 中的下标
        self._rendered_sids: list[str] = []    # 当前已渲染到表格里的 session_id（有序）
        self._selected_sids: set[str] = set()  # 模型级选中（跨窗口保留，是选中的唯一真相）
        self._rendering: bool = False          # 渲染期间抑制 TreeviewSelect 回调（保留兜底）
        self._counts_text: str = ""            # 状态栏计数部分（范围提示由渲染时拼接）
        self._ROWH = self._px(32 if self._density == "comfortable" else 25)
        self._sel_anchor: Optional[str] = None  # Shift 范围选择的锚点 session_id
        self._previewed_sid: Optional[str] = None  # 预览面板当前显示的会话（用于删除/刷新后清空残留）
        self._scanning: bool = False           # 后台扫描进行中（避免重复触发叠加）
        self._refresh_pending: bool = False    # 扫描期间又来了刷新请求，收尾后补跑
        self._refresh_pending_preserve: bool = False
        self._search_after_id: Optional[str] = None  # 搜索防抖的 after 句柄
        self._suppress_filter: bool = False    # 批量改筛选变量时抑制中间过滤
        # 分组下拉：显示串（带计数）→ 真实分组名；以及当前选中的真实分组名
        self._group_display_to_real: dict[str, str] = {"全部": "全部"}
        self._selected_group_real = self.ui_settings.get("group_filter", "全部")
        if not isinstance(self._selected_group_real, str):
            self._selected_group_real = "全部"

        self._build_ui()
        self._update_heading_arrows()
        self._poll_id = self.root.after(40, self._drain_queue)
        self._refresh()
        self._schedule_auto_refresh()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Configure>", self._on_root_configure, add="+")
        if self.ui_settings.get("window_state") == "zoomed":
            try:
                self.root.state("zoomed")
            except tk.TclError:
                pass
        self._layout_after_id = self.root.after_idle(self._restore_sash)

    def _px(self, value):
        return round(value * self._scale)

    def _safe_geometry(self, geom) -> str:
        """Restore a usable window within a connected monitor's work area."""
        mm = re.fullmatch(r"(\d{1,6})x(\d{1,6})(?:([+-]-?\d{1,7})([+-]-?\d{1,7}))?",
                          geom.strip()) if isinstance(geom, str) else None
        area = self._areas[0]
        x = y = None
        if mm and mm.group(3):
            x, y = (int(t[1:]) if t.startswith("+") else int(t) for t in mm.group(3, 4))
            w, h = int(mm[1]), int(mm[2])
            def overlap(a):
                return max(0, min(x + w, a[2]) - max(x, a[0])) * max(0, min(y + h, a[3]) - max(y, a[1]))
            area = max(self._areas, key=overlap)
        aw, ah = area[2] - area[0], area[3] - area[1]
        default_w = min(self._px(1560), int(aw * 0.92))
        default_h = min(self._px(920), int(ah * 0.86))
        w, h = (int(mm[1]), int(mm[2])) if mm else (default_w, default_h)
        # Upgrade the old small default once; later deliberate resizing is respected.
        if self.ui_settings.get("layout_version") != self._LAYOUT_VERSION and w <= 1100 and h <= 600:
            w, h = default_w, default_h
        w = min(max(self._px(900), w), aw - 24)
        h = min(max(self._px(540), h), ah - 64)
        x = area[0] + (aw - w) // 2 if x is None else x
        y = area[1] + (ah - h) // 2 if y is None else y
        x = max(area[0] + 8, min(x, area[2] - w - 16))
        y = max(area[1] + 8, min(y, area[3] - h - 48))
        return f"{w}x{h}+{x}+{y}"

    def _build_ui(self):
        import tkinter.font as tkfont
        style = ttk.Style(self.root)
        style.theme_use("clam" if "clam" in style.theme_names() else style.theme_use())
        self.root.configure(background="#f4f5f7")
        tkfont.nametofont("TkDefaultFont").configure(family=UI_FONT, size=10)
        style.configure(".", font=(UI_FONT, 10), background="#f4f5f7", foreground="#25292f")
        style.configure("TButton", padding=(8, 5), relief="flat", borderwidth=1,
                        bordercolor="#d3d9de", lightcolor="#f4f5f7", darkcolor="#f4f5f7")
        style.map("TButton", background=[("active", "#e6edef")])
        style.configure("Icon.TButton", font=("Segoe MDL2 Assets", 11), width=3, padding=(7, 7))
        style.configure("TEntry", padding=(6, 5), fieldbackground="white", bordercolor="#c8d0d6", lightcolor="white")
        style.configure("TCombobox", padding=(6, 4), bordercolor="#c8d0d6", arrowsize=self._px(12))
        style.map("TCombobox", fieldbackground=[("readonly", "white")], selectbackground=[("readonly", "white")],
                  selectforeground=[("readonly", "#25292f")])
        style.configure("Primary.TButton", background="#13786b", foreground="white")
        style.map("Primary.TButton", background=[("active", "#0d6258"), ("disabled", "#e1e5e7")],
                  foreground=[("disabled", "#747b80")])
        style.configure("Treeview", rowheight=self._ROWH, font=(UI_FONT, 10),
                        background="#ffffff", fieldbackground="#ffffff", borderwidth=0)
        style.configure("Treeview.Heading", font=(UI_FONT, 10, "bold"), padding=(8, 7),
                        background="#e9edef", relief="flat")
        style.map("Treeview", background=[("selected", "#ceeae5")], foreground=[("selected", "#133e38")])
        style.configure("TPanedwindow", background="#d5dbdf")
        style.configure("Sash", sashthickness=self._px(8))
        style.configure("Danger.TCheckbutton", foreground="#a42c37")
        style.configure("Empty.TFrame", background="#ffffff")
        self._text_font = tkfont.Font(root=self.root, family=UI_FONT, size=10)
        self._icons = {}
        assets = Path(__file__).parent / "assets"
        try:
            atlas = tk.PhotoImage(master=self.root, file=str(assets / "icons.png"))
            positions = json.loads((assets / "icons.json").read_text(encoding="utf-8"))
            for name, variants in positions.items():
                size = min(variants, key=lambda n: abs(int(n) - self._px(16)))
                x, y, edge = variants[size]
                icon = tk.PhotoImage(master=self.root, width=edge, height=edge)
                self.root.tk.call(str(icon), "copy", str(atlas), "-from", x, y, x + edge, y + edge)
                self._icons[name] = icon
        except (OSError, tk.TclError, ValueError):
            pass

        toolbar = ttk.Frame(self.root, padding=(12, 10, 12, 5))
        toolbar.pack(fill=tk.X)
        toolbar.columnconfigure(2, weight=1)
        ttk.Label(toolbar, text="会话", font=(UI_FONT, 14, "bold")).grid(row=0, column=0, padx=(0, 18))
        ttk.Label(toolbar, text="搜索").grid(row=0, column=1, padx=(0, 6))
        search = self.ui_settings.get("search", "")
        self.search_var = tk.StringVar(value=search if isinstance(search, str) else "")
        self.search_entry = ttk.Entry(toolbar, textvariable=self.search_var, width=18)
        self.search_entry.grid(row=0, column=2, sticky="ew")
        self.search_var.trace_add("write", lambda *_: self._on_search_changed())
        _Tooltip(self.search_entry, "搜索标题、项目、路径、备注、分组和 Session ID\nCtrl+F")
        self._icon_button(toolbar, "\ue711", "清空搜索", lambda: self.search_var.set("")).grid(row=0, column=3, padx=(2, 10))
        ttk.Button(toolbar, text="新建会话", style="Primary.TButton", command=self._show_new_session).grid(row=0, column=4)
        self.refresh_btn = self._icon_button(toolbar, "\ue72c", "刷新 (F5)", self._refresh)
        self.refresh_btn.grid(row=0, column=5, padx=(8, 2))
        self.preview_visible_var = tk.BooleanVar(value=self.ui_settings.get("preview_visible", True) is not False)
        self.preview_btn = self._icon_button(toolbar, "\ue8a1", "显示或隐藏预览 (Ctrl+P)", self._toggle_preview)
        self.preview_btn.grid(row=0, column=6, padx=2)
        self.layout_btn = self._icon_button(toolbar, "\ue713", "视图设置", self._show_layout_menu)
        self.layout_btn.grid(row=0, column=7, padx=(2, 0))

        filters = ttk.Frame(self.root, padding=(12, 2, 12, 10))
        filters.pack(fill=tk.X)
        filters.columnconfigure(3, weight=1)
        ttk.Label(filters, text="来源").grid(row=0, column=0)
        src = self.ui_settings.get("source_filter", "全部")
        self.source_filter_var = tk.StringVar(value=src if src in ["全部"] + ALL_SOURCES else "全部")
        self.source_combo = ttk.Combobox(filters, textvariable=self.source_filter_var,
                                         state="readonly", width=9, values=["全部"] + ALL_SOURCES)
        self.source_combo.grid(row=0, column=1, padx=(6, 18))
        self.source_combo.bind("<<ComboboxSelected>>", lambda _: self._apply_filter())
        ttk.Label(filters, text="分组").grid(row=0, column=2, padx=(0, 6))
        self.group_filter_var = tk.StringVar(value=self._selected_group_real)
        self.group_combo = ttk.Combobox(filters, textvariable=self.group_filter_var,
                                        state="readonly", width=16, values=[self._selected_group_real])
        self.group_combo.grid(row=0, column=3, sticky="ew", padx=(0, 16))
        self.group_combo.bind("<<ComboboxSelected>>", lambda _: self._on_group_selected())
        self.notes_only_var = tk.BooleanVar(value=self.ui_settings.get("notes_only") is True)
        self.running_only_var = tk.BooleanVar(value=self.ui_settings.get("running_only") is True)
        ttk.Checkbutton(filters, text="有备注", variable=self.notes_only_var,
                        command=self._apply_filter).grid(row=0, column=4, padx=(0, 12))
        ttk.Checkbutton(filters, text="运行中", variable=self.running_only_var,
                        command=self._apply_filter).grid(row=0, column=5, padx=(0, 8))
        self.clear_filters_btn = self._icon_button(filters, "\ue894", "清除全部筛选", self._clear_filters)
        self.clear_filters_btn.grid(row=0, column=6)

        saved_paths = load_new_session_paths()
        self.new_session_path_var1 = tk.StringVar(value=saved_paths[0])
        self.new_session_path_var2 = tk.StringVar(value=saved_paths[1])
        src = self.ui_settings.get("new_source", self.source_filter_var.get())
        self.new_source_var = tk.StringVar(value=src if src in ALL_SOURCES else SOURCE_CODEX)
        self.skip_perms_var = tk.BooleanVar(value=self.ui_settings.get("skip_permissions") is True)

        bottom = ttk.Frame(self.root, padding=(12, 5))
        bottom.pack(fill=tk.X, side=tk.BOTTOM)
        bottom.columnconfigure(0, weight=1)
        bottom.columnconfigure(1, weight=1)
        self.status_label = ttk.Label(bottom, text="扫描中…", anchor="w")
        self.status_label.grid(row=0, column=0, sticky="ew")
        self.bottom_label = ttk.Label(bottom, text="", anchor="e", foreground="#646c75")
        self.bottom_label.grid(row=0, column=1, sticky="ew", padx=(8, 0))

        self.paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        self.paned.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 4))
        self.paned.bind("<Configure>", self._on_paned_configure)
        self.paned.bind("<ButtonPress-1>", self._on_sash_press)
        self.paned.bind("<ButtonRelease-1>", self._on_sash_release)
        self.paned.bind("<Double-Button-1>", self._reset_split)
        self.left = ttk.Frame(self.paned)
        self.left.bind("<Configure>", self._on_paned_configure)
        self.left.columnconfigure(0, weight=1)
        self.left.rowconfigure(1, weight=1)
        actions = ttk.Frame(self.left, padding=(0, 0, 6, 6))
        actions.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.resume_btn = ttk.Button(actions, text="恢复会话", command=self._resume_selected)
        self.resume_btn.pack(side=tk.LEFT)
        _Tooltip(self.resume_btn, "恢复选中的会话 (Enter)")
        self.note_btn = self._icon_button(actions, "\ue70f", "编辑备注 (Ctrl+D)", self._edit_note)
        self.note_btn.pack(side=tk.LEFT, padx=(6, 2))
        self.group_btn = self._icon_button(actions, "\ue8f1", "修改分组", self._change_group)
        self.group_btn.pack(side=tk.LEFT, padx=2)
        self.folder_btn = self._icon_button(actions, "\ue8b7", "打开项目目录", self._open_project_dir)
        self.folder_btn.pack(side=tk.LEFT, padx=2)
        self.copy_btn = self._icon_button(actions, "\ue8c8", "复制 Session ID (Ctrl+C)", self._copy_session_id)
        self.copy_btn.pack(side=tk.LEFT, padx=2)
        self.delete_btn = self._icon_button(actions, "\ue74d", "移入回收站 (Delete)", self._delete_selected)
        self.delete_btn.pack(side=tk.LEFT, padx=2)
        self.selection_label = ttk.Label(actions, text="", foreground="#646c75")
        self.selection_label.pack(side=tk.RIGHT)

        self._col_titles = {"title": "会话标题", "status": "状态", "time": "创建时间", "source": "来源",
                            "project": "项目", "group": "分组", "note": "备注"}
        self.tree = ttk.Treeview(self.left, columns=self._COLUMNS, show="headings", selectmode="extended")
        visible = self.ui_settings.get("visible_columns", [c for c in self._COLUMNS if c != "group"])
        if not isinstance(visible, list):
            visible = list(self._COLUMNS)
        self._column_vars = {c: tk.BooleanVar(value=c == "title" or c in visible) for c in self._COLUMNS}
        self.tree.configure(displaycolumns=self._display_columns())
        widths = self.ui_settings.get("col_widths", {})
        widths = widths if isinstance(widths, dict) else {}
        saved_scale = _number(self.ui_settings.get("ui_scale"), 1, 0.5, 4)
        self._column_widths = {}
        for col in self._COLUMNS:
            width = _number(widths.get(col), self._px(self._DEFAULT_WIDTHS[col]),
                            self._px(self._MIN_WIDTHS[col]), self._px(1200))
            if col in widths and self.ui_settings.get("layout_version") == self._LAYOUT_VERSION:
                width = _number(width / saved_scale * self._scale, width,
                                self._px(self._MIN_WIDTHS[col]), self._px(1200))
            self._column_widths[col] = round(width)
            self.tree.heading(col, text=self._col_titles[col], anchor="w", command=lambda c=col: self._sort_by(c))
            self.tree.column(col, width=round(width), minwidth=self._px(self._MIN_WIDTHS[col]),
                             stretch=False, anchor="w")
        self.tree.tag_configure("odd", background="#f7f8fa")
        self.tree.tag_configure("even", background="#ffffff")
        self.tree.tag_configure("alive", background="#edf7ee", foreground="#24582f")
        self.vscroll = ttk.Scrollbar(self.left, orient=tk.VERTICAL, command=self._yscroll)
        self.hscroll = ttk.Scrollbar(self.left, orient=tk.HORIZONTAL, command=self._xscroll)
        self.tree.configure(xscrollcommand=self.hscroll.set)
        self.tree.grid(row=1, column=0, sticky="nsew")
        self.vscroll.grid(row=1, column=1, sticky="ns")
        self.hscroll.grid(row=2, column=0, sticky="ew")
        self.empty_state = ttk.Frame(self.tree, style="Empty.TFrame")
        self.empty_hint = ttk.Label(self.empty_state, text="", foreground="#646c75", justify=tk.CENTER, background="#ffffff")
        self.empty_hint.pack()
        self.empty_action = ttk.Button(self.empty_state, text="清除筛选", command=self._clear_filters)
        self._cell_tip = _Tooltip(self.tree, "", delay_ms=450)
        self._tip_cell = ()
        self.tree.bind("<Motion>", self._on_tree_motion, add="+")
        self.tree.bind("<Leave>", lambda _: self._cell_tip.hide(), add="+")
        self.paned.add(self.left, weight=3)

        self.right = ttk.Frame(self.paned, padding=(8, 0, 0, 0))
        self.right.columnconfigure(0, weight=1)
        self.right.rowconfigure(1, weight=1)
        preview_bar = ttk.Frame(self.right, padding=(0, 0, 0, 6))
        preview_bar.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Label(preview_bar, text="会话预览", font=(UI_FONT, 10, "bold")).pack(side=tk.LEFT)
        self._icon_button(preview_bar, "\ue711", "隐藏预览", self._toggle_preview).pack(side=tk.RIGHT)
        self._icon_button(preview_bar, "\ue74b", "最近对话", lambda: self.preview_text.yview_moveto(1)).pack(side=tk.RIGHT)
        self._icon_button(preview_bar, "\ue74a", "会话开头", lambda: self.preview_text.yview_moveto(0)).pack(side=tk.RIGHT)
        self.preview_tabs = ttk.Notebook(self.right)
        self.preview_tabs.grid(row=1, column=0, sticky="nsew")
        conversation = ttk.Frame(self.preview_tabs)
        details = ttk.Frame(self.preview_tabs)
        self.preview_tabs.add(conversation, text="对话")
        self.preview_tabs.add(details, text="详情")
        self.preview_text = tk.Text(conversation, wrap=tk.WORD, width=1, height=1, font=(UI_FONT, 10),
                                    state=tk.DISABLED, background="#ffffff", foreground="#30363d", relief=tk.FLAT,
                                    padx=14, pady=12, spacing1=3, spacing3=5, borderwidth=0)
        prev_scroll = ttk.Scrollbar(conversation, orient=tk.VERTICAL, command=self.preview_text.yview)
        self.preview_text.configure(yscrollcommand=prev_scroll.set)
        self.preview_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        prev_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.details_text = tk.Text(details, wrap=tk.WORD, width=1, height=1, font=(UI_FONT, 10),
                                    state=tk.DISABLED, background="white", relief="flat", padx=14, pady=12, spacing3=8)
        detail_scroll = ttk.Scrollbar(details, orient=tk.VERTICAL, command=self.details_text.yview)
        self.details_text.configure(yscrollcommand=detail_scroll.set)
        self.details_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        detail_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.preview_text.tag_configure("title", font=(UI_FONT, 11, "bold"), spacing3=8)
        self.preview_text.tag_configure("meta", foreground="#646c75", spacing3=2)
        self.preview_text.tag_configure("user", foreground="#14786b", font=(UI_FONT, 10, "bold"), spacing1=6)
        self.preview_text.tag_configure("assistant", foreground="#78549a", font=(UI_FONT, 10, "bold"), spacing1=6)
        if self.preview_visible_var.get():
            self.paned.add(self.right, weight=1)
        self._set_preview("未选择会话")

        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Button-3>", self._on_right_click)
        # 选中完全由我们接管（返回 "break" 抑制原生选中）——因为 Treeview 的
        # <<TreeviewSelect>> 是异步投递的，会在虚拟化重渲染后错误改写模型级选中。
        self.tree.bind("<Button-1>", self._on_click)
        # 虚拟化：滚轮 / 尺寸变化 / 键盘导航都驱动窗口滑动
        self.tree.bind("<MouseWheel>", self._on_wheel)      # Windows / macOS
        self.tree.bind("<Button-4>", self._on_wheel)        # Linux 上滚
        self.tree.bind("<Button-5>", self._on_wheel)        # Linux 下滚
        self.tree.bind("<Configure>", self._on_tree_configure)
        self.tree.bind("<ButtonRelease-1>", self._on_column_release, add="+")
        self.tree.bind("<Shift-MouseWheel>", self._horizontal_wheel)
        for seq in ("<Up>", "<Down>", "<Prior>", "<Next>", "<Home>", "<End>"):
            self.tree.bind(seq, self._nav)
        # 键盘快捷键
        self.tree.bind("<Return>", lambda _: self._resume_selected())
        self.tree.bind("<Delete>", lambda _: self._delete_selected())
        self.root.bind("<F5>", lambda _: self._refresh())
        self.root.bind("<Control-f>", lambda _: (self.search_entry.focus_set(),
                                                 self.search_entry.select_range(0, tk.END)))
        self.root.bind("<Control-p>", lambda _: self._toggle_preview())
        self.root.bind("<Control-n>", lambda _: self._show_new_session())
        self.tree.bind("<Control-a>", self._select_all)
        self.tree.bind("<Control-c>", lambda _: self._copy_session_id())
        self.tree.bind("<Control-d>", lambda _: self._edit_note())
        self.tree.bind("<Escape>", self._clear_selection)
        self.search_entry.bind("<Down>", self._focus_list)
        self.search_entry.bind("<Escape>", lambda _: self.search_var.set(""))

        # ─ 右键菜单 ─
        self.context_menu = tk.Menu(self.root, tearoff=0)
        self.context_menu.add_command(label="恢复会话", command=self._resume_selected)
        self.context_menu.add_command(label="编辑备注", command=self._edit_note)
        self.context_menu.add_command(label="修改分组", command=self._change_group)
        self.context_menu.add_command(label="复制 Session ID", command=self._copy_session_id)
        self.context_menu.add_command(label="打开项目目录", command=self._open_project_dir)
        self.context_menu.add_command(label="打开 JSONL 文件", command=self._open_jsonl_file)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="删除会话", command=self._delete_selected)
        self.density_var = tk.StringVar(value=self._density)
        self.layout_menu = tk.Menu(self.root, tearoff=0)
        self.layout_menu.add_checkbutton(label="对话预览", variable=self.preview_visible_var, command=self._set_preview_visibility)
        self.layout_menu.add_separator()
        self.layout_menu.add_radiobutton(label="舒适行距", variable=self.density_var, value="comfortable", command=self._set_density)
        self.layout_menu.add_radiobutton(label="紧凑行距", variable=self.density_var, value="compact", command=self._set_density)
        columns_menu = tk.Menu(self.layout_menu, tearoff=0)
        for col in self._COLUMNS:
            columns_menu.add_checkbutton(label=self._col_titles[col], variable=self._column_vars[col],
                                         command=self._on_columns_changed, state="disabled" if col == "title" else "normal")
        self.layout_menu.add_cascade(label="显示列", menu=columns_menu)
        self.layout_menu.add_separator()
        self.layout_menu.add_command(label="列宽适应窗口", command=self._fit_columns)
        self.layout_menu.add_command(label="重置布局", command=self._reset_layout)
        self.layout_menu.add_separator()
        self.layout_menu.add_checkbutton(label="跳过授权", variable=self.skip_perms_var, command=self._on_toggle_skip_perms)
        self._update_actions()

    def _icon_button(self, parent, icon, tip, command):
        names = {"\ue711": "x", "\ue72c": "refresh-cw", "\ue8a1": "panel-right", "\ue713": "settings-2",
                 "\ue894": "filter-x", "\ue70f": "square-pen", "\ue8f1": "tags", "\ue8b7": "folder-open",
                 "\ue8c8": "copy", "\ue74d": "trash-2", "\ue74a": "arrow-up-to-line", "\ue74b": "arrow-down-to-line"}
        image = self._icons.get(names.get(icon))
        button = ttk.Button(parent, text="" if image else icon, image=image or "", style="Icon.TButton", command=command)
        _Tooltip(button, tip)
        return button

    def _show_layout_menu(self):
        try:
            self.layout_menu.tk_popup(self.layout_btn.winfo_rootx(), self.layout_btn.winfo_rooty() + self.layout_btn.winfo_height())
        finally:
            self.layout_menu.grab_release()

    def _display_columns(self):
        return tuple(c for c in self._COLUMNS if self._column_vars[c].get())

    def _on_columns_changed(self):
        self.tree.configure(displaycolumns=self._display_columns())
        self._sync_column_widths()
        self._schedule_save()

    # ─── 刷新逻辑 ─────────────────────────────────────────────────

    def _refresh(self, preserve_scroll: bool = True):
        """完整刷新：重新扫描会话 + 标题 + 备注 + 分组 + 状态。

        扫描（scan + titles + alive，实测约 1.2s）全部放后台线程，
        否则启动和每次 F5 都会整窗卡死一秒多、拖动无响应。
        这几个函数只碰文件系统不碰 tkinter，且结果先落在线程内的局部列表里，
        直到回主线程才发布给 self.sessions，因此没有跨线程读写同一对象的竞态。
        preserve_scroll=True 时保留当前滚动位置（用于后台自动刷新，避免打断浏览）。
        """
        if self._scanning:
            # 已有扫描在跑。不能直接丢弃这次请求——删除会话后就是靠它把行去掉的，
            # 丢了列表会继续显示已删的会话。记为待办，等当前扫描收尾时再跑一次。
            self._refresh_pending = True
            self._refresh_pending_preserve = preserve_scroll
            return
        self._scanning = True
        self.refresh_btn.state(["disabled"])
        self.status_label.config(text="扫描中…")
        self._update_empty_hint(len(self.filtered_sessions))

        import threading

        def _work():
            exc_copy = None  # 在 _work() 函数作用域内捕获异常，lambda 就不用闭包捕获栈帧了
            try:
                sessions = scan_all_sessions()
                populate_titles(sessions)
                check_alive(sessions)
                notes = load_notes()
                assignments = load_groups().get("assignments", {})
                for s in sessions:
                    s.note = notes.get(s.session_id, "")
                    s.group = assignments.get(s.session_id, "")
            except Exception as e:
                exc_copy = e
            # 无论成功失败都投回主线程，而不是在后台线程里调 messagebox（会死锁）
            if exc_copy:
                self._post(lambda err=exc_copy: self._on_scan_failed(err))
            else:
                self._post(lambda sess=sessions, ps=preserve_scroll: self._on_scan_done(sess, ps))

        threading.Thread(target=_work, daemon=True).start()

    def _post(self, fn):
        """把回调投递到主线程；窗口已销毁时安全丢弃。"""
        if not self._closed:
            self._ui_queue.put(fn)

    def _drain_queue(self):
        if self._closed:
            return
        for _ in range(50):
            try:
                fn = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                self.root.report_callback_exception(*sys.exc_info())
        self._poll_id = self.root.after(40, self._drain_queue)

    def _on_scan_done(self, sessions: list, preserve_scroll: bool):
        """主线程：发布扫描结果并刷新视图。"""
        self._scanning = False
        self.refresh_btn.state(["!disabled"])
        self.sessions = sessions
        self._update_group_combo()
        self._apply_filter(preserve_scroll=preserve_scroll)
        self._on_select_preview()
        self._run_pending_refresh()

    def _on_scan_failed(self, err: Exception):
        """主线程：扫描异常时告知用户，而不是无声停在「扫描中…」。"""
        self._scanning = False
        self.refresh_btn.state(["!disabled"])
        self.status_label.config(text=f"扫描失败：{type(err).__name__}")
        messagebox.showerror("扫描失败", f"扫描会话时出错：\n{type(err).__name__}: {err}")
        self._run_pending_refresh()

    def _run_pending_refresh(self):
        """扫描收尾后，补跑期间被合并掉的刷新请求（如删除会话触发的那次）。"""
        if not self._refresh_pending:
            return
        self._refresh_pending = False
        self._refresh(preserve_scroll=self._refresh_pending_preserve)

    def _auto_refresh_status(self):
        """轻量刷新：在后台线程执行进程检测，完成后回到主线程更新 UI"""
        if self._scanning or getattr(self, "_checking", False):
            return  # 全量扫描正在跑，它自带 check_alive，无需再叠一次

        import threading

        # 对列表本身取快照：全量扫描完成时会整体替换 self.sessions，
        # 若这里边遍历边被换掉就会检测到一半旧一半新。
        import copy
        original = self.sessions
        snapshot = [copy.copy(s) for s in original]
        self._checking = True

        def _bg_check():
            """后台线程：执行耗时的进程检测和文件扫描"""
            try:
                check_alive(snapshot)
                has_new = list_session_ids() != {s.session_id for s in snapshot}
            except Exception:
                self._post(lambda: setattr(self, "_checking", False))
                return
            def publish():
                self._checking = False
                if self.sessions is not original or self._scanning:
                    return
                changed = any(a.alive != b.alive for a, b in zip(original, snapshot))
                for current, updated in zip(original, snapshot):
                    current.alive, current.pid = updated.alive, updated.pid
                self._on_bg_check_done(changed, has_new)
            self._post(publish)

        threading.Thread(target=_bg_check, daemon=True).start()

    def _on_bg_check_done(self, changed: bool, has_new: bool):
        """后台检测完成后，在主线程更新 UI"""
        if has_new:
            # 后台自动刷新：保留用户当前滚动位置，避免有新会话/删会话时列表突然跳回顶部
            self._refresh(preserve_scroll=True)
        elif changed:
            # 自动刷新只更新状态，保留用户当前滚动位置
            self._apply_filter(preserve_scroll=True)
        else:
            # 集合和状态都没变，也要重渲染当前窗口：时间列是相对文案
            # （「刚刚」/「N 分钟前」），不重画就会一直停在旧读数上。
            # 只有可视区约 20 行，开销可忽略。
            self._render_window()

    def _on_toggle_skip_perms(self):
        """勾选「跳过授权」时二次确认；取消勾选直接放行。"""
        if self.skip_perms_var.get():
            ok = messagebox.askyesno(
                "确认开启「跳过授权」",
                "开启后，恢复和新建的会话将：\n"
                "  · 跳过所有文件改动 / 命令执行的确认\n"
                "  · 关闭沙箱隔离\n\n"
                "该设置会被记住，直到你手动取消。\n"
                "确定开启吗？",
                icon="warning", default="no")
            if not ok:
                self.skip_perms_var.set(False)
                return
        self._save_settings()

    def _has_active_filters(self) -> bool:
        return bool(
            self.search_var.get().strip()
            or self._selected_group_real not in ("", "全部")
            or self.source_filter_var.get() not in ("", "全部")
            or self.notes_only_var.get()
            or self.running_only_var.get()
        )

    def _clear_filters(self):
        """一键复位全部筛选条件。

        搜索 + 分组 + 来源 + 两个勾选项叠加时，逐个复位要点五六次，
        而且很容易漏掉一个还在生效的条件，然后误以为「会话丢了」。
        """
        # 先静默改值，最后统一过滤一次，避免每个变量触发一轮重排
        self._suppress_filter = True
        try:
            self.search_var.set("")
            self.group_filter_var.set("全部")
            self._selected_group_real = "全部"
            self.source_filter_var.set("全部")
            self.notes_only_var.set(False)
            self.running_only_var.set(False)
        finally:
            self._suppress_filter = False
        # 取消可能在途的搜索防抖，避免它稍后又跑一次
        if self._search_after_id is not None:
            try:
                self.root.after_cancel(self._search_after_id)
            except (tk.TclError, ValueError):
                pass
            self._search_after_id = None
        self._apply_filter()

    def _update_group_combo(self):
        """更新分组下拉框选项列表，选项上带各分组的会话数。

        以会话的「有效分组」为准：自定义分组优先 → 项目名 → 未分组。
        选项显示成「AITest (42)」，选之前就知道这个分组有多少条，
        不用点进去才发现是空的或有几百条。

        显示串会随会话数变化，所以不能拿它当筛选值：另存 display→real 映射，
        过滤时按真实分组名匹配；刷新后也用「上次选中的真实名」重新定位到新的
        显示串，否则计数一变就会被判定为「选项已消失」而重置成「全部」。
        """
        counts: dict[str, int] = {}
        for s in self.sessions:
            counts[s.effective_group] = counts.get(s.effective_group, 0) + 1
        all_groups: set[str] = set(counts)
        # 同时把已登记但当前没会话归属的自定义分组也保留
        for g in load_groups().get("groups", []):
            if g:
                all_groups.add(g)
        all_groups.discard("")

        # 确保「未分组」固定排第一位（如果存在）
        has_ungrouped = "未分组" in all_groups
        all_groups.discard("未分组")
        ordered = sorted(all_groups)
        if has_ungrouped:
            ordered.insert(0, "未分组")

        self._group_display_to_real = {"全部": "全部"}
        values = ["全部"]
        for g in ordered:
            disp = f"{g} ({counts[g]})" if g in counts else f"{g} (0)"
            self._group_display_to_real[disp] = g
            values.append(disp)
        self.group_combo["values"] = values

        # 用「真实分组名」重新定位当前选中；分组真的消失了才回落到「全部」
        want_real = self._selected_group_real
        for disp, real in self._group_display_to_real.items():
            if real == want_real:
                if self.group_filter_var.get() != disp:
                    self._suppress_filter = True
                    try:
                        self.group_filter_var.set(disp)
                    finally:
                        self._suppress_filter = False
                self._selected_group_real = want_real
                return
        self._suppress_filter = True
        try:
            self.group_filter_var.set("全部")
        finally:
            self._suppress_filter = False
        self._selected_group_real = "全部"

    def _on_group_selected(self):
        """记下用户选的真实分组名（刷新后据此重新定位带计数的显示串）。"""
        self._selected_group_real = self._group_display_to_real.get(
            self.group_filter_var.get(), "全部")
        self._apply_filter()

    def _schedule_auto_refresh(self):
        """定时自动刷新"""
        self._auto_refresh_status()
        self._auto_refresh_id = self.root.after(AUTO_REFRESH_INTERVAL_MS, self._schedule_auto_refresh)

    # ─── 过滤与显示 ───────────────────────────────────────────────

    _SEARCH_DEBOUNCE_MS = 180

    def _on_search_changed(self):
        """搜索框防抖：连续输入时只在停顿后过滤一次。

        每个按键都全量过滤 + 排序（数百条会话、6 个字段 lower() 比较）会让
        输入明显发涩、删字尤其卡。停顿 180ms 再跑一次，视觉上仍是即时的。
        """
        if self._search_after_id is not None:
            try:
                self.root.after_cancel(self._search_after_id)
            except (tk.TclError, ValueError):
                pass
        self._search_after_id = self.root.after(
            self._SEARCH_DEBOUNCE_MS, self._run_search_filter)

    def _run_search_filter(self):
        self._search_after_id = None
        self._apply_filter()

    def _apply_filter(self, preserve_scroll: bool = False):
        """按搜索词 + 分组 + 来源 + 仅显示有备注 + 仅显示运行中 过滤并刷新表格。
        preserve_scroll=False（用户主动筛选/排序/刷新）时回到列表顶部；
        =True（后台自动刷新）时保留当前滚动位置。
        """
        if self._suppress_filter:
            return  # 批量改筛选变量期间（如「清除筛选」），由调用方最后统一过滤一次
        top_sid = (self.filtered_sessions[self._window_start].session_id
                   if self.filtered_sessions and self._window_start < len(self.filtered_sessions) else None)
        if not preserve_scroll:
            self._window_start = 0
        keyword = self.search_var.get().strip().lower()
        # 下拉里显示的是「分组名 (数量)」，映射回真实分组名再匹配
        group_filter = self._selected_group_real
        source_filter = self.source_filter_var.get() if hasattr(self, "source_filter_var") else "全部"
        notes_only = self.notes_only_var.get() if hasattr(self, "notes_only_var") else False
        running_only = self.running_only_var.get() if hasattr(self, "running_only_var") else False

        filtered = list(self.sessions)

        # 仅显示有备注
        if notes_only:
            filtered = [s for s in filtered if s.note]

        # 仅显示运行中
        if running_only:
            filtered = [s for s in filtered if s.alive]

        # 分组过滤：按「有效分组」匹配（自定义分组优先 → 项目名 → 未分组）
        if group_filter and group_filter != "全部":
            filtered = [s for s in filtered if s.effective_group == group_filter]

        # 来源过滤
        if source_filter and source_filter != "全部":
            filtered = [s for s in filtered if s.source == source_filter]

        # 关键词过滤
        if keyword:
            filtered = [
                s for s in filtered
                if keyword in s.title.lower()
                or keyword in s.project_name.lower()
                or keyword in s.cwd.lower()
                or keyword in s.session_id.lower()
                or keyword in s.note.lower()
                or keyword in s.effective_group.lower()
            ]

        # 排序（点表头切换列 / 升降序）
        filtered.sort(key=lambda s: self._sort_key(s), reverse=self._sort_reverse)

        self.filtered_sessions = filtered
        if not self._view_restored and not self._scanning:
            self._view_restored = True
            selected = self.ui_settings.get("selected_sids", [])
            self._selected_sids = {sid for sid in selected if isinstance(sid, str)} if isinstance(selected, list) else set()
            top_sid = self.ui_settings.get("top_sid")
            self._window_start = int(_number(self.ui_settings.get("window_start"), 0, 0, max(0, len(filtered) - 1)))
            self.tree.xview_moveto(_number(self.ui_settings.get("xview"), 0, 0, 1))
            preserve_scroll = True
        if preserve_scroll and top_sid:
            index = self._index_of(top_sid)
            if index is not None:
                self._window_start = index

        # 计数文本（渲染时再拼接「显示 X-Y/M」范围）
        running = sum(1 for s in self.sessions if s.alive)
        self._counts_text = (
            f"{len(filtered)} / {len(self.sessions)} 个会话 · {running} 个运行中"
        )
        self.clear_filters_btn.state(["!disabled"] if self._has_active_filters() else ["disabled"])
        self._populate_tree()
        self._schedule_save()

    def _populate_tree(self):
        """刷新表格：按当前过滤结果剪枝选中，然后只渲染可视窗口（虚拟化）。"""
        valid = {s.session_id for s in self.filtered_sessions}
        self._selected_sids &= valid
        # 预览的会话若已不在列表（被删除/被筛掉），清空预览面板，避免残留旧内容
        if self._previewed_sid is not None and self._previewed_sid not in self._selected_sids:
            self._on_select_preview()
        self._render_window()
        self._update_actions()

    # ─── 虚拟化渲染 ───────────────────────────────────────────────

    def _visible_rows(self) -> int:
        """当前表格可视区能容纳的行数（不含表头）。"""
        try:
            h = self.tree.winfo_height()
        except tk.TclError:
            h = 0
        if h <= 1:
            return 20  # 首次布局尚未完成时的估计值
        return max(1, (h - self._ROWH) // self._ROWH)

    def _render_window(self):
        """只把 filtered_sessions[start : start+可视行数] 渲染进 Treeview。"""
        total = len(self.filtered_sessions)
        vis = self._visible_rows()
        max_start = max(0, total - vis)
        self._window_start = max(0, min(self._window_start, max_start))
        start = self._window_start
        window = self.filtered_sessions[start:start + vis]

        self._rendering = True
        try:
            self.tree.delete(*self.tree.get_children())
            for i, s in enumerate(window):
                if s.alive:
                    status = "● 运行中"
                elif s.jsonl_path:
                    status = "○ 可恢复"
                else:
                    status = "✕ 已停止"
                # 斑马纹按「在完整列表中的绝对位置」算，滚动时条纹不会跳变
                stripe = "odd" if (start + i) % 2 else "even"
                tags = ("alive",) if s.alive else (stripe,)
                values = {"title": " ".join(s.title.split()), "status": status,
                          "time": _fmt_time(s.started_at), "source": s.source,
                          "project": s.project_name, "group": s.effective_group,
                          "note": " ".join(s.note.split())}
                self.tree.insert("", tk.END, iid=s.session_id, tags=tags,
                                 values=tuple(values[c] for c in self._COLUMNS))
            self._rendered_sids = [s.session_id for s in window]
            # 重放模型级选中（窗口内的部分）
            sel = [sid for sid in self._rendered_sids if sid in self._selected_sids]
            if sel:
                self.tree.selection_set(sel)
        finally:
            self._rendering = False

        # 滚动条 thumb 反映完整列表位置
        if total > 0:
            self.vscroll.set(start / total, min(1.0, (start + vis) / total))
        else:
            self.vscroll.set(0.0, 1.0)

        # 状态栏：计数 + 当前渲染范围
        rng = f" | 显示 {start + 1}-{min(start + vis, total)}/{total}" if total > vis else ""
        self.status_label.config(text="扫描中…" if self._scanning else self._counts_text + rng)
        self._cell_tip.hide()
        self._tip_cell = ()
        self._update_empty_hint(total)

    def _update_empty_hint(self, total: int):
        """列表为空时给出「为什么空」和「怎么办」，而不是留一片白。"""
        if total:
            self.empty_state.place_forget()
            return
        if self._scanning:
            msg = "正在扫描会话…"
        elif not self.sessions:
            msg = "暂无会话"
        else:
            # 有数据但被筛没了：把当前生效的条件列出来，用户才知道该松哪一个
            active = []
            if self.search_var.get().strip():
                active.append(f'搜索「{self.search_var.get().strip()}」')
            if self._selected_group_real not in ("", "全部"):
                active.append(f"分组「{self._selected_group_real}」")
            if self.source_filter_var.get() not in ("", "全部"):
                active.append(f"来源「{self.source_filter_var.get()}」")
            if self.notes_only_var.get():
                active.append("仅有备注")
            if self.running_only_var.get():
                active.append("仅运行中")
            cond = " + ".join(active) if active else "当前条件"
            cond = " ".join(cond.split())
            if len(cond) > 100:
                cond = cond[:100] + "…"
            msg = f"没有匹配的会话\n\n{cond}"
        self.empty_hint.config(text=msg, wraplength=max(100, min(self._px(420), self.tree.winfo_width() - 60)))
        self.empty_state.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        if self._scanning:
            self.empty_action.pack_forget()
        else:
            self.empty_action.config(text="清除筛选" if self.sessions else "新建会话",
                                     command=self._clear_filters if self.sessions else self._show_new_session)
            self.empty_action.pack(pady=(16, 0))

    def _yscroll(self, *args):
        """自定义滚动条 command：把 moveto/scroll 映射到窗口起点。"""
        if not args:
            return
        total = len(self.filtered_sessions)
        vis = self._visible_rows()
        max_start = max(0, total - vis)
        op = args[0]
        if op == "moveto":
            try:
                self._window_start = int(round(float(args[1]) * total))
            except (ValueError, IndexError):
                return
        elif op == "scroll":
            try:
                n = int(args[1])
            except (ValueError, IndexError):
                return
            what = args[2] if len(args) > 2 else "units"
            self._window_start += n * (vis if what == "pages" else 1)
        self._window_start = max(0, min(self._window_start, max_start))
        self._render_window()
        self._schedule_save()

    def _on_wheel(self, event):
        """鼠标滚轮滑动窗口（每次 3 行）。"""
        if getattr(event, "num", 0) == 4:
            step = -3
        elif getattr(event, "num", 0) == 5:
            step = 3
        else:
            step = -3 if event.delta > 0 else 3
        max_start = max(0, len(self.filtered_sessions) - self._visible_rows())
        self._window_start = max(0, min(self._window_start + step, max_start))
        self._render_window()
        self._schedule_save()
        return "break"

    def _nav(self, event):
        """键盘上下/翻页/Home/End：移动单选并保证其可见（跨窗口）。"""
        total = len(self.filtered_sessions)
        if total == 0:
            return "break"
        vis = self._visible_rows()
        active = self._window_start
        for i, s in enumerate(self.filtered_sessions):
            if s.session_id in self._selected_sids:
                active = i
                break
        key = event.keysym
        active += {"Up": -1, "Down": 1, "Prior": -vis, "Next": vis}.get(key, 0)
        if key == "Home":
            active = 0
        elif key == "End":
            active = total - 1
        active = max(0, min(active, total - 1))
        active_sid = self.filtered_sessions[active].session_id
        self._selected_sids = {active_sid}
        self._sel_anchor = active_sid
        if active < self._window_start:
            self._window_start = active
        elif active >= self._window_start + vis:
            self._window_start = active - vis + 1
        self._render_window()
        self._on_select_preview()
        return "break"

    # 悬浮提示里显示全文的列（状态/时间不会截断，没必要）
    _TIP_COLS = {"project", "group", "note", "title"}

    def _on_tree_motion(self, event):
        """光标停在被截断的单元格上时，用悬浮提示显示全文。

        只在「光标移到了另一个单元格」时才改提示内容，
        否则每个 <Motion> 事件都重建 Toplevel，会闪。
        """
        row = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        cell = (row, col)
        if cell == self._tip_cell:
            return
        self._tip_cell = cell
        self._cell_tip.hide()
        if not row or not col:
            self._cell_tip.set_text("")
            return
        try:
            idx = int(col[1:]) - 1        # "#3" -> 2
            col_name = self._display_columns()[idx]
        except (ValueError, IndexError):
            self._cell_tip.set_text("")
            return
        if col_name not in self._TIP_COLS:
            self._cell_tip.set_text("")
            return
        try:
            text = str(self.tree.set(row, col_name) or "")
        except tk.TclError:
            return
        if not text:
            self._cell_tip.set_text("")
            return
        # 只在文本确实放不下时提示，否则纯噪音
        try:
            avail = int(self.tree.column(col_name, "width")) - 10
            needed = self._text_font.measure(text)
        except tk.TclError:
            return
        self._cell_tip.set_text(text if needed > avail else "")
        self._cell_tip._schedule()

    def _index_of(self, sid: str) -> Optional[int]:
        """会话在 filtered_sessions 中的下标（找不到返回 None）"""
        for i, s in enumerate(self.filtered_sessions):
            if s.session_id == sid:
                return i
        return None

    def _reapply_tree_selection(self):
        """把模型级选中中「当前窗口可见」的部分同步为表格高亮。"""
        sel = [sid for sid in self._rendered_sids if sid in self._selected_sids]
        self._rendering = True
        try:
            if sel:
                self.tree.selection_set(sel)
            else:
                cur = self.tree.selection()
                if cur:
                    self.tree.selection_remove(cur)
        finally:
            self._rendering = False

    def _on_click(self, event):
        """接管鼠标左键选中：普通单击=替换，Ctrl=切换，Shift=范围（跨窗口）。
        返回 "break" 抑制 Treeview 原生选中，避免其异步 <<TreeviewSelect>> 改写模型。
        """
        region = self.tree.identify_region(event.x, event.y)
        if region == "separator":
            col = self.tree.identify_column(event.x)
            self._resizing_column = self._display_columns()[int(col[1:]) - 1]
            return
        row = self.tree.identify_row(event.y)
        if not row:
            if region == "nothing" and event.y > self._ROWH:
                return self._clear_selection()
            return
        self.tree.focus_set()
        ctrl = bool(event.state & 0x0004)
        shift = bool(event.state & 0x0001)
        if ctrl:
            if row in self._selected_sids:
                self._selected_sids.discard(row)
            else:
                self._selected_sids.add(row)
            self._sel_anchor = row
        elif shift and self._sel_anchor is not None:
            a = self._index_of(self._sel_anchor)
            b = self._index_of(row)
            if a is None or b is None:
                self._selected_sids = {row}
                self._sel_anchor = row
            else:
                lo, hi = sorted((a, b))
                self._selected_sids = {s.session_id for s in self.filtered_sessions[lo:hi + 1]}
        else:
            self._selected_sids = {row}
            self._sel_anchor = row
        self._reapply_tree_selection()
        self._on_select_preview()
        return "break"

    # ─── 排序 ─────────────────────────────────────────────────────

    @property
    def _time_tiebreak(self) -> int:
        """次级排序用的时间分量，恒为「新的在前」。

        list.sort(reverse=) 会翻转整个 key 元组，所以次级分量必须预先反向补偿：
        reverse=False 时取负（升序排完即时间倒序），reverse=True 时取正（翻转后仍是时间倒序）。
        这样无论主键箭头朝哪边，同组内始终最新的会话在上面。
        """
        return 1 if self._sort_reverse else -1

    def _sort_key(self, s: "SessionInfo"):
        col = self._sort_col
        t = s.started_at * self._time_tiebreak
        if col == "status":
            rank = 2 if s.alive else (1 if s.jsonl_path else 0)
            return (rank, t)
        if col == "project":
            return (s.project_name.lower(), t)
        if col == "group":
            return (s.effective_group.lower(), t)
        if col == "source":
            return (s.source, t)
        if col == "note":
            return (bool(s.note) if self._sort_reverse else not bool(s.note), t)
        if col == "title":
            return (s.title.lower(), t)
        return (s.started_at,)  # time（默认）：主键即时间，交给 reverse 控制方向

    # 首次点击即降序的列：时间（最新在前）、状态（运行中在前）
    _DESC_FIRST = frozenset({"time", "status"})

    def _sort_by(self, col: str):
        if self._sort_col == col:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_col = col
            self._sort_reverse = (col in self._DESC_FIRST)
        self._update_heading_arrows()
        self._apply_filter()

    def _on_heading_double_click(self, event):
        """双击列边界时按当前可见内容调整列宽。"""
        region = self.tree.identify_region(event.x, event.y)
        if region != "separator":
            return
        col = self.tree.identify_column(event.x)
        if not col:
            return
        # identify_column 返回 "#1" 格式，转为列名
        try:
            col_idx = int(col.replace("#", "")) - 1
            col_name = self._display_columns()[col_idx]
        except (ValueError, IndexError):
            return

        # 测量当前可见内容的最大宽度（表头 + 所有可见行）
        header_width = self._text_font.measure(self._col_titles[col_name]) + 20
        max_width = header_width

        for item in self.tree.get_children():
            try:
                val = str(self.tree.set(item, col_name) or "")
                if val:
                    w = self._text_font.measure(val) + 20  # 20px padding
                    max_width = max(max_width, w)
            except tk.TclError:
                continue

        # 保留可读下限，并避免长文本把单列扩展到整个桌面之外。
        new_width = max(self._px(self._MIN_WIDTHS[col_name]), min(self._px(800), max_width))
        try:
            self._column_widths[col_name] = new_width
            self._resizing_column = None
            self._sync_column_widths()
            self._schedule_save()
        except tk.TclError:
            pass

    def _update_heading_arrows(self):
        arrow = " ▼" if self._sort_reverse else " ▲"
        for col, title in self._col_titles.items():
            self.tree.heading(col, text=title + (arrow if col == self._sort_col else ""))

    # ─── 预览 ─────────────────────────────────────────────────────

    def _set_preview(self, text: str):
        self.preview_text.configure(state=tk.NORMAL)
        self.preview_text.delete("1.0", tk.END)
        self.preview_text.insert("1.0", text)
        self.preview_text.tag_add("title", "1.0", "1.end")
        for line_no, line in enumerate(text.splitlines(), 1):
            tag = "user" if line == "【用户】" else "assistant" if line == "【助手】" else None
            if tag:
                self.preview_text.tag_add(tag, f"{line_no}.0", f"{line_no}.end")
            elif line.startswith(("项目：", "创建：", "来源：", "分组：", "目录：", "Session：", "备注：")):
                self.preview_text.tag_add("meta", f"{line_no}.0", f"{line_no}.end")
        self.preview_text.configure(state=tk.DISABLED)
        self.preview_text.yview_moveto(0)

    def _on_select_preview(self, _event=None):
        self._update_actions()
        self._schedule_save()
        self._preview_generation += 1
        generation = self._preview_generation
        if self._preview_after_id:
            self.root.after_cancel(self._preview_after_id)
            self._preview_after_id = None
        if self._preview_future:
            self._preview_future.cancel()
        session = self._get_selected_session()
        if len(self._selected_sids) > 1:
            self._previewed_sid = None
            self._set_preview(f"已选择 {len(self._selected_sids)} 个会话")
            self._set_details("")
            return
        if not session:
            self._previewed_sid = None
            self._set_preview("未选择会话")
            self._set_details("")
            return
        if not self.preview_visible_var.get():
            return
        self._previewed_sid = session.session_id
        details = (
            f"{session.title}\n\n"
            f"项目：{session.project_name}\n"
            f"创建：{session.started_dt:%Y-%m-%d %H:%M}\n"
            f"来源：{session.source}\n"
            f"分组：{session.effective_group}\n"
            f"目录：{session.cwd}\n"
            f"Session：{session.session_id}\n"
        )
        if session.note:
            details += f"备注：{session.note}\n"
        details += "\n对话文件：\n" + "\n".join(str(p) for p in (session.rollout_paths or [session.jsonl_path]) if p)
        self._set_details(details)
        header = f"{' '.join(session.title.split())}\n\n{session.source} · {session.project_name} · {session.started_dt:%m-%d %H:%M}\n\n"
        self._set_preview(header + "正在读取…")

        def load():
            self._preview_after_id = None
            future = self._preview_executor.submit(extract_preview, session.jsonl_path, list(session.rollout_paths))
            self._preview_future = future
            def done(result):
                if result.cancelled():
                    return
                try:
                    body = result.result()
                except Exception as error:
                    body = f"读取失败：{error}"
                self._post(lambda: self._publish_preview(generation, header, body))
            future.add_done_callback(done)
        self._preview_after_id = self.root.after(100, load)

    def _set_details(self, text):
        self.details_text.configure(state=tk.NORMAL)
        self.details_text.delete("1.0", tk.END)
        self.details_text.insert("1.0", text)
        self.details_text.configure(state=tk.DISABLED)

    def _publish_preview(self, generation, header, body):
        if generation != self._preview_generation or not self.preview_visible_var.get():
            return
        self._preview_body = body
        self._set_preview(header + body)

    def _update_actions(self):
        selected = self._get_selected_sessions()
        one = len(selected) == 1
        for button in (self.resume_btn, self.note_btn, self.folder_btn):
            button.state(["!disabled"] if one else ["disabled"])
        for button in (self.group_btn, self.copy_btn, self.delete_btn):
            button.state(["!disabled"] if selected else ["disabled"])
        self.selection_label.config(text=f"已选 {len(selected)}" if selected else "")

    def _select_all(self, _event=None):
        self._selected_sids = {s.session_id for s in self.filtered_sessions}
        self._reapply_tree_selection()
        self._on_select_preview()
        return "break"

    def _clear_selection(self, _event=None):
        self._selected_sids.clear()
        self._sel_anchor = None
        self._reapply_tree_selection()
        self._on_select_preview()
        return "break"

    def _focus_list(self, _event=None):
        self.tree.focus_set()
        if self.filtered_sessions and not self._selected_sids:
            self._selected_sids = {self.filtered_sessions[self._window_start].session_id}
            self._reapply_tree_selection()
            self._on_select_preview()
        return "break"

    # ─── 布局持久化 ───────────────────────────────────────────────

    def _save_ui_state(self):
        """Persist stable preferences, never a pre-layout or minimized snapshot."""
        if self._save_after_id:
            self.root.after_cancel(self._save_after_id)
        self._save_after_id = None
        s = load_settings()
        if self._layout_ready:
            state = self.root.state()
            if state == "normal":
                self._normal_geometry = self.root.geometry()
            if state in ("normal", "zoomed"):
                s["window_state"] = state
            s.update(geometry=self._normal_geometry, col_widths=dict(self._column_widths),
                     sash_ratio=self._preferred_ratio, layout_version=self._LAYOUT_VERSION,
                     ui_scale=self._scale, preview_visible=self.preview_visible_var.get(), density=self._density)
            s["visible_columns"] = list(self._display_columns())
            s.pop("sash", None)
        s["source_filter"] = self.source_filter_var.get()
        s["group_filter"] = self._selected_group_real
        s["search"] = self.search_var.get()
        s["notes_only"] = self.notes_only_var.get()
        s["running_only"] = self.running_only_var.get()
        s["sort_col"] = self._sort_col
        s["sort_reverse"] = self._sort_reverse
        s["skip_permissions"] = bool(self.skip_perms_var.get())
        s["new_source"] = self.new_source_var.get()
        if self._view_restored:
            s["selected_sids"] = list(self._selected_sids)
            s["top_sid"] = self.filtered_sessions[self._window_start].session_id if self.filtered_sessions else None
            s["window_start"] = self._window_start
            s["xview"] = self.tree.xview()[0]
        if s == self.ui_settings:
            return True
        saved = save_settings(s)
        if saved:
            self.ui_settings = s
        else:
            self.bottom_label.config(text="设置保存失败，请检查磁盘权限")
        return saved

    def _schedule_save(self):
        if self._closed or not self._layout_ready:
            return
        if self._save_after_id:
            self.root.after_cancel(self._save_after_id)
        self._save_after_id = self.root.after(350, self._save_ui_state)

    def _on_root_configure(self, event):
        if event.widget is self.root and self._layout_ready:
            if self.root.state() == "normal":
                self._normal_geometry = self.root.geometry()
            self._schedule_save()

    def _on_paned_configure(self, _event=None):
        if self._layout_after_id:
            self.root.after_cancel(self._layout_after_id)
        self._layout_after_id = self.root.after_idle(self._restore_sash)

    def _sash_bounds(self):
        width = max(1, self.paned.winfo_width() - self._px(8))
        lower = min(self._px(480), round(width * 0.6))
        upper = max(lower, width - min(self._px(280), round(width * 0.35)))
        return width, lower, upper

    def _restore_sash(self):
        if self._layout_after_id:
            self.root.after_cancel(self._layout_after_id)
        self._layout_after_id = None
        if self._closed:
            return
        if self.paned.winfo_width() <= 1:
            self._layout_after_id = self.root.after(30, self._restore_sash)
            return
        width, lower, upper = self._sash_bounds()
        if not self._layout_ready and "sash_ratio" not in self.ui_settings:
            legacy = _number(self.ui_settings.get("sash"), 0, 0, 100000)
            if legacy >= lower:
                self._preferred_ratio = max(lower, min(upper, legacy)) / width
        if self.preview_visible_var.get() and not self._dragging_sash:
            position = max(lower, min(upper, round(width * self._preferred_ratio)))
            if self.paned.sashpos(0) != position:
                self.paned.sashpos(0, position)
        self._layout_ready = True
        self._schedule_save()

    def _on_sash_press(self, event):
        self._dragging_sash = self.paned.identify(event.x, event.y) == 0

    def _on_sash_release(self, _event=None):
        if not self._dragging_sash:
            return
        self._dragging_sash = False
        width, lower, upper = self._sash_bounds()
        pos = max(lower, min(upper, self.paned.sashpos(0)))
        self.paned.sashpos(0, pos)
        self._preferred_ratio = pos / width
        self._schedule_save()

    def _toggle_preview(self):
        self.preview_visible_var.set(not self.preview_visible_var.get())
        self._set_preview_visibility()

    def _set_preview_visibility(self):
        visible = self.preview_visible_var.get()
        attached = str(self.right) in self.paned.panes()
        if visible and not attached:
            self.paned.add(self.right, weight=1)
        elif not visible and attached:
            self.paned.forget(self.right)
        self._restore_sash()
        self._on_select_preview()

    def _reset_split(self, event=None):
        if event is not None and self.paned.identify(event.x, event.y) != 0:
            return
        self._preferred_ratio = self._DEFAULT_RATIO
        self._dragging_sash = False
        self._restore_sash()
        return "break"

    def _set_density(self):
        self._density = self.density_var.get()
        self._ROWH = self._px(25 if self._density == "compact" else 32)
        ttk.Style(self.root).configure("Treeview", rowheight=self._ROWH)
        self._render_window()
        self._schedule_save()

    def _on_tree_configure(self, _event=None):
        if not self._resizing_column:
            self._sync_column_widths()
        self._render_window()

    def _sync_column_widths(self):
        spare = max(0, self.tree.winfo_width() - 2 - sum(self._column_widths[c] for c in self._display_columns()))
        for col, width in self._column_widths.items():
            self.tree.column(col, width=width + (spare if col == "title" else 0))

    def _on_column_release(self, _event=None):
        if self._resizing_column:
            col = self._resizing_column
            self._column_widths[col] = max(self._px(self._MIN_WIDTHS[col]), int(self.tree.column(col, "width")))
            self._resizing_column = None
            self._sync_column_widths()
            self._schedule_save()

    def _fit_columns(self):
        self._column_widths = {col: self._px(width) for col, width in self._MIN_WIDTHS.items()}
        room = max(0, self.tree.winfo_width() - 2 - sum(self._column_widths[c] for c in self._display_columns()))
        self._column_widths["title"] += room
        self._sync_column_widths()
        self.tree.xview_moveto(0)
        self._schedule_save()

    def _reset_layout(self):
        self._column_widths = {col: self._px(width) for col, width in self._DEFAULT_WIDTHS.items()}
        for col, var in self._column_vars.items():
            var.set(col != "group")
        self.tree.configure(displaycolumns=self._display_columns())
        self._preferred_ratio = self._DEFAULT_RATIO
        self.preview_visible_var.set(True)
        self.density_var.set("comfortable")
        self._set_density()
        self._set_preview_visibility()
        self._sync_column_widths()
        self.tree.xview_moveto(0)
        self._schedule_save()

    def _xscroll(self, *args):
        self.tree.xview(*args)
        self._schedule_save()

    def _horizontal_wheel(self, event):
        self._xscroll("scroll", -2 if event.delta > 0 else 2, "units")
        return "break"

    def _on_close(self):
        if self._closed:
            return
        if not self._save_ui_state() and not messagebox.askyesno(
                "设置未保存", "当前布局未能写入磁盘，仍要关闭吗？", parent=self.root, default="no"):
            return
        self._closed = True
        for job in (self._auto_refresh_id, self._save_after_id, self._layout_after_id,
                    self._preview_after_id, self._search_after_id, self._poll_id,
                    getattr(self, "_instance_poll_id", None)):
            if job:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
        self._cell_tip.hide()
        self._preview_executor.shutdown(wait=False, cancel_futures=True)
        _release_single_instance()
        self.root.destroy()

    # ─── 交互操作 ─────────────────────────────────────────────────

    def _get_selected_session(self) -> Optional[SessionInfo]:
        """仅在选中一条会话时返回，用于恢复、备注等单选操作。
        用模型级 _selected_sids，即使选中行已滚出可视窗口也有效。"""
        if len(self._selected_sids) != 1:
            return None
        for s in self.filtered_sessions:
            if s.session_id in self._selected_sids:
                return s
        return None

    def _get_selected_sessions(self) -> list[SessionInfo]:
        """返回所有选中会话（用于多选语义的操作：批量删除），跨窗口有效。"""
        return [s for s in self.filtered_sessions if s.session_id in self._selected_sids]

    def _on_double_click(self, event):
        region = self.tree.identify_region(event.x, event.y)
        if region == "separator":
            self._on_heading_double_click(event)
            return "break"
        if region == "cell" and self.tree.identify_row(event.y) in self._selected_sids:
            self._resume_selected()
            return "break"

    def _resume_selected(self):
        session = self._get_selected_session()
        if not session:
            return
        if session.alive:
            messagebox.showinfo(
                "运行中",
                (f"会话仍在运行中（PID {session.pid}）。\n" if session.pid else "会话在最近 60 秒内仍有写入。\n") +
                "请切换到对应的终端窗口。")
            return
        if not session.jsonl_path:
            messagebox.showinfo(
                "无法恢复",
                f"此会话没有对话记录，无法恢复。\n"
                f"Session ID: {session.session_id[:8]}…")
            return
        self._save_ui_state()
        resume_session(session, skip_permissions=self.skip_perms_var.get())

    def _on_right_click(self, event):
        item = self.tree.identify_row(event.y)
        if not item:
            return
        # 点击的行不在已选中集合里 → 切换单选到当前行；否则保留跨窗口多选
        if item not in self._selected_sids:
            self._selected_sids = {item}
            self._render_window()
            self._on_select_preview()
        # 动态更新「删除」菜单标签反映选中数量（模型级，跨窗口）
        n = len(self._selected_sids)
        for index in (0, 1, 4, 5):
            self.context_menu.entryconfigure(index, state="normal" if n == 1 else "disabled")
        delete_label = "删除会话" if n <= 1 else f"删除选中的 {n} 个会话"
        try:
            # 「删除会话」是最后一个 command，索引可能随分隔符位置变化，按 label 查找更稳
            last_index = self.context_menu.index("end")
            if last_index is not None:
                self.context_menu.entryconfigure(last_index, label=delete_label)
        except tk.TclError:
            pass
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()

    def _copy_session_id(self):
        sessions = self._get_selected_sessions()
        if sessions:
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(s.session_id for s in sessions))
            self.bottom_label.config(text=f"已复制 {len(sessions)} 个 Session ID")

    def _open_project_dir(self):
        session = self._get_selected_session()
        if not session or not session.cwd:
            return
        cwd = session.cwd
        # cwd 可能指向已不存在的路径（如迁移到别的电脑后旧盘符失效）
        if not os.path.isdir(cwd):
            messagebox.showwarning("目录不存在", f"项目目录不存在（可能已迁移或删除）：\n{cwd}")
            return
        try:
            if sys.platform == "win32" or os.name == "nt":
                os.startfile(cwd)
            else:
                try:
                    subprocess.Popen(["explorer.exe", cwd])
                except FileNotFoundError:
                    subprocess.Popen(["xdg-open", cwd])
        except OSError as e:
            messagebox.showerror("打开失败", f"无法打开目录：\n{cwd}\n\n{e}")

    def _open_jsonl_file(self):
        session = self._get_selected_session()
        if not session:
            return
        jsonl_path = session.jsonl_path
        if not jsonl_path or not jsonl_path.is_file():
            messagebox.showinfo("提示", "未找到对应的 JSONL 对话文件。")
            return
        try:
            if sys.platform == "win32" or os.name == "nt":
                os.startfile(str(jsonl_path))
            else:
                try:
                    subprocess.Popen(["explorer.exe", str(jsonl_path)])
                except FileNotFoundError:
                    subprocess.Popen(["xdg-open", str(jsonl_path)])
        except OSError as e:
            messagebox.showerror("打开失败", f"无法打开文件：\n{jsonl_path}\n\n{e}")

    def _browse_path(self, path_var: tk.StringVar):
        from tkinter import filedialog
        path = filedialog.askdirectory(
            initialdir=path_var.get(),
            title="选择项目目录")
        if path:
            path_var.set(path.replace("/", "\\"))
            self._save_paths()

    def _save_paths(self):
        """保存两个快捷目录，失败时保留当前输入并给出反馈。"""
        saved = save_new_session_paths([
            self.new_session_path_var1.get(),
            self.new_session_path_var2.get(),
        ])
        if not saved:
            self.bottom_label.config(text="快捷目录保存失败，请检查磁盘权限")
        return saved

    def _save_settings(self):
        self._save_ui_state()

    def _new_session_source(self) -> Optional[str]:
        src = self.new_source_var.get()
        return src if src in ALL_SOURCES else None

    def _center_dialog(self, dialog, width, height):
        dialog.update_idletasks()
        w, h = self._px(width), max(self._px(height), dialog.winfo_reqheight())
        x = self.root.winfo_x() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - h) // 2
        cx = self.root.winfo_x() + self.root.winfo_width() // 2
        cy = self.root.winfo_y() + self.root.winfo_height() // 2
        area = next((a for a in self._areas if a[0] <= cx < a[2] and a[1] <= cy < a[3]), self._areas[0])
        w, h = min(w, area[2] - area[0] - 24), min(h, area[3] - area[1] - 64)
        x = max(area[0] + 8, min(x, area[2] - w - 16))
        y = max(area[1] + 8, min(y, area[3] - h - 48))
        dialog.geometry(f"{w}x{h}+{x}+{y}")
        dialog.minsize(w, h)

    def _show_new_session(self):
        if getattr(self, "_new_dialog", None) and self._new_dialog.winfo_exists():
            self._new_dialog.lift()
            return
        dialog = tk.Toplevel(self.root)
        self._new_dialog = dialog
        dialog.title("新建会话")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(True, False)
        content = ttk.Frame(dialog, padding=16)
        content.pack(fill=tk.BOTH, expand=True)
        content.columnconfigure(1, weight=1)
        ttk.Label(content, text="CLI").grid(row=0, column=0, sticky="w", pady=(0, 14))
        ttk.Combobox(content, textvariable=self.new_source_var, state="readonly", values=ALL_SOURCES,
                     width=14).grid(row=0, column=1, sticky="w", padx=8, pady=(0, 14))
        for row, var in enumerate((self.new_session_path_var1, self.new_session_path_var2), 1):
            ttk.Label(content, text=f"目录 {row}").grid(row=row, column=0, sticky="w", pady=6)
            entry = ttk.Entry(content, textvariable=var, width=42)
            entry.grid(row=row, column=1, sticky="ew", padx=8, pady=6)
            entry.bind("<FocusOut>", lambda _: self._save_paths())
            self._icon_button(content, "\ue8b7", "选择项目目录", lambda v=var: self._browse_path(v)).grid(row=row, column=2)
            def launch(v=var):
                self._save_paths()
                self._save_ui_state()
                if self._open_new_session(v):
                    dialog.destroy()
            ttk.Button(content, text="新建", command=launch).grid(row=row, column=3, padx=(8, 0))
            entry.bind("<Return>", lambda _, fn=launch: fn())
        cb = ttk.Checkbutton(content, text="跳过授权", style="Danger.TCheckbutton", variable=self.skip_perms_var,
                             command=self._on_toggle_skip_perms)
        cb.grid(row=3, column=1, sticky="w", padx=8, pady=(14, 0))
        _Tooltip(cb, "恢复和新建会话时跳过授权确认并关闭沙箱隔离")
        def close():
            self._save_paths()
            self._save_ui_state()
            dialog.destroy()
        ttk.Button(content, text="关闭", command=close).grid(row=3, column=3, pady=(14, 0))
        dialog.protocol("WM_DELETE_WINDOW", close)
        dialog.bind("<Escape>", lambda _: close())
        self._center_dialog(dialog, 660, 230)

    def _open_new_session(self, path_var: tk.StringVar):
        active_source = self._new_session_source()
        if active_source is None:
            messagebox.showinfo(
                "请选择 CLI", "请在新建会话窗口选择 claude 或 codex。", parent=self.root)
            return
        cwd = path_var.get().strip()
        # 路径为空（如迁移到新电脑后默认路径被清空）时，直接弹目录选择框让用户现选
        if not cwd:
            from tkinter import filedialog
            picked = filedialog.askdirectory(title="选择新建会话的项目目录")
            if not picked:
                return  # 用户取消
            cwd = picked.replace("/", "\\")
            path_var.set(cwd)
        self._save_paths()
        if not os.path.isdir(cwd):
            messagebox.showwarning("路径无效", f"目录不存在：{cwd}")
            return
        # active_source 已在函数开头确定（非具体 CLI 时已提前 return）
        cmd = _cmd_for_source(active_source)
        # 「跳过授权」按来源映射:claude 系→--dangerously-skip-permissions;codex→yolo 标志
        extra_args = _skip_flag_for_source(active_source) if self.skip_perms_var.get() else ""

        argv = [cmd]
        if extra_args:
            argv.append(extra_args.strip())
        try:
            launch_windows_session(
                argv=argv, cwd=cwd, source=active_source,
                git_bash=_find_git_bash_path() if active_source == SOURCE_CLAUDE else "")
            return True
        except (OSError, ValueError) as e:
            messagebox.showerror("新建失败", f"无法启动会话终端：\n{e}")

    def _change_group(self):
        sessions = self._get_selected_sessions()
        if not sessions:
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("修改分组")
        dialog.resizable(True, False)
        dialog.transient(self.root)
        dialog.grab_set()

        ttk.Label(dialog, text=f"已选择 {len(sessions)} 个会话").pack(padx=12, pady=(12, 6), anchor=tk.W)

        # 下拉框：已有自定义分组 + 项目名（保留旧行为）
        groups_data = load_groups()
        existing_groups = list(groups_data.get("groups", []))
        project_names = sorted({s.project_name for s in self.sessions if s.project_name})
        all_choices = sorted(set(existing_groups + project_names))

        ttk.Label(dialog, text="分组名称").pack(padx=12, pady=(4, 0), anchor=tk.W)
        # 默认值：当前自定义分组；未分组时留空，不再回落到项目名
        groups = {s.group for s in sessions}
        group_var = tk.StringVar(value=next(iter(groups)) if len(groups) == 1 else "")
        combo = ttk.Combobox(dialog, textvariable=group_var, values=all_choices, width=40)
        combo.pack(padx=10, pady=4, fill=tk.X)
        combo.focus_set()

        def on_save():
            new_group = group_var.get().strip()
            gd = load_groups()
            if new_group:
                # 自定义分组（即使等于项目名也允许显式打标）
                for session in sessions:
                    gd["assignments"][session.session_id] = new_group
                if new_group not in gd["groups"]:
                    gd["groups"].append(new_group)
            else:
                # 留空 = 未分组：清除 assignment
                for session in sessions:
                    gd["assignments"].pop(session.session_id, None)
            if not save_groups(gd):
                messagebox.showerror("保存失败", "分组未能写入磁盘。", parent=dialog)
                return
            for session in sessions:
                session.group = new_group
            self._update_group_combo()
            self._apply_filter(preserve_scroll=True)
            self._on_select_preview()
            self.bottom_label.config(
                text=f"已更新 {len(sessions)} 个会话的分组")
            dialog.destroy()

        def on_clear():
            group_var.set("")
            on_save()

        combo.bind("<Return>", lambda _: on_save())
        combo.bind("<Escape>", lambda _: dialog.destroy())

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(pady=5)
        ttk.Button(btn_frame, text="保存", command=on_save).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="清除分组", command=on_clear).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="取消", command=dialog.destroy).pack(side=tk.LEFT, padx=5)
        self._center_dialog(dialog, 480, 220)

    def _edit_note(self):
        session = self._get_selected_session()
        if not session:
            return

        # 弹出输入框
        dialog = tk.Toplevel(self.root)
        dialog.title("编辑备注")
        dialog.resizable(True, False)
        dialog.transient(self.root)
        dialog.grab_set()

        ttk.Label(dialog, text=session.title, wraplength=self._px(450)).pack(padx=12, pady=(12, 6), anchor=tk.W)

        note_var = tk.StringVar(value=session.note)
        entry = ttk.Entry(dialog, textvariable=note_var, width=50)
        entry.pack(padx=10, pady=5, fill=tk.X)
        entry.focus_set()
        entry.select_range(0, tk.END)

        def on_save():
            new_note = note_var.get().strip()
            notes = load_notes()
            if new_note:
                notes[session.session_id] = new_note
            else:
                notes.pop(session.session_id, None)
            if not save_notes(notes):
                messagebox.showerror("保存失败", "备注未能写入磁盘。", parent=dialog)
                return
            session.note = new_note
            self._apply_filter(preserve_scroll=True)
            self._on_select_preview()
            self.bottom_label.config(text=f"备注已保存：{new_note[:30]}" if new_note else "备注已清除")
            dialog.destroy()

        entry.bind("<Return>", lambda _: on_save())
        entry.bind("<Escape>", lambda _: dialog.destroy())

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(pady=5)
        ttk.Button(btn_frame, text="保存", command=on_save).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="取消", command=dialog.destroy).pack(side=tk.LEFT, padx=5)
        self._center_dialog(dialog, 500, 200)

    def _delete_selected(self):
        sessions = self._get_selected_sessions()
        if not sessions:
            return

        # 1. 校验：运行中的会话不能删
        running = [s for s in sessions if s.alive]
        if running:
            preview = "\n".join(f"  · {s.title[:40]}" for s in running[:5])
            if len(running) > 5:
                preview += f"\n  · …（共 {len(running)} 个）"
            messagebox.showwarning(
                "无法删除",
                f"以下 {len(running)} 个会话仍在运行，请先关闭对应终端：\n\n{preview}")
            return

        # 2. 确认对话框
        if len(sessions) == 1:
            s = sessions[0]
            status_text = "● 运行中" if s.alive else ("○ 可恢复" if s.jsonl_path else "✕ 已停止")
            confirm_msg = (
                f"确定要删除此会话？\n\n"
                f"标题：{s.title[:50]}\n"
                f"时间：{s.started_dt:%Y-%m-%d %H:%M}\n"
                f"状态：{status_text}\n"
                f"Session ID：{s.session_id[:8]}…\n\n"
                f"💡 对话记录将移入回收站（.session_trash），可手动恢复"
            )
        else:
            preview_lines = []
            for s in sessions[:8]:
                status = "●" if s.alive else ("○" if s.jsonl_path else "✕")
                preview_lines.append(f"  {status} [{s.started_dt:%m-%d %H:%M}] {s.title[:38]}")
            preview = "\n".join(preview_lines)
            if len(sessions) > 8:
                preview += f"\n  …（共 {len(sessions)} 个）"
            confirm_msg = (
                f"确定要删除以下 {len(sessions)} 个会话？\n\n{preview}\n\n"
                f"💡 对话记录将移入回收站（.session_trash），可手动恢复"
            )
        if not messagebox.askyesno("确认删除", confirm_msg, parent=self.root, default="no"):
            return

        # 3. 批量移入回收站（可恢复，非永久删除）
        import shutil
        from datetime import datetime
        errors: list[str] = []
        notes = load_notes()
        gd = load_groups()
        gd_changed = False
        notes_changed = False

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        trash_batch = SESSION_TRASH_DIR / stamp
        try:
            trash_batch.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("回收站创建失败", f"{trash_batch}\n{e}")
            return

        for session in sessions:
            sid_short = session.session_id[:8]

            # 3.1 JSONL 文件 → 回收站
            if session.jsonl_path and session.jsonl_path.is_file():
                if session.source in CODEX_STYLE_DIRS and session.rollout_paths:
                    # codex：整段 rollout 移入回收站，保留 <年>/<月>/<日>/<文件名> 相对路径
                    sessions_dir = CODEX_STYLE_DIRS.get(session.source)
                    for rp in session.rollout_paths:
                        if not rp.is_file():
                            continue
                        try:
                            rel = rp.relative_to(sessions_dir) if sessions_dir else Path(rp.name)
                        except ValueError:
                            rel = Path(rp.name)
                        dst = trash_batch / rel
                        try:
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(rp), str(dst))
                        except OSError as e:
                            errors.append(f"{sid_short} rollout {rp.name}: {e}")
                else:
                    try:
                        shutil.move(str(session.jsonl_path),
                                    str(trash_batch / f"{session.session_id}.jsonl"))
                    except OSError as e:
                        errors.append(f"{sid_short}.jsonl: {e}")

            # 3.2 子目录（subagents 等）→ 回收站
            if session.jsonl_path:
                session_dir = session.jsonl_path.parent / session.session_id
                if session_dir.is_dir():
                    try:
                        shutil.move(str(session_dir), str(trash_batch / session.session_id))
                    except OSError as e:
                        errors.append(f"{sid_short} 子目录: {e}")

            # 3.3 sessions/ PID 文件（按 source 取对应目录）→ 回收站
            # codex 不在 SOURCE_DIRS（无 pid 文件），用 .get 兜底避免 KeyError
            src_dirs = SOURCE_DIRS.get(session.source)
            sessions_dir = src_dirs["sessions"] if src_dirs else None
            if sessions_dir and sessions_dir.is_dir():
                for fp in list(sessions_dir.glob("*.json")):
                    try:
                        data = json.loads(fp.read_text(encoding="utf-8"))
                        if data.get("sessionId") == session.session_id:
                            shutil.move(str(fp), str(trash_batch / f"session_{fp.name}"))
                    except (json.JSONDecodeError, OSError):
                        continue

            # 3.4 备注 / 3.5 分组（先在内存里改，循环结束统一存盘）
            if session.session_id in notes:
                del notes[session.session_id]
                notes_changed = True
            assignments = gd.get("assignments", {})
            if session.session_id in assignments:
                del assignments[session.session_id]
                gd_changed = True

        if notes_changed:
            save_notes(notes)
        if gd_changed:
            save_groups(gd)

        # 4. 反馈
        if errors:
            preview = "\n".join(errors[:20])
            if len(errors) > 20:
                preview += f"\n…（共 {len(errors)} 条错误）"
            messagebox.showwarning("部分移入回收站失败", preview)
        else:
            if len(sessions) == 1:
                self.bottom_label.config(text=f"已移入回收站：{sessions[0].title[:50]}")
            else:
                self.bottom_label.config(text=f"已移入回收站 {len(sessions)} 个会话")

        # 5. 刷新列表
        self._refresh()


# ─── 单实例 ───────────────────────────────────────────────────────

# 锁文件里的程序标识前缀，用于校验/排查
_LOCK_TAG = "cli-session-manager"
_instance_mutex = None
_instance_event = None


def _lock_path() -> str:
    """单实例锁文件路径（跟随本工具其他临时文件的 gettempdir 约定）。"""
    import tempfile
    return os.path.join(tempfile.gettempdir(), "cli_session_manager.lock")


def _read_lock_pid() -> Optional[int]:
    """读锁文件里的 PID，解析失败/文件不存在返回 None。"""
    try:
        with open(_lock_path(), "r", encoding="utf-8") as handle:
            raw = handle.read().strip()
    except OSError:
        return None
    # 内容格式 "cli-session-manager|<pid>"；只信任带正确前缀的锁
    parts = raw.split("|")
    if len(parts) != 2 or parts[0] != _LOCK_TAG:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def _acquire_single_instance():
    """Repeated launches notify the existing window; they never terminate it."""
    global _instance_mutex, _instance_event
    if os.name != "nt":
        return True
    try:
        import ctypes
        import hashlib
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateEventW.restype = wintypes.HANDLE
        kernel.SetEvent.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        identity = hashlib.sha256(str(HOME).encode("utf-8")).hexdigest()[:16]
        name = "Local\\CLI-Session-Manager-" + identity
        event = kernel.CreateEventW(None, False, False, name + "-activate")
        mutex = kernel.CreateMutexW(None, False, name)
        exists = ctypes.get_last_error() == 183
        if not mutex or not event:
            if mutex:
                kernel.CloseHandle(mutex)
            if event:
                kernel.CloseHandle(event)
            return True
        if exists:
            kernel.SetEvent(event)
            kernel.CloseHandle(event)
            kernel.CloseHandle(mutex)
            return False
        _instance_mutex, _instance_event = mutex, event
        with open(_lock_path(), "w", encoding="utf-8") as f:
            f.write(f"{_LOCK_TAG}|{os.getpid()}")
    except (OSError, AttributeError):
        pass
    return True


def _release_single_instance():
    """退出时清理锁文件——仅当锁里的 PID 是自己（避免删掉已被新实例抢占的锁）。"""
    global _instance_mutex, _instance_event
    if os.name != "nt":
        return
    try:
        if _read_lock_pid() == os.getpid():
            os.remove(_lock_path())
    except OSError:
        pass
    import ctypes
    from ctypes import wintypes
    close = ctypes.windll.kernel32.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    for handle in (_instance_event, _instance_mutex):
        if handle:
            close(handle)
    _instance_mutex = _instance_event = None


def _poll_instance(app):
    if app._closed:
        return
    if _instance_event:
        import ctypes
        from ctypes import wintypes
        wait = ctypes.windll.kernel32.WaitForSingleObject
        wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        if wait(_instance_event, 0) == 0:
            if app.root.state() == "iconic":
                app.root.deiconify()
            app.root.lift()
            app.root.focus_force()
    app._instance_poll_id = app.root.after(200, lambda: _poll_instance(app))


# ─── 入口 ──────────────────────────────────────────────────────────

def main():
    if not _acquire_single_instance():
        return

    # 高 DPI 支持（Windows）— 必须在创建 Tk 之前调用
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    root = tk.Tk()

    app = SessionManagerApp(root)
    _poll_instance(app)
    root.mainloop()


if __name__ == "__main__":
    main()
