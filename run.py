#!/usr/bin/env python3
"""Launcher for the LLM-Assisted IT Audit Risk and Control Assessment System.

Usage
-----
    python run.py              # start the FastAPI backend and the Streamlit UI
    python run.py ui           # Streamlit only (the UI works standalone, in-process)
    python run.py api          # FastAPI only, with interactive docs at /docs
    python run.py init         # create the database and seed the control library
    python run.py seed-demo    # + create the demo project and synthetic evidence
    python run.py evaluate     # run experiments A/B/C over the synthetic datasets
    python run.py reset        # DESTRUCTIVE: drop all tables, then re-initialise

The Streamlit interface talks to the shared service layer in-process, so ``ui`` alone
is a complete, working application. The API is provided for programmatic access and
because the research design calls for a documented backend.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

#: Prefer the project virtualenv so `python run.py` works even when invoked with the
#: system interpreter.
VENV_PYTHON = BASE_DIR / ".venv" / "bin" / "python"
PYTHON = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable


def _fmt(value: Optional[float]) -> str:
    """Render a metric, distinguishing an undefined ratio from a measured zero."""
    return "n/a" if value is None else f"{value:.3f}"


def _banner(text: str) -> None:
    print(f"\n\033[96m{'=' * 78}\n  {text}\n{'=' * 78}\033[0m", flush=True)


def _settings():
    from app.config import get_settings

    return get_settings()


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
    from app.database.seed import bootstrap

    _banner("Seeding demo project and synthetic evidence")
    init_db()
    with session_scope() as session:
        project = bootstrap(session)
    print(f"Demo project ready: {getattr(project, 'name', project)}")
    return 0


def cmd_reset(_args: argparse.Namespace) -> int:
    from app.database.base import drop_all, init_db, session_scope
    from app.database.seed import seed_controls

    reply = input("This DELETES all projects, evidence, assessments and reviews. Type 'yes' to confirm: ")
    if reply.strip().lower() != "yes":
        print("Aborted. Nothing was deleted.")
        return 1
    _banner("Resetting database")
    drop_all()
    init_db()
    with session_scope() as session:
        seed_controls(session)
    print("Database reset and re-seeded.")
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    from app.database.base import init_db, session_scope
    from app.evaluation.runner import run_all_experiments
    from app.schemas.enums import ExperimentMode

    _banner("Running research experiments A / B / C")
    init_db()
    modes = [ExperimentMode(m) for m in args.modes] if args.modes else list(ExperimentMode)
    with session_scope() as session:
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
        "--server.headless", "false",
        "--browser.gatherUsageStats", "false",
    ]


def cmd_api(_args: argparse.Namespace) -> int:
    settings = _settings()
    _banner(f"FastAPI backend -> http://{settings.api_host}:{settings.api_port}/docs")
    return subprocess.call(_api_command(settings), cwd=str(BASE_DIR))


def cmd_ui(_args: argparse.Namespace) -> int:
    settings = _settings()
    _banner(f"Streamlit interface -> http://localhost:{settings.streamlit_port}")
    return subprocess.call(_ui_command(settings), cwd=str(BASE_DIR))


def cmd_all(_args: argparse.Namespace) -> int:
    """Run both services, forwarding termination so Ctrl-C stops the pair cleanly."""
    settings = _settings()
    env = dict(os.environ)
    env.setdefault("USE_API", "false")  # the UI stays self-sufficient if the API dies

    _banner("Starting FastAPI backend and Streamlit interface")
    print(f"  API : http://{settings.api_host}:{settings.api_port}/docs")
    print(f"  UI  : http://localhost:{settings.streamlit_port}")
    print("  Provider: " + settings.llm_provider + " | Press Ctrl-C to stop both.\n")

    procs: List[subprocess.Popen] = []
    try:
        procs.append(subprocess.Popen(_api_command(settings), cwd=str(BASE_DIR), env=env))
        time.sleep(2.0)  # let uvicorn bind before Streamlit probes it
        procs.append(subprocess.Popen(_ui_command(settings), cwd=str(BASE_DIR), env=env))
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


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="LLM-Assisted IT Audit Risk and Control Assessment System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("all", help="run the API and the UI together (default)").set_defaults(func=cmd_all)
    sub.add_parser("api", help="run the FastAPI backend only").set_defaults(func=cmd_api)
    sub.add_parser("ui", help="run the Streamlit interface only").set_defaults(func=cmd_ui)
    sub.add_parser("init", help="create the database and seed the control library").set_defaults(func=cmd_init)
    sub.add_parser("seed-demo", help="seed the demo project and synthetic evidence").set_defaults(func=cmd_seed_demo)
    sub.add_parser("reset", help="DESTRUCTIVE: drop and recreate all tables").set_defaults(func=cmd_reset)

    evaluate = sub.add_parser("evaluate", help="run the research experiments over the synthetic datasets")
    evaluate.add_argument("--modes", nargs="*", default=None, help="subset of A_RAW_LLM B_RAG C_RAG_WORKFLOW")
    evaluate.set_defaults(func=cmd_evaluate)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args.func = cmd_all
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
