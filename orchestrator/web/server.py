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
import json
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import jinja2
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

    # Runner ↔ web communication (plan review)
    review_event: threading.Event = field(default_factory=threading.Event)
    review_decision: Optional[dict] = None

    # Runner ↔ web communication (post-iteration pause)
    post_iter_event: threading.Event = field(default_factory=threading.Event)
    post_iter_decision: Optional[str] = None   # "continue" | "stopped"
    last_iter_summary: Optional[dict] = None   # snapshot of just-completed iteration

    # Handles to planner + store for in-request use (questions, log reading)
    _planner: Optional[OpenAIPlanner] = None
    _store: Optional[RunStore] = None

    # Error detail
    error: Optional[str] = None
    active_objective: Optional[str] = None
    queued_next_objective: Optional[str] = None

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
        self.post_iter_event = threading.Event()
        self.post_iter_decision = None
        self.last_iter_summary = None
        self._planner = None
        self._store = None
        self.error = None
        self.active_objective = None
        self.queued_next_objective = None
        self._thread = None


# Module-level singleton — one active session at a time.
session = WebSession()

# Statuses that mean a run has permanently finished (no resume possible).
_TERMINAL_STATUSES = {"succeeded", "stopped", "timed_out", "failed"}


def _restore_session_from_disk() -> None:
    """At module load, restore the most recent non-terminal run from disk.

    Sets session.status = "interrupted" so the UI can prompt the user to
    Resume or Abandon before any new planner call is made.
    """
    try:
        cfg = Config.load()
        store = RunStore.latest(cfg.log_dir)
        if store is None:
            return
        state = store.read_state()
        if not state:
            return
        if state.get("status") in _TERMINAL_STATUSES:
            return

        session.run_id = state.get("run_id") or store.run_id
        session.repo_path = state.get("repo_path", "")
        session.task = store.read_task()
        session.current_iteration = state.get("current_iteration", 0)
        session._store = store
        session.active_objective = state.get("active_objective") or session.task or ""
        session.queued_next_objective = state.get("queued_next_objective") or ""
        session.status = "interrupted"

        # Restore current plan if the interrupted iteration was awaiting review.
        itr_n = session.current_iteration
        itr_state = store.read_iteration_state(itr_n)
        if itr_state.get("status") == "awaiting_review":
            session.current_plan = {
                "objective": itr_state.get("objective", ""),
                "proposed_prompt": itr_state.get("proposed_prompt", ""),
                "validation_commands": itr_state.get("validation_commands", []),
                "risks": itr_state.get("risks", ""),
                "next_step_framing": itr_state.get("next_step_framing", ""),
            }

        # Restore last_iter_summary from the previous completed iteration.
        if itr_n > 0:
            prev = store.read_iteration_state(itr_n - 1)
            if prev.get("status") == "succeeded":
                session.last_iter_summary = {
                    "number": prev.get("number", itr_n - 1),
                    "objective": prev.get("objective", ""),
                    "validation_results": prev.get("validation_results", []),
                    "executor_exit_code": prev.get("executor_exit_code"),
                }
    except Exception:
        pass  # Never crash at startup due to restore failure


_restore_session_from_disk()


# ---------------------------------------------------------------------------
# FastAPI app + templates
# ---------------------------------------------------------------------------

app = FastAPI(title="Orchestrator", docs_url=None, redoc_url=None)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
# Construct Jinja2 env manually with cache_size=0 to avoid Python 3.14
# hashability issue in LRUCache (tuple key containing weakref becomes unhashable
# when the loader carries state that Python 3.14 refuses to hash).
_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=jinja2.select_autoescape(["html"]),
    cache_size=0,
)
templates = Jinja2Templates(env=_jinja_env)


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


def _make_post_iter_fn(sess: WebSession) -> Callable:
    """Block after each completed iteration until the user clicks Continue or Stop."""
    def post_iter_fn(itr_state, run_state) -> str:
        sess.last_iter_summary = {
            "number": itr_state.number,
            "objective": itr_state.objective,
            "validation_results": itr_state.validation_results or [],
            "executor_exit_code": itr_state.executor_exit_code,
        }
        sess.qa_history = []
        sess.status = "paused"
        sess.post_iter_event.clear()
        sess.post_iter_event.wait(timeout=7200)
        sess.post_iter_event.clear()
        return sess.post_iter_decision or "stopped"
    return post_iter_fn


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
        active_objective=task.strip(),
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
        post_iter_fn=_make_post_iter_fn(sess),
    )

    sess.run_id = store.run_id
    sess.repo_path = repo_path
    sess.task = task
    sess.active_objective = task.strip()
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


def _resume_run(sess: WebSession) -> None:
    """Resume an interrupted run, reusing its existing store."""
    cfg = Config.load()
    planner = OpenAIPlanner(api_key=cfg.openai_api_key, model=cfg.openai_model)
    executor = make_executor(cfg.executor_mode, cfg.claude_cli_path)

    sess._planner = planner
    sess.status = "planning"
    sess.review_event = threading.Event()
    sess.post_iter_event = threading.Event()

    runner = OrchestratorRunner(
        store=sess._store,
        planner=planner,
        executor=executor,
        config=cfg,
        yes=True,
        review_fn=_make_review_fn(sess),
        status_fn=_make_status_fn(sess),
        post_iter_fn=_make_post_iter_fn(sess),
    )

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

def _write_objective_state(sess: WebSession) -> None:
    """Persist active_objective and queued_next_objective to state.yaml."""
    if not sess._store:
        return
    state = sess._store.read_state()
    state["active_objective"] = sess.active_objective or ""
    state["queued_next_objective"] = sess.queued_next_objective or ""
    sess._store.write_state(state)


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
        sat["snippet"] = mem.load_working_memory()
        return sat
    except Exception:
        return None


