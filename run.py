#!/usr/bin/env python3
"""Launcher for AuditAI - the LLM-assisted IT audit console.

Usage
-----
Start
    python run.py              # start the audit console (same as "ui") and open a browser
    python run.py ui           # the Streamlit console alone - a complete application
    python run.py all          # console + FastAPI backend together
    python run.py api          # FastAPI only, with interactive docs at /docs

Maintenance
    python run.py init         # create the database and seed the control library
    python run.py seed-demo    # + create the demo project and load synthetic evidence
    python run.py reset        # DESTRUCTIVE: drop all tables, then re-initialise

Research
    python run.py evaluate     # run experiments A/B/C over the synthetic datasets

The console talks to the shared service layer in-process, so ``ui`` alone is a complete,
working application and is the default. The API exists for programmatic access and
because the research design calls for a documented backend.

Local safety defaults: the console binds 127.0.0.1 (a tool holding audit evidence must
not become LAN-reachable by accident) and the browser is opened by this launcher, not by
Streamlit, because ``.streamlit/config.toml`` runs headless for containers. Set
``NO_BROWSER=1`` to suppress the browser; deployments start the server through
``scripts/start_ui.sh`` and pass their own address and port.
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import List, Optional

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

#: Prefer the project virtualenv so `python run.py` works even when invoked with the
#: system interpreter.
VENV_PYTHON = BASE_DIR / ".venv" / "bin" / "python"
PYTHON = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable

#: The console only ever listens on loopback when started from here.
UI_BIND_ADDRESS = "127.0.0.1"

#: Delay before the browser opens - long enough for Streamlit to bind on a laptop,
#: short enough that nobody wonders whether anything happened.
BROWSER_OPEN_DELAY_SECONDS = 2.5

#: Exit code for "the environment is not ready" (distinct from a crash inside the app).
EXIT_PREFLIGHT = 2

#: Directories on disk that ``reset`` leaves untouched. It drops database tables only.
RESET_KEEPS = ("data/uploads", "data/reports", "data/synthetic", "data/evaluation", "data/controls")


def _fmt(value: Optional[float]) -> str:
    """Render a metric, distinguishing an undefined ratio from a measured zero."""
    return "n/a" if value is None else f"{value:.3f}"


def _banner(text: str) -> None:
    print(f"\n\033[96m{'=' * 78}\n  {text}\n{'=' * 78}\033[0m", flush=True)


def _settings():
    from app.config import get_settings

    return get_settings()


def _ui_url(settings) -> str:
    return f"http://{UI_BIND_ADDRESS}:{settings.streamlit_port}"


def _is_mock_provider(settings) -> bool:
    """Mirror the factory's alias table without importing the provider classes."""
    try:
        from app.llm.factory import MOCK, normalise_provider_name

        return normalise_provider_name(settings.llm_provider) == MOCK
    except Exception:  # pragma: no cover - the banner must never stop the launcher
        return settings.llm_provider == "mock"


