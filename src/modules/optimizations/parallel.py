"""Per-material parallelism for the optimizer.

``OptimizationService.compute`` optimizes one pool per material, and the pools are
independent — so a request *costs* the sum over its materials when it *could* cost
the max. This module runs them in separate processes. Processes, not threads: the
engine is pure-Python CPU work and the GIL would serialize it anyway.

Two properties make that safe, and both are load-bearing:

- ``src/cutting/`` imports nothing from ``src.shared`` or ``src.modules``. No DB
  session, no Redis, no config, no module-level mutable state, no ``lru_cache``,
  and the RNG is instance-local and seeded from the request (``search.py``). Every
  memo is built fresh inside ``optimize_bins``. A pool therefore computes
  identically wherever it runs.
- CP-SAT already runs single-threaded with a fixed seed and a *deterministic time*
  budget (a work unit, not wall clock), so two solvers running concurrently cannot
  change each other's answer.

Hence this is arithmetically neutral: same layouts, same board counts, same
optimization hash. ``ENGINE_VERSION`` is deliberately NOT bumped, and the Redis
cache stays valid across the deploy — which is also the best evidence that the
claim holds, since every pre-deploy entry keeps being served and must keep being
correct.
"""

import atexit
import logging
import multiprocessing
import os
import pickle
import signal
import threading
import time
from concurrent.futures import BrokenExecutor, ProcessPoolExecutor
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from src.cutting.consolidate import DEFAULT_MIN_USABLE_OFFCUT, consolidate_layouts
from src.cutting.enums import PackingStrategy
from src.cutting.models import BinSpec, CuttingLayout, Piece
from src.cutting.parameters import CuttingParameters
from src.cutting.search import ExactConfig, SearchBudget, optimize_bins
from src.modules.optimizations.engine_info import probe_worker
from src.modules.optimizations.materials import ResolvedMaterial
from src.modules.optimizations.pool import optimize_pool
from src.shared.config import config

logger = logging.getLogger(__name__)

# Workers run below everything else on the box. Two un-niced CPU-bound children
# saturate a 2-vCore VPS and make the whole site feel dead — TLS handshakes stall,
# Postgres gets descheduled, the SPA stops loading — for the whole time one
# person's quote is computing. The quote is the thing that is allowed to be slow.
_WORKER_NICENESS = 5

# How long a break keeps us on the in-process path. Rebuilding an executor on a
# box that is genuinely out of memory just pays fork-and-die on every request.
_BREAKER_COOLDOWN_SECONDS = 300.0

# Infrastructure failures, and only those. A domain error raised inside a worker
# must reach the caller unchanged: falling back on it would silently double the
# work and then raise anyway, and would hide a real engine bug behind nothing but
# a permanent performance regression. Note ``TimeoutError`` is an ``OSError``
# subclass on 3.11+, listed separately because it is the case we actually expect.
_POOL_FAILURES = (
    BrokenExecutor,
    TimeoutError,
    OSError,
    pickle.PicklingError,
    pickle.UnpicklingError,
)

_executor: Optional[ProcessPoolExecutor] = None
# ``compute`` runs on anyio threadpool threads (the route handler is a sync
# ``def``), so the singleton needs a real lock.
_executor_lock = threading.Lock()
_disabled_until = 0.0


@dataclass(frozen=True)
class PoolResult:
    """One material's layouts plus what they cost to produce.

    The timing rides back with the layouts instead of being taken around the
    call: in the parallel path the parent only sees *completion* order, so a
    ``perf_counter`` on this side would measure queueing, not the search. It is
    measured where the work happens and crosses the pickle boundary as a float,
    which keeps ``run_pool_job`` the single implementation for both paths.
    """

    layouts: List[CuttingLayout]
    seconds: float