def _parse_qa_answer(raw: str):
    """Return a parsed dict if the answer is a plan-shaped JSON, else the raw string.

    The planner's system prompt instructs it to always respond with JSON
    (objective / proposed_prompt / validation_commands / risks / …).
    We detect that and let the template render it nicely; plain-text answers
    fall through unchanged.
    """
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and "objective" in parsed:
            return parsed
    except Exception:
        pass
    return raw


def _tail(text: str, n_lines: int) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-n_lines:])


def _all_iter_details(sess: WebSession) -> list:
    """Return per-iteration output details for completed iterations."""
    if not sess._store:
        return []
    store = sess._store
    details = []
    for n in store.list_iterations():
        itr = store.read_iteration_state(n) or {}
        # Only include iterations that have at least started executing
        if itr.get("status") not in ("queued", "running"):
            out = store.read_executor_output(n)
            details.append({
                "number": n,
                "objective": itr.get("objective", ""),
                "status": itr.get("status", ""),
                "human_decision": itr.get("human_decision", ""),
                "executor_exit_code": itr.get("executor_exit_code"),
                "validation_results": itr.get("validation_results", []),
                "executor_stdout_tail": _tail(out.get("stdout", ""), 80),
                "validation_stdout": out.get("validation_stdout", ""),
                "git_diff": out.get("git_diff", ""),
            })
    return details


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, error: str = ""):
    cfg = Config.load()
    runs = _recent_runs(cfg)
    return templates.TemplateResponse(request, "index.html", {
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
    # Block new runs while an interrupted run awaits explicit user decision.
    if session.status == "interrupted":
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
    iter_details = _all_iter_details(session)
    return templates.TemplateResponse(request, "run.html", {
        "session": session,
        "log_tail": log_tail,
        "memory": mem,
        "iter_details": iter_details,
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
    session.post_iter_decision = "stopped"
    session.status = "stopped"
    session.review_event.set()
    session.post_iter_event.set()
    return RedirectResponse("/run", status_code=303)


@app.post("/continue")
async def continue_run():
    if session.status != "paused":
        return RedirectResponse("/run", status_code=303)
    session.current_plan = None   # prevent stale plan leaking into next iteration's /approve
    session.post_iter_decision = "continue"
    session.status = "planning"   # optimistic — runner will call status_fn immediately
    session.post_iter_event.set()
    return RedirectResponse("/run", status_code=303)


@app.post("/question")
async def question(q: str = Form(...)):
    if not session._planner:
        return RedirectResponse("/run", status_code=303)
    if session.status == "awaiting_review" and session.current_plan:
        ctx = f"Task:\n{session.task}\n\nPlan:\n{session.current_plan}"
    elif session.status == "paused" and session.last_iter_summary:
        s = session.last_iter_summary
        val_lines = "\n".join(
            f"  {'✓' if r.get('exit_code') == 0 else '✗'} {r.get('cmd')} ({r.get('classification')})"
            for r in s["validation_results"]
        )
        ctx = (
            f"Task:\n{session.task}\n\n"
            f"Completed iteration {s['number']}:\n"
            f"Objective: {s['objective']}\n"
            f"Validation:\n{val_lines or '  (none)'}"
        )
    elif session.status == "done":
        working_memory = ""
        if session.repo_path:
            try:
                working_memory = MemoryManager(session.repo_path).load_working_memory()
            except Exception:
                pass
        last = ""
        if session.last_iter_summary:
            s = session.last_iter_summary
            last = f"\nLast iteration objective: {s['objective']}"
        ctx = (
            f"Task:\n{session.task}\n"
            f"{last}\n\n"
            f"Working memory (accumulated project context):\n{working_memory}"
        )
    else:
        return RedirectResponse("/run", status_code=303)
    try:
        # Planner.ask() is a blocking LLM call — run off the event loop.
        answer = await asyncio.get_event_loop().run_in_executor(
            None, session._planner.ask, q, ctx
        )
    except Exception as exc:
        answer = f"[Error asking planner: {exc}]"
    session.qa_history.append({"q": q, "a": _parse_qa_answer(answer)})
    return RedirectResponse("/run", status_code=303)


@app.post("/resume")
async def resume():
    if session.status != "interrupted":
        return RedirectResponse("/run", status_code=303)
    if not session._store:
        return RedirectResponse("/", status_code=303)
    cfg = Config.load()
    if not cfg.openai_api_key:
        return RedirectResponse("/?error=OPENAI_API_KEY+not+set+in+.env", status_code=303)
    _resume_run(session)
    return RedirectResponse("/run", status_code=303)


@app.post("/abandon")
async def abandon():
    if session.status == "interrupted" and session._store:
        # Mark the run as stopped on disk so it won't be restored again.
        state = session._store.read_state()
        state["status"] = "stopped"
        session._store.write_state(state)
    session.reset()
    return RedirectResponse("/", status_code=303)


@app.post("/set-objective")
async def set_objective(objective: str = Form(...)):
    obj = objective.strip()
    if obj:
        session.active_objective = obj
        _write_objective_state(session)
    return RedirectResponse("/run", status_code=303)


@app.post("/queue-next")
async def queue_next(objective: str = Form(...)):
    obj = objective.strip()
    if obj:
        session.queued_next_objective = obj
        _write_objective_state(session)
    return RedirectResponse("/run", status_code=303)


@app.post("/promote-next")
async def promote_next():
    if session.queued_next_objective:
        session.active_objective = session.queued_next_objective
        session.queued_next_objective = ""
        _write_objective_state(session)
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