# ------------------------------------------------------------------ preflight
def _streamlit_importable() -> bool:
    """Check with the interpreter that will actually run Streamlit, not this one."""
    result = subprocess.run(
        [PYTHON, "-c", "import streamlit"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(BASE_DIR),
    )
    return result.returncode == 0


def _port_in_use(port: int, host: str = UI_BIND_ADDRESS) -> bool:
    """True when something already listens on ``host:port``.

    A connect attempt rather than a bind: binding would fail for unrelated reasons
    (permissions, a lingering TIME_WAIT socket) and report a false "busy".
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((host, int(port))) == 0


def _preflight(settings) -> int:
    """Return 0 when the console can start, otherwise print the exact fix and return 2."""
    if not _streamlit_importable():
        print(
            "Streamlit is not installed in the interpreter that runs the console.\n"
            f"Fix: {PYTHON} -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        return EXIT_PREFLIGHT
    if _port_in_use(settings.streamlit_port):
        print(
            f"Port {settings.streamlit_port} is already in use (another console running?) "
            "- set STREAMLIT_PORT in .env or stop the other process",
            file=sys.stderr,
        )
        return EXIT_PREFLIGHT
    return 0


def _open_browser_later(url: str, delay: float = BROWSER_OPEN_DELAY_SECONDS) -> Optional[threading.Thread]:
    """Open ``url`` after ``delay`` seconds on a daemon thread, unless NO_BROWSER=1.

    Streamlit runs headless (see .streamlit/config.toml) so it will not open a browser
    itself. The thread is a daemon so it never keeps the launcher alive after Ctrl-C.
    """
    if os.environ.get("NO_BROWSER", "").strip().lower() in {"1", "true", "yes"}:
        return None

    def _open() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:  # pragma: no cover - a missing browser is not an error
            pass

    thread = threading.Thread(target=_open, name="open-browser", daemon=True)
    thread.start()
    return thread


def _print_start_notes(settings) -> None:
    print(f"  Console : {_ui_url(settings)}")
    if _is_mock_provider(settings):
        print("  Demo mode - offline rule-based assistant, no API key needed")
    else:
        print(f"  Provider: {settings.llm_provider} ({settings.active_llm_model})")
    print("  First time? Press 'Try the demo audit' on the home page.")
    if os.environ.get("NO_BROWSER"):
        print("  NO_BROWSER is set - open the URL above yourself.")
    print("  Press Ctrl-C to stop.\n", flush=True)


# ---------------------------------------------------------------- maintenance
def cmd_init(_args: argparse.Namespace) -> int:
    from app.database.base import init_db, session_scope
    from app.database.seed import seed_controls
    from app.audit.service import list_controls

    _banner("Initialising database")
    init_db()
    with session_scope() as session:
        added = seed_controls(session)
        # seed_controls returns how many rows it *wrote*; on a second run that is zero,
        # which reads like an empty library. Report the library total as well.
        total = len(list_controls(session, active_only=False))
    print(f"Database ready. Control library contains {total} controls ({added} newly seeded).")
    return 0


def cmd_seed_demo(_args: argparse.Namespace) -> int:
    from app.database.base import init_db, session_scope
    from app.database.seed import load_demo_evidence

    _banner("Seeding demo project and synthetic evidence")
    init_db()
    with session_scope() as session:
        result = load_demo_evidence(
            session,
            progress=lambda done, total, name: print(f"  [{done}/{total}] {name}"),
        )
    ingested = result.get("ingested") or []
    skipped = result.get("skipped") or []
    failures = result.get("failures") or []
    print(f"\nDemo project ready: {result.get('project_name', '(unnamed)')}")
    print(
        f"  controls in scope : {len(result.get('scoped_control_refs') or [])} "
        f"of {result.get('controls_total', 0)} in the library"
    )
    print(f"  evidence ingested : {len(ingested)} file(s)")
    if skipped:
        print(f"  already present   : {len(skipped)} file(s) skipped")
    for failure in failures:
        print(f"  FAILED            : {failure.get('filename')} - {failure.get('error')}")
    if result.get("note"):
        print(f"\n{result['note']}")
    return 1 if failures and not ingested else 0


def cmd_reset(_args: argparse.Namespace) -> int:
    from app.database.base import drop_all, init_db, session_scope
    from app.database.seed import seed_controls

    print("This DELETES every database row: projects, evidence records, assessments and reviews.")
    print("Files on disk are kept: " + ", ".join(RESET_KEEPS) + ".")
    reply = input("Type 'yes' to confirm: ")
    if reply.strip().lower() != "yes":
        print("Aborted. Nothing was deleted.")
        return 1
    _banner("Resetting database")
    drop_all()
    init_db()
    with session_scope() as session:
        seed_controls(session)
    print("Database reset and control library re-seeded. Kept on disk: " + ", ".join(RESET_KEEPS) + ".")
    print("Run 'python run.py seed-demo' to recreate the demo project.")
    return 0


# ------------------------------------------------------------------- research
def cmd_evaluate(args: argparse.Namespace) -> int:
    from app.database.base import init_db, session_scope
    from app.database.seed import bootstrap
    from app.evaluation.runner import run_all_experiments
    from app.schemas.enums import ExperimentMode

    _banner("Running research experiments A / B / C")
    init_db()
    modes = [ExperimentMode(m) for m in args.modes] if args.modes else list(ExperimentMode)
    with session_scope() as session:
        # A fresh database has no control library; the experiments score against it.
        bootstrap(session)
        runs = run_all_experiments(session, modes=modes)
    for run in runs:
        metrics = run.metrics or {}
        # Classification figures live under the "classification" block; "grounded_accuracy"
        # is the stricter figure that only credits conclusions backed by a verified citation.
        classification = metrics.get("classification") or {}
        grounded = metrics.get("grounded_accuracy") or {}
        print(
            f"  {run.experiment_mode:<16} "
            f"accuracy={_fmt(classification.get('accuracy'))}  "
            f"grounded={_fmt(grounded.get('grounded_accuracy'))}  "
            f"macro_f1={_fmt(classification.get('macro_f1'))}  "
            f"n={metrics.get('n', 0)}"
        )
    print(
        "\n'grounded' credits a prediction only when it is correct AND supported by at least "
        "one verified citation; a large gap from 'accuracy' means right answers nobody can check."
    )
    print("\nOpen the Evaluation page in the UI for the full comparison.")
    return 0


# ---------------------------------------------------------------------- start
def _api_command(settings) -> List[str]:
    return [
        PYTHON, "-m", "uvicorn", "app.api.main:app",
        "--host", settings.api_host,
        "--port", str(settings.api_port),
    ]


def _ui_command(settings) -> List[str]:
    return [
        PYTHON, "-m", "streamlit", "run", str(BASE_DIR / "app" / "frontend" / "streamlit_app.py"),
        "--server.port", str(settings.streamlit_port),
        "--server.address", UI_BIND_ADDRESS,
        "--server.showEmailPrompt", "false",
        "--browser.gatherUsageStats", "false",
    ]


def cmd_api(_args: argparse.Namespace) -> int:
    settings = _settings()
    _banner(f"FastAPI backend -> http://{settings.api_host}:{settings.api_port}/docs")
    return subprocess.call(_api_command(settings), cwd=str(BASE_DIR))


def cmd_ui(_args: argparse.Namespace) -> int:
    settings = _settings()
    failed = _preflight(settings)
    if failed:
        return failed
    _banner("AuditAI console")
    _print_start_notes(settings)
    _open_browser_later(_ui_url(settings))
    return subprocess.call(_ui_command(settings), cwd=str(BASE_DIR))


def cmd_all(_args: argparse.Namespace) -> int:
    """Run both services, forwarding termination so Ctrl-C stops the pair cleanly."""
    settings = _settings()
    failed = _preflight(settings)
    if failed:
        return failed
    env = dict(os.environ)
    env.setdefault("USE_API", "false")  # the UI stays self-sufficient if the API dies

    _banner("AuditAI console + FastAPI backend")
    print(f"  API     : http://{settings.api_host}:{settings.api_port}/docs")
    _print_start_notes(settings)

    procs: List[subprocess.Popen] = []
    try:
        procs.append(subprocess.Popen(_api_command(settings), cwd=str(BASE_DIR), env=env))
        time.sleep(2.0)  # let uvicorn bind before Streamlit probes it
        procs.append(subprocess.Popen(_ui_command(settings), cwd=str(BASE_DIR), env=env))
        _open_browser_later(_ui_url(settings))
        while True:
            for proc in procs:
                code: Optional[int] = proc.poll()
                if code is not None:
                    print(f"\nProcess {proc.args[2] if len(proc.args) > 2 else proc.pid} exited with {code}.")
                    return code or 0
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nShutting down...")
        return 0
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        for proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                proc.kill()


# ----------------------------------------------------------------------- main
#: (group, name, help, handler). The order here is the order shown in --help.
COMMANDS = (
    ("Start", "ui", "start the audit console and open a browser (default)", cmd_ui),
    ("Start", "all", "start the console and the FastAPI backend together", cmd_all),
    ("Start", "api", "start the FastAPI backend only", cmd_api),
    ("Maintenance", "init", "create the database and seed the control library", cmd_init),
    ("Maintenance", "seed-demo", "create the demo project and load synthetic evidence", cmd_seed_demo),
    ("Maintenance", "reset", "DESTRUCTIVE: drop and recreate all tables (files on disk are kept)", cmd_reset),
    ("Research", "evaluate", "run experiments A/B/C over the synthetic datasets", cmd_evaluate),
)


def _grouped_help() -> str:
    lines: List[str] = []
    current = None
    for group, name, help_text, _func in COMMANDS:
        if group != current:
            lines.append(f"\n{group}:")
            current = group
        lines.append(f"  {name:<12} {help_text}")
    lines.append(
        "\nEnvironment: NO_BROWSER=1 stops the launcher opening a browser; STREAMLIT_PORT "
        "changes the console port; LLM_PROVIDER=mock (default) needs no API key."
    )
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="AuditAI - LLM-assisted IT audit console. With no command, starts the console.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_grouped_help(),
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    for _group, name, help_text, func in COMMANDS:
        # No per-command ``help`` on purpose: argparse would print a flat list, and on
        # Python 3.9 ``help=SUPPRESS`` prints the literal token. The grouped list in the
        # epilog is the single command listing.
        command = sub.add_parser(name, description=help_text)
        if name == "evaluate":
            command.add_argument(
                "--modes", nargs="*", default=None, help="subset of A_RAW_LLM B_RAG C_RAG_WORKFLOW"
            )
        command.set_defaults(func=func)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args.func = cmd_ui
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