@dataclass(frozen=True)
class PoolJob:
    """Everything one material's optimization needs, and nothing else.

    A closed set of plain dataclasses, because this is the pickle boundary: no
    ORM object, no ``Session``, no Pydantic model, and no ``edge_map``/``net_map``
    (those are cheap, pure, and needed only by the parent).

    It is a *single frozen argument* for a reason that is not ergonomics. The
    parallel path is ``executor.submit(run_pool_job, job)`` and the fallback is
    ``run_pool_job(job)``: one implementation of "optimize one pool", one
    argument, so the two paths cannot drift. Had the fallback re-called
    ``optimize_pool(...)`` with nine unpacked kwargs, an argument added to one
    call site and not the other would produce different geometry only on the path
    that runs least often — and is therefore least tested.
    """

    material_key: str  # parent-side logging identity; the optimizer never reads it
    pieces: Tuple[Piece, ...]
    material: ResolvedMaterial
    offcuts: Tuple[ResolvedMaterial, ...]
    cutting_params: CuttingParameters
    strategy: PackingStrategy
    half_spec: Optional[BinSpec]
    budget: SearchBudget
    seed: int
    exact_config: ExactConfig
    min_usable_offcut: float = DEFAULT_MIN_USABLE_OFFCUT


def run_pool_job(job: PoolJob) -> PoolResult:
    """Optimizes ONE material. The single implementation of that verb.

    Runs identically in-process and in a pool worker — that identity *is* the
    determinism argument. Deliberately pure: no logging (a forkserver child never
    imports ``main``, so it has no logging config and no request context), no
    cache, no DB, no globals. The ``perf_counter`` around the body respects all
    of that — it reads a clock and returns the number, so the caller can attribute
    latency per material without this function learning where the log goes.
    """
    started = time.perf_counter()
    layouts = _optimize_job(job)
    return PoolResult(layouts=layouts, seconds=time.perf_counter() - started)


def _optimize_job(job: PoolJob) -> List[CuttingLayout]:
    """The optimization itself, split out so the timing wraps exactly the work.

    The consolidation pass is applied HERE, on the way out, and nowhere else.
    Both branches below produce final layouts, so this is the one place that sees
    every board exactly once — and, just as importantly, it is *outside* the
    search: ``optimize_bins`` is re-entered by ``_catalog_first``'s probing, by
    ``_recut`` and by the half-board downgrade, and re-deriving cut trees for
    candidates that are about to be thrown away would be pure waste. It also
    means ``scripts/bench_battery.py``, which calls ``optimize_bins`` directly,
    keeps producing the same digests — which is the cheap proof that none of this
    can cost a board.
    """
    pieces = list(job.pieces)
    if job.offcuts:
        # Pool: pack across the catalog board + its finite offcuts.
        layouts = optimize_pool(
            pieces=pieces,
            primary=job.material,
            offcuts=list(job.offcuts),
            cutting_params=job.cutting_params,
            strategy=job.strategy,
            half_spec=job.half_spec,
            budget=job.budget,
            seed=job.seed,
            exact_config=job.exact_config,
        )
    else:
        bins = [
            BinSpec(
                key=job.material.key,
                width=job.material.width,
                height=job.material.height,
                thickness=job.material.thickness,
                cost_per_unit=job.material.cost_per_unit,
            )
        ]
        if job.half_spec is not None:
            bins.append(job.half_spec)
        layouts = optimize_bins(
            pieces,
            bins,
            cutting_params=job.cutting_params,
            strategy=job.strategy,
            budget=job.budget,
            seed=job.seed,
            exact_config=job.exact_config,
        )[0]

    return consolidate_layouts(
        layouts,
        job.cutting_params,
        min_usable_offcut=job.min_usable_offcut,
    )


def runs_in_process(jobs: Sequence[PoolJob]) -> bool:
    """Whether this batch skips the executor entirely.

    One material has nothing to overlap with, and the executor would cost a
    forkserver on a box that may never need one. Public so the caller can name
    the path in its latency log without restating the condition — a duplicated
    predicate would drift and then mislabel exactly the slow requests it exists
    to explain. Note ``run_pool_jobs`` can still fall back mid-batch after a pool
    failure; that path logs its own warning, so the two lines together are true.
    """
    return len(jobs) <= 1 or config.OPT_POOL_WORKERS <= 1 or _breaker_open()


