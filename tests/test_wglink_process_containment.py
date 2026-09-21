"""The add-in kills only the process tree it started (A8 brief 7, add-in side).

The resampler used ``subprocess.run(timeout=)``, which kills its direct child
only: a resampler that started a process of its own left it running, and on
Windows ``run`` then waited for that orphan to close the pipes it inherited, so
the timeout did not bound Fusion's wait. ``wglink_core._run_contained`` starts
the child in its own session (POSIX) or a kill-on-close job object (Windows) and
kills that on timeout.

The other half is the absence: WGLink kills nothing else. That is asserted
structurally over every add-in source file, with a positive control that the
scan does find a kill, and behaviourally with stand-in "foreign" processes --
one in the caller's own process group, as Fusion's other children would be, and
one in a session of its own -- that must survive a contained timeout.
"""

from __future__ import annotations

import ast
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import time
import types

import pytest


ROOT = Path(__file__).resolve().parents[1]
ADDINS = ROOT / "fusion-addins"
CORE = ADDINS / "WGLink" / "wglink_core.py"


@pytest.fixture
def core(monkeypatch):
    adsk = types.ModuleType("adsk")
    adsk.__path__ = []
    adsk_core = types.ModuleType("adsk.core")
    adsk_fusion = types.ModuleType("adsk.fusion")
    adsk.core = adsk_core
    adsk.fusion = adsk_fusion
    monkeypatch.setitem(sys.modules, "adsk", adsk)
    monkeypatch.setitem(sys.modules, "adsk.core", adsk_core)
    monkeypatch.setitem(sys.modules, "adsk.fusion", adsk_fusion)
    monkeypatch.syspath_prepend(str(CORE.parent))
    spec = importlib.util.spec_from_file_location("wglink_core_containment_test", CORE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


# -- process probes (test-side only) -----------------------------------------------


def _alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers signal 0; it is not running.
    try:
        status = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=10
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return True
    return bool(status) and not status.startswith("Z")


def _gone_within(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def _stop(pid: int) -> None:
    """Test clean-up for a process this test itself caused to exist."""

    try:
        os.kill(pid, signal.SIGTERM if os.name == "nt" else signal.SIGKILL)
    except OSError:
        pass


# A child that starts a grandchild, reports the grandchild's pid, then hangs.
# The grandchild ticks a file so "it is running" is measured, not assumed.
_CHILD = textwrap.dedent(
    """
    import subprocess, sys, time
    pid_file, tick_file, inherit = sys.argv[1], sys.argv[2], sys.argv[3] == "inherit"
    grandchild = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time\\n"
         "while True:\\n"
         "    open(sys.argv[1], 'a').write('.')\\n"
         "    time.sleep(0.05)\\n",
         tick_file],
        stdout=None if inherit else subprocess.DEVNULL,
        stderr=None if inherit else subprocess.DEVNULL,
    )
    open(pid_file + ".part", "w").write(str(grandchild.pid))
    import os
    os.replace(pid_file + ".part", pid_file)
    time.sleep(120)
    """
)


def _command(tmp_path: Path, inherit: str) -> tuple[list[str], Path, Path]:
    script = tmp_path / "child.py"
    script.write_text(_CHILD)
    pid_file = tmp_path / "grandchild.pid"
    tick_file = tmp_path / "ticks"
    return [sys.executable, str(script), str(pid_file), str(tick_file), inherit], pid_file, tick_file


def _ticks(path: Path) -> int:
    try:
        return len(path.read_text())
    except OSError:
        return 0


def _wait_for(path: Path, seconds: float = 30.0) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)


@pytest.mark.parametrize("inherit", ["inherit", "devnull"])
def test_a_resampler_timeout_kills_the_grandchild_it_started(
    core, tmp_path: Path, inherit: str
) -> None:
    """``inherit``: the grandchild holds the output pipes, which is also what made
    ``subprocess.run``'s timeout unbounded on Windows."""

    command, pid_file, tick_file = _command(tmp_path, inherit)
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        core._run_contained(command, cwd=str(tmp_path), env=dict(os.environ), timeout=3.0)

    elapsed = time.monotonic() - started
    assert pid_file.exists(), "the child never started its grandchild"
    grandchild = int(pid_file.read_text())
    try:
        # The grandchild ran (the measurement below can rise)...
        assert _ticks(tick_file) > 0
        # ...and now it is gone, and the wait was bounded.
        assert _gone_within(grandchild, 10.0)
        settled = _ticks(tick_file)
        time.sleep(0.5)
        assert _ticks(tick_file) == settled
        assert elapsed < 3.0 + core._CONTAINED_REAP_SECONDS + 5.0
    finally:
        _stop(grandchild)


def test_control_plain_subprocess_run_leaves_the_grandchild_running(tmp_path: Path) -> None:
    """The defect the helper removes, measured with the same probe."""

    command, pid_file, tick_file = _command(tmp_path, "devnull")

    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run(command, cwd=str(tmp_path), capture_output=True, text=True, timeout=3.0)

    _wait_for(pid_file)
    grandchild = int(pid_file.read_text())
    try:
        assert _alive(grandchild)
        before = _ticks(tick_file)
        time.sleep(0.5)
        assert _ticks(tick_file) > before
    finally:
        _stop(grandchild)


def test_a_contained_run_that_finishes_returns_what_subprocess_run_would(
    core, tmp_path: Path
) -> None:
    completed = core._run_contained(
        [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"],
        cwd=str(tmp_path),
        env=dict(os.environ),
        timeout=30.0,
    )

    assert (completed.returncode, completed.stdout.strip(), completed.stderr.strip()) == (3, "out", "err")


def test_a_resample_timeout_is_a_refusal_naming_the_limit(core, monkeypatch, tmp_path: Path) -> None:
    def too_slow(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(core, "_run_contained", too_slow)
    monkeypatch.setattr(core, "_repo_root", lambda _options: tmp_path)
    monkeypatch.setattr(core, "_python_for_resampler", lambda _root, _options: Path(sys.executable))

    with pytest.raises(core.WgLinkError, match="exceeded 7 seconds"):
        core._resample_payload(tmp_path / "b.wglink", {}, {"timeout_seconds": 7}, bundle=None)


_FOREIGN = "import time\ntime.sleep(120)\n"


def test_a_contained_timeout_leaves_every_process_it_did_not_start(core, tmp_path: Path) -> None:
    """Stand-ins for Fusion's world: one process in the caller's own group (as
    Fusion's other children are) and one in a session of its own."""

    same_group = subprocess.Popen([sys.executable, "-c", _FOREIGN])
    own_session = subprocess.Popen(
        [sys.executable, "-c", _FOREIGN],
        **({"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}),
    )
    command, pid_file, _tick_file = _command(tmp_path, "devnull")
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            core._run_contained(command, cwd=str(tmp_path), env=dict(os.environ), timeout=3.0)
        grandchild = int(pid_file.read_text())
        assert _gone_within(grandchild, 10.0)
        assert same_group.poll() is None and _alive(same_group.pid)
        assert own_session.poll() is None and _alive(own_session.pid)
        assert _alive(os.getpid())
    finally:
        for process in (same_group, own_session):
            process.kill()
            process.wait(timeout=30)


# -- structural: the add-in signals nothing it did not start ----------------------


#: Calls that end, suspend or signal a process, by the name they are called as.
_PROCESS_KILLERS = frozenset({
    "kill", "killpg", "terminate", "send_signal", "pthread_kill", "abort",
    "TerminateProcess", "TerminateJobObject", "OpenProcess",
    "NtSuspendProcess", "DebugActiveProcess", "GenerateConsoleCtrlEvent",
})
#: Process launches whose command could itself be a kill (``taskkill``, ``kill``).
_LAUNCHERS = frozenset({"system", "popen", "Popen", "run", "call", "check_call", "check_output"})
_KILL_COMMANDS = ("taskkill", "pkill", "killall", "kill ")

#: Every place the add-in may do any of the above: the containment of the one
#: tree ``_run_contained`` starts. (module file, enclosing function)
_ALLOWED = {
    ("wglink_core.py", "_kill_started_tree"),
    ("wglink_core.py", "terminate"),  # _WindowsJob.terminate: the job it created
    ("wglink_core.py", "_confine_to_windows_job"),  # OpenProcess on the child it started
}


def _called_name(node: ast.Call) -> str | None:
    function = node.func
    if isinstance(function, ast.Attribute):
        return function.attr
    if isinstance(function, ast.Name):
        return function.id
    return None


def _kill_sites(source: str, filename: str) -> list[tuple[str, str, int, str]]:
    """``(file, enclosing function, line, name)`` for every process kill in ``source``."""

    tree = ast.parse(source, filename=filename)
    sites: list[tuple[str, str, int, str]] = []

    def visit(node: ast.AST, function: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        if isinstance(node, ast.Call):
            name = _called_name(node)
            if name in _PROCESS_KILLERS:
                sites.append((filename, function, node.lineno, name))
            elif name in _LAUNCHERS:
                text = ast.unparse(node).lower()
                if any(word in text for word in _KILL_COMMANDS):
                    sites.append((filename, function, node.lineno, f"{name}:kill-command"))
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, "<module>")
    return sites


def _addin_sources() -> list[Path]:
    return sorted(path for path in ADDINS.rglob("*.py") if "__pycache__" not in path.parts)


def test_control_the_scan_finds_a_kill_when_there_is_one() -> None:
    source = textwrap.dedent(
        """
        import os, signal, subprocess
        def stop_fusion(pid):
            os.kill(pid, signal.SIGKILL)
        def stop_by_name():
            subprocess.run(["taskkill", "/F", "/IM", "Fusion360.exe"])
        """
    )

    sites = _kill_sites(source, "synthetic.py")

    assert [(function, name) for _file, function, _line, name in sites] == [
        ("stop_fusion", "kill"),
        ("stop_by_name", "run:kill-command"),
    ]


def test_the_add_in_kills_nothing_but_the_tree_it_started() -> None:
    sources = _addin_sources()
    assert len(sources) >= 10, sources  # the scan is looking at the add-ins

    found = {
        (Path(file).name, function)
        for path in sources
        for file, function, _line, _name in _kill_sites(path.read_text(encoding="utf-8"), str(path))
    }

    assert found == _ALLOWED


def test_the_tree_kill_is_reached_only_with_the_child_the_add_in_just_started() -> None:
    """``_kill_started_tree`` trusts its argument, so its callers are the proof:
    exactly one, ``_run_contained``, passing the ``Popen`` it created itself."""

    tree = ast.parse(CORE.read_text(encoding="utf-8"))
    callers: list[tuple[str, str]] = []
    created: dict[str, str] = {}
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef):
            continue
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Call)
                and ast.unparse(node.value.func) == "subprocess.Popen"
            ):
                created[function.name] = ast.unparse(node.targets[0])
            if isinstance(node, ast.Call) and _called_name(node) == "_kill_started_tree":
                callers.append((function.name, ast.unparse(node.args[0])))

    assert callers == [("_run_contained", "process")]
    assert created.get("_run_contained") == "process"
