"""
main.py
=======

DEPLOYMENT (owner-confirmed, 2026-09-16): this bot runs as a SINGLE Python
process on Railway.com - one process, one interpreter, for every account
and pair across both Binance and OKX. This is a real architectural fact
several parts of this codebase now rely on directly, not just background
context:

  - app/binance_futures.py's shared per-account order-rate-limit tracker
    (_SharedAccountOrderRateTracker) is a plain in-process dict, safe
    ONLY because every pair's client lives in this one process and can
    share memory directly. It would silently stop working (each process
    reverting to its own private, unsynced view) if this project were
    ever split across multiple Railway services/replicas or otherwise
    scaled horizontally - that would need an external shared store (e.g.
    Redis) instead of an in-process dict.
  - The same applies to anything else in this codebase that assumes
    "there is exactly one process holding all state in memory" - the
    manager/okx_manager instance dictionaries, the global STOP ALL/START
    ALL control (global_control.py), and the auth/session store all make
    this same single-process assumption.

If this project's deployment topology ever changes from "one Railway
process" to anything with more than one process/replica running at once,
every one of the above needs to be revisited - flagging this prominently
now, in the app's own entry point, so a future change in hosting doesn't
silently reintroduce bugs this project has already fixed once (the
per-pair-instead-of-per-account rate-limit issue this note originally
accompanied is exactly that kind of bug).
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from app.api import router as api_router
from app.api.okx import router as okx_api_router
from app.manager import manager
from app.okx_manager import okx_manager
from app import singleton_lock
from app import startup_guard
from app import telegram_notifier as tg

STATIC_DIR = Path(__file__).resolve().parent / "static"
_START_TIME = time.time()
# 2026-09-16, owner request: set by the lifespan startup check below - if
# a conflicting instance is detected, trading engines are never started
# but the web server/dashboard still comes up normally, and this is what
# the dashboard actually reads to show a prominent, hard-to-miss banner
# about it (see /healthz and /api/status).
_singleton_conflict_message: str | None = None
_heartbeat_task = None
_claim_task = None
_storage_problem_message: str | None = None

# How the startup lock behaves when another instance's heartbeat is still fresh
# (the normal situation for a few seconds after a Railway redeploy/restart, because
# the old process may have been stopped before it could release the lock):
#   * the web server and dashboard come up anyway,
#   * NO trading starts in this process while the other one looks alive,
#   * the claim is retried every CLAIM_RETRY_SECONDS and succeeds by itself as soon
#     as the old heartbeat goes stale (or is released) - nobody has to restart anything,
#   * if it is still blocked after CONFLICT_ALERT_AFTER_SECONDS, an alert is sent once.
# Trading only ever starts after a successful claim, so two copies still can never
# trade the same accounts at the same time.
CLAIM_RETRY_SECONDS = 5
CONFLICT_ALERT_AFTER_SECONDS = 180


async def _start_trading_engines() -> None:
    await manager.start_all_enabled()
    await okx_manager.start_all_enabled()


async def _claim_when_free() -> None:
    """Background task: keeps trying to claim the single-instance lock while another
    instance looks alive; starts trading the moment the claim succeeds."""
    global _singleton_conflict_message, _heartbeat_task
    waited_since = time.time()
    alerted = False
    while True:
        await asyncio.sleep(CLAIM_RETRY_SECONDS)
        claimed, conflict_message = singleton_lock.check_and_claim_lock(quiet=True)
        if claimed:
            _singleton_conflict_message = None
            _heartbeat_task = asyncio.create_task(singleton_lock.heartbeat_loop())
            logging.getLogger("main").info(
                "Single-instance lock claimed after waiting %.0fs - starting trading engines.",
                time.time() - waited_since)
            try:
                await _start_trading_engines()
            except asyncio.CancelledError:
                raise
            except Exception:   # never let a start-up error vanish silently inside a background task
                logging.getLogger("main").exception("Starting the trading engines after the lock claim failed")
            return
        _singleton_conflict_message = conflict_message
        if not alerted and time.time() - waited_since >= CONFLICT_ALERT_AFTER_SECONDS:
            alerted = True
            try:
                await tg.notify_error(
                    "SYSTEM", "STARTUP",
                    f"Trading has NOT started: another instance of this bot still looks alive after "
                    f"{CONFLICT_ALERT_AFTER_SECONDS}s. This process keeps retrying and will start by "
                    f"itself once the other one is gone. If you did not just redeploy, check Railway "
                    f"for a second running deployment. Detail: {conflict_message}", True)
            except Exception:  # an alert failure must never interrupt anything
                logging.getLogger("main").exception("Could not send the startup-conflict alert")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _singleton_conflict_message, _heartbeat_task, _claim_task, _storage_problem_message
    _storage_problem_message = startup_guard.evaluate_at_startup(singleton_lock.DATA_DIR)
    if _storage_problem_message:
        # Persistent-storage check failed: the dashboard still comes up (so the reason is
        # visible), but this process never claims the lock or starts any trading engine.
        # (The managers' start gate also refuses manual starts - see startup_guard.)
        try:
            await tg.notify_error("SYSTEM", "STARTUP", "Trading has NOT started: " + _storage_problem_message, True)
        except Exception:  # an alert failure must never interrupt anything
            logging.getLogger("main").exception("Could not send the storage-problem alert")
    else:
        claimed, conflict_message = singleton_lock.check_and_claim_lock()
        if claimed:
            _heartbeat_task = asyncio.create_task(singleton_lock.heartbeat_loop())
            await _start_trading_engines()
        else:
            _singleton_conflict_message = conflict_message
            _claim_task = asyncio.create_task(_claim_when_free())
    yield
    # 2026-09-16 fix: every trading engine is fully stopped FIRST, and the lock is
    # released last - a new process can only ever start after this one has genuinely
    # finished, not while it's still in the middle of doing so.
    if _claim_task:
        _claim_task.cancel()
        try:
            await _claim_task
        except asyncio.CancelledError:
            pass
    if _heartbeat_task:
        _heartbeat_task.cancel()
        try:
            await _heartbeat_task
        except asyncio.CancelledError:
            pass
    await manager.shutdown_all()
    await okx_manager.shutdown_all()
    singleton_lock.release_lock_on_clean_shutdown()


# The interactive API docs (/docs, /redoc) and the machine-readable API description (/openapi.json)
# are switched OFF: they need no login and would hand anyone who finds the public URL a complete map
# of every endpoint and field name. Nothing in the dashboard uses them.
app = FastAPI(title="Multi-Strategy Futures Bot", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(api_router)
app.include_router(okx_api_router)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def dashboard():
    return FileResponse(str(STATIC_DIR / "dashboard.html"))


@app.get("/healthz")
def healthz():
    """Deliberately unauthenticated, minimal liveness check - for an
    external uptime monitor (UptimeRobot, healthchecks.io, etc.) to ping
    from outside this server, so you find out if the whole process/server
    is down even when its own Telegram alerts can't fire (because the
    process that would send them is the thing that died). Returns only
    process uptime and instance count - no account/position/balance data,
    since this endpoint is intentionally reachable without login.

    singleton_lock_conflict (2026-09-16, owner request): non-sensitive -
    just says whether another instance was detected running at startup,
    same category of information as uptime_seconds. Included here (not
    only behind login) so an external monitor can also catch this
    specific, serious failure mode.

    running_instances (2026-09-16 fix, item #8): this used to count ONLY
    manager.instances (Binance) - okx_manager was a completely separate
    dict, never included. An account running entirely on OKX with zero
    Binance pairs would report 0 running instances here even while
    genuinely healthy and trading, which is exactly the kind of thing that
    makes an external monitor misleading rather than useful. Now the
    TOTAL across both platforms (kept as this same field name and meaning,
    so an existing monitor watching this number doesn't need
    reconfiguring), with the per-platform breakdown also included for
    anyone who wants more detail than the total alone gives."""
    return JSONResponse({
        "status": "ok",
        "uptime_seconds": round(time.time() - _START_TIME, 1),
        "running_instances": len(manager.instances) + len(okx_manager.instances),
        "running_instances_binance": len(manager.instances),
        "running_instances_okx": len(okx_manager.instances),
        "singleton_lock_conflict": _singleton_conflict_message,
        "storage_problem": _storage_problem_message,
    })