def run_pool_jobs(jobs: Sequence[PoolJob]) -> List[PoolResult]:
    """Runs every job and returns the results IN THE SAME ORDER as ``jobs``.

    Parallel when it pays and the executor is healthy, in-process otherwise.
    Never raises for an infrastructure reason — only domain errors escape.
    """
    if not jobs:
        return []
    if runs_in_process(jobs):
        return [run_pool_job(job) for job in jobs]

    executor = _get_executor()
    if executor is None:
        return [run_pool_job(job) for job in jobs]

    timeout = config.OPT_POOL_TIMEOUT_SECONDS
    deadline = time.monotonic() + timeout if timeout > 0 else None
    try:
        futures = [executor.submit(run_pool_job, job) for job in jobs]
        return [
            future.result(
                timeout=None
                if deadline is None
                else max(0.0, deadline - time.monotonic())
            )
            for future in futures
        ]
    except _POOL_FAILURES as exc:
        logger.warning(
            "optimization pool failed (%s: %s) for materials %s; "
            "falling back to in-process optimization",
            type(exc).__name__,
            exc,
            [job.material_key for job in jobs],
        )
        # Drop the poisoned executor first: a BrokenProcessPool is terminal, and a
        # timed-out child is still burning a core (``future.cancel()`` cannot stop
        # a running task). Then stop rebuilding it for a while.
        shutdown_pool_executor()
        _trip_breaker()
        return [run_pool_job(job) for job in jobs]


def warmup_pool_executor(parent_packing: Optional[str] = None) -> None:
    """Starts the forkserver and the workers ahead of the first real request.

    ``ProcessPoolExecutor`` spawns workers lazily, so merely constructing it
    starts nothing; one trivial submit is what pays for the forkserver and its
    preload (``ortools`` is the expensive import). Best-effort by construction —
    a failure here must never stop the application from booting.

    The warm-up payload is ``probe_worker`` rather than a no-op because the fork
    is paid either way and the child is the only thing that can answer which
    engine the *packing* actually runs on: workers resolve the backend in their
    own process. ``parent_packing`` lets the caller assert they agree — a child
    that disagrees is a real deployment fault (a stale forkserver preload, a
    per-process environment difference) that would otherwise be invisible.
    """
    if config.OPT_POOL_WORKERS <= 1:
        return
    executor = _get_executor()
    if executor is None:
        return
    try:
        probe = executor.submit(probe_worker).result(timeout=30)
        logger.warning(
            "optimization pool ready (%s workers) · packing=%s · CP-SAT=%s",
            max(1, config.OPT_POOL_WORKERS),
            probe["packing"],
            "sí" if probe["cpsat"] else "NO",
        )
        if parent_packing is not None and probe["packing"] != parent_packing:
            logger.warning(
                "el worker del pool corre packing=%s pero el proceso padre "
                "corre packing=%s: las optimizaciones multi-material no usan "
                "el mismo motor que el resto",
                probe["packing"],
                parent_packing,
            )
    except Exception as exc:  # noqa: BLE001 - boot must not depend on this
        logger.warning(
            "could not warm up the optimization pool (%s: %s); "
            "optimizations will run in-process until it recovers",
            type(exc).__name__,
            exc,
        )
        shutdown_pool_executor()


