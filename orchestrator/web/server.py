"""
Thin web UI for the orchestrator.

Single-user, mobile-optimized. Access locally or over Tailscale.
Run with: orchestrator web [--host 0.0.0.0] [--port 7999]

Architecture:
  - FastAPI serves HTML (Jinja2 templates) and handles form POST actions.
  - OrchestratorRunner runs in a background daemon thread.
  - The thread blocks at review_fn() waiting on a threading.Event.
  - Web endpoints set the event to unblock the runner.
  - Status updates use <meta http-equiv="refresh"> polling (no WebSocket needed).
"""

from __future__ import annotations

import asyncio
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import yaml
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from orchestrator.executor.cli_executor import make_executor
from orchestrator.jobs.models import RunState, Status
from orchestrator.jobs.runner import OrchestratorRunner
from orchestrator.memory.manager import MemoryManager
from orchestrator.planner.openai_planner import OpenAIPlanner
from orchestrator.storage.store import RunStore
from orchestrator.utils.config import Config


# ---------------------------------------------------------------------------
# Session state (single-user in-process)
# ---------------------------------------------------------------------------

@dataclass
class WebSession:
    # Run identity
    run_id: Optional[str] = None
    repo_path: Optional[str] = None
    task: Optional[str] = None

    # Phase: idle | planning | awaiting_review | executing | validating | done | stopped | error
    status: str = "idle"
    current_iteration: int = 0

    # Populated when status == "awaiting_review"
    current_plan: Optional[dict] = None

    # Q&A during review (out-of-band planner calls)
    qa_history: list = field(default_factory=list)

    # Runner ↔ web communication
    review_event: threading.Event = field(default_factory=threading.Event)
    review_decision: Optional[dict] = None

    # Handles to planner + store for in-request use (questions, log reading)
    _planner: Optional[OpenAIPlanner] = None
    _store: Optional[RunStore] = None

    # Error detail
    error: Optional[str] = None

    # Background thread handle
    _thread: Optional[threading.Thread] = None

    def is_busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def reset(self) -> None:
        self.run_id = None
        self.repo_path = None
        self.task = None
        self.status = "idle"
        self.current_iteration = 0
        self.current_plan = None
        self.qa_history = []
        self.review_event = threading.Event()
        self.review_decision = None
        self._planner = None
        self._store = None
        self.error = None
        self._thread = None


# Module-level singleton — one active session at a time.
session = WebSession()


# ---------------------------------------------------------------------------
# FastAPI app + templates
# ---------------------------------------------------------------------------

app = FastAPI(title="Orchestrator", docs_url=None, redoc_url=None)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Runner thread helpers
# ---------------------------------------------------------------------------

def _make_review_fn(sess: WebSession) -> Callable:
    """Return a review function that blocks until the web UI responds."""
    def review_fn(plan_dict: dict, iteration: int, ask_planner: Callable) -> dict:
        sess.current_plan = plan_dict
        sess.current_iteration = iteration
        sess.qa_history = []
        sess.status = "awaiting_review"
        sess.review_event.clear()
        # Block until approve/stop posted (2-hour timeout → auto-stop)
        sess.review_event.wait(timeout=7200)
        sess.review_event.clear()
        return sess.review_decision or {"decision": "stopped", "prompt": ""}
    return review_fn


def _make_status_fn(sess: WebSession) -> Callable:
    """Return a status callback for the runner."""
    def status_fn(status: str, iteration: int = 0) -> None:
        sess.status = status
        sess.current_iteration = iteration
    return status_fn


def _run_in_thread(sess: WebSession, runner: OrchestratorRunner) -> None:
    try:
        runner.run()
        if sess.status not in ("stopped", "error"):
            sess.status = "done"
    except Exception as exc:
        sess.status = "error"
        sess.error = f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}"
        # Unblock any waiting review_fn so the thread can exit cleanly.
        sess.review_event.set()