def shutdown_pool_executor() -> None:
    """Releases the worker processes and clears the breaker.

    Used for application shutdown, for break recovery (the next batch builds a
    fresh executor) and by tests, which would otherwise hang at exit with a live
    forkserver.

    Terminating the workers before joining is not an optimization — it is the
    only way to get both properties we need, and each half was measured:

    - A plain ``shutdown(wait=False)`` returns instantly but **leaks**: under
      uvicorn the idle worker never exits, and because every worker inherits a
      duplicate of the forkserver's liveness pipe, the forkserver never sees EOF
      either. Both survive the server process forever, and under ``--reload``
      every code change would strand another pair.
    - A plain ``shutdown(wait=True)`` reaps cleanly but would block behind an
      in-flight job. ``docker stop`` allows 10 seconds before SIGKILL, and a quote
      can take 70 on the VPS: the container would be killed mid-teardown and the
      orphans would burn both cores while its replacement boots.

    Killing first makes the join immediate, so ``wait=True`` costs nothing. The
    in-flight quote is lost either way — today it dies with the worker — and the
    client retries into a warm cache.
    """
    global _executor, _disabled_until
    with _executor_lock:
        executor, _executor = _executor, None
        _disabled_until = 0.0
    if executor is None:
        return
    # Private attribute, hence the defensive read: on a CPython that renames it
    # we lose the prompt kill, not correctness — the join below still reaps.
    for process in list((getattr(executor, "_processes", None) or {}).values()):
        try:
            process.terminate()
        except (OSError, ValueError, AttributeError):  # pragma: no cover
            pass
    executor.shutdown(wait=True, cancel_futures=True)


def _get_executor() -> Optional[ProcessPoolExecutor]:
    """The process pool, built on first use. ``None`` if it cannot be created.

    A lazily-built module singleton rather than something the lifespan owns:
    ``compute`` is reached from paths that never run a lifespan (unit tests, the
    bench scripts, ``PreOrderService`` under pytest), and a second construction
    path for those is exactly how the two paths would drift.

    Being a singleton with a fixed ``max_workers`` is also what bounds memory:
    the child count per uvicorn worker is capped regardless of HTTP concurrency,
    so concurrent requests queue behind it instead of multiplying processes.
    """
    global _executor
    with _executor_lock:
        if _executor is not None:
            return _executor
        try:
            _executor = ProcessPoolExecutor(
                max_workers=max(1, config.OPT_POOL_WORKERS),
                mp_context=_make_context(),
                initializer=_init_worker,
                max_tasks_per_child=max(1, config.OPT_POOL_MAX_TASKS_PER_CHILD),
            )
        except (OSError, ValueError) as exc:
            logger.warning(
                "could not create the optimization pool (%s: %s); "
                "optimizing in-process",
                type(exc).__name__,
                exc,
            )
            _executor = None
    if _executor is None:
        _trip_breaker()
    return _executor


def _make_context() -> multiprocessing.context.BaseContext:
    """Start method for the workers: ``forkserver``, never ``fork``.

    ``compute`` runs on an anyio threadpool thread, so a bare ``os.fork()`` would
    duplicate only that thread: any mutex another thread held stays locked forever
    in the child. At that moment the parent holds a SQLAlchemy pool with live
    libpq sockets, a Redis socket, and CPython's allocator and ``logging`` locks —
    and the child would inherit those sockets as writable descriptors. There is no
    safe version of it.

    ``forkserver`` avoids both problems: the parent starts the forkserver by
    fork+exec (``spawnv_passfds``), so it never performs a bare fork, and every
    worker is forked by that process, which is single-threaded and has never
    opened a database connection. The preload imports ``ortools`` once, so each
    worker is a copy-on-write fork of an already-warm image.
    """
    method = (
        "forkserver"
        if "forkserver" in multiprocessing.get_all_start_methods()
        else "spawn"
    )
    ctx = multiprocessing.get_context(method)
    if method == "forkserver":
        # Transitively pulls src.cutting (including ortools) and the pool module.
        # set_forkserver_preload swallows ImportError, so a wrong name here costs
        # the warm-up, never correctness.
        ctx.set_forkserver_preload([__name__])
    return ctx


def _init_worker() -> None:  # pragma: no cover - runs in a child process
    try:
        os.nice(_WORKER_NICENESS)
    except OSError:
        pass
    # Ctrl-C in dev otherwise produces a traceback storm from every child.
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _trip_breaker() -> None:
    global _disabled_until
    _disabled_until = time.monotonic() + _BREAKER_COOLDOWN_SECONDS


def _breaker_open() -> bool:
    return time.monotonic() < _disabled_until


atexit.register(shutdown_pool_executor)