def _launch_runner(sess: WebSession, repo_path: str, task: str, cfg: Config) -> None:
    """Create runner + store, write initial state, start background thread."""
    planner = OpenAIPlanner(api_key=cfg.openai_api_key, model=cfg.openai_model)
    executor = make_executor(cfg.executor_mode, cfg.claude_cli_path)

    store = RunStore.create(cfg.log_dir, repo_path)
    store.write_task(task)

    run_state = RunState(
        run_id=store.run_id,
        repo_path=str(Path(repo_path).resolve()),
        status=Status.QUEUED,
    )
    store.write_state(run_state.to_dict())

    runner = OrchestratorRunner(
        store=store,
        planner=planner,
        executor=executor,
        config=cfg,
        yes=True,                          # skip all Confirm prompts
        review_fn=_make_review_fn(sess),
        status_fn=_make_status_fn(sess),
    )

    sess.run_id = store.run_id
    sess.repo_path = repo_path
    sess.task = task
    sess._planner = planner
    sess._store = store
    sess.status = "planning"

    sess._thread = threading.Thread(
        target=_run_in_thread,
        args=(sess, runner),
        daemon=True,
        name="orchestrator-runner",
    )
    sess._thread.start()


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _recent_runs(cfg: Config, limit: int = 8) -> list[dict]:
    runs_dir = Path(cfg.log_dir).expanduser()
    if not runs_dir.exists():
        return []
    out = []
    dirs = sorted(runs_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    for d in dirs[:limit * 2]:  # over-fetch in case some dirs lack state.yaml
        state_file = d / "state.yaml"
        if state_file.exists():
            try:
                data = yaml.safe_load(state_file.read_text())
                out.append(data)
                if len(out) >= limit:
                    break
            except Exception:
                pass
    return out


def _executor_log_tail(sess: WebSession, n_lines: int = 80) -> str:
    if not sess._store:
        return ""
    log_path = sess._store.iteration_dir(sess.current_iteration) / "executor_stdout.log"
    if not log_path.exists():
        return ""
    text = log_path.read_text(errors="replace")
    lines = text.splitlines()
    return "\n".join(lines[-n_lines:])


def _memory_ctx(sess: WebSession) -> Optional[dict]:
    if not sess.repo_path:
        return None
    mem = MemoryManager(sess.repo_path)
    if not mem.working_memory_path.exists():
        return None
    try:
        sat = mem.saturation_status()
        sat["snippet"] = mem.load_working_memory()[-600:]
        return sat
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, error: str = ""):
    cfg = Config.load()
    runs = _recent_runs(cfg)
    return templates.TemplateResponse("index.html", {
        "request": request,
        "session": session,
        "runs": runs,
        "error": error,
    })


@app.post("/start")
async def start(
    repo_path: str = Form(...),
    task: str = Form(...),
):
    if session.is_busy():
        return RedirectResponse("/run", status_code=303)

    repo = Path(repo_path.strip()).expanduser().resolve()
    if not repo.exists():
        return RedirectResponse(f"/?error=Repo+not+found:+{repo_path}", status_code=303)

    cfg = Config.load()
    if not cfg.openai_api_key:
        return RedirectResponse("/?error=OPENAI_API_KEY+not+set+in+.env", status_code=303)

    session.reset()
    _launch_runner(session, str(repo), task.strip(), cfg)
    return RedirectResponse("/run", status_code=303)


@app.get("/run", response_class=HTMLResponse)
async def run_page(request: Request):
    if session.status == "idle":
        return RedirectResponse("/", status_code=303)
    log_tail = _executor_log_tail(session)
    mem = _memory_ctx(session)
    return templates.TemplateResponse("run.html", {
        "request": request,
        "session": session,
        "log_tail": log_tail,
        "memory": mem,
    })


@app.post("/approve")
async def approve(prompt: Optional[str] = Form(default=None)):
    if session.status != "awaiting_review":
        return RedirectResponse("/run", status_code=303)
    final_prompt = (prompt or "").strip() or (
        (session.current_plan or {}).get("proposed_prompt", "")
    )
    session.review_decision = {"decision": "approved", "prompt": final_prompt}
    session.status = "executing"
    session.review_event.set()
    return RedirectResponse("/run", status_code=303)


@app.post("/stop")
async def stop():
    session.review_decision = {"decision": "stopped", "prompt": ""}
    session.status = "stopped"
    session.review_event.set()
    return RedirectResponse("/run", status_code=303)


@app.post("/question")
async def question(q: str = Form(...)):
    if not session._planner or not session.current_plan:
        return RedirectResponse("/run", status_code=303)
    ctx = f"Task:\n{session.task}\n\nPlan:\n{session.current_plan}"
    try:
        # Planner.ask() is a blocking LLM call — run off the event loop.
        answer = await asyncio.get_event_loop().run_in_executor(
            None, session._planner.ask, q, ctx
        )
    except Exception as exc:
        answer = f"[Error asking planner: {exc}]"
    session.qa_history.append({"q": q, "a": answer})
    return RedirectResponse("/run", status_code=303)


@app.post("/new")
async def new_run():
    if session.is_busy():
        session.review_decision = {"decision": "stopped", "prompt": ""}
        session.review_event.set()
    session.reset()
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------------------
# Lightweight JSON API (for optional JS polling / status checks)
# ---------------------------------------------------------------------------

@app.get("/api/status")
async def api_status():
    return {
        "status": session.status,
        "run_id": session.run_id,
        "iteration": session.current_iteration,
        "busy": session.is_busy(),
    }


@app.get("/api/logs")
async def api_logs():
    return {"log": _executor_log_tail(session, n_lines=120)}


@app.get("/api/memory")
async def api_memory():
    return _memory_ctx(session) or {}
