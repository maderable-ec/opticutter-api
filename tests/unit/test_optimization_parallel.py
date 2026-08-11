"""Unit tests for per-material process parallelism.

No DB. What these pin down is the contract that makes the feature safe rather
than fast: the pickle boundary, result ORDER, and the exact set of failures that
may fall back to the in-process path. Timing is not asserted anywhere — the win
is measured by ``scripts/bench_shopfiles.py``, not by a test that would go flaky
on a loaded machine.
"""

import logging
import pickle
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

import pytest

from src.cutting import CuttingParameters, PackingStrategy
from src.cutting.models import BinSpec, Piece
from src.cutting.search import ExactConfig, SearchBudget
from src.modules.optimizations import parallel
from src.modules.optimizations.materials import ResolvedMaterial
from src.modules.optimizations.parallel import PoolJob, run_pool_job, run_pool_jobs
from src.modules.optimizations.schemas import PoolFillOrder
from src.shared.config import config
from src.shared.exceptions import ValidationError

PARAMS = CuttingParameters(kerf=3, top_trim=0, bottom_trim=0, left_trim=0, right_trim=0)
# Small on purpose: these tests are about plumbing, and every second here is paid
# on every CI run.
BUDGET = SearchBudget(tries_per_board=8, iterations=4, beam_width=3)
NO_EXACT = ExactConfig(enabled=False)


@pytest.fixture(autouse=True)
def _release_executor():
    """A live forkserver makes pytest hang at exit."""
    yield
    parallel.shutdown_pool_executor()


def _mat(key, width=1000.0, height=800.0, *, source="catalog", cost=50.0):
    return ResolvedMaterial(
        key=key,
        width=width,
        height=height,
        thickness=18,
        cost_per_unit=cost,
        source=source,
        fill_order=PoolFillOrder.auto,
    )


def _offcut(key, width, height, *, pool_key="board", quantity=1):
    return ResolvedMaterial(
        key=key,
        width=width,
        height=height,
        thickness=18,
        cost_per_unit=0.0,
        source="clientOffcut",
        quantity=quantity,
        pool_key=pool_key,
        fill_order=PoolFillOrder.auto,
    )


def _pieces(prefix, count, width=300.0, height=200.0):
    return tuple(
        Piece(id=f"{prefix}#{i + 1}", width=width, height=height, quantity=1)
        for i in range(count)
    )


def _job(key="board", *, pieces=None, offcuts=(), half_spec=None, material=None):
    material = material or _mat(key)
    return PoolJob(
        material_key=key,
        pieces=pieces if pieces is not None else _pieces(key, 6),
        material=material,
        offcuts=offcuts,
        cutting_params=PARAMS,
        strategy=PackingStrategy.MAX_EFFICIENCY,
        half_spec=half_spec,
        budget=BUDGET,
        seed=0,
        exact_config=NO_EXACT,
    )


def _dicts(layouts):
    return [layout.to_dict() for layout in layouts]


class _StubFuture:
    def __init__(self, exc):
        self._exc = exc

    def result(self, timeout=None):
        raise self._exc


class _StubExecutor:
    """Stands in for a pool that is broken in a specific way."""

    def __init__(self, exc, *, raise_on_submit=True):
        self._exc = exc
        self._raise_on_submit = raise_on_submit
        self.submits = 0

    def submit(self, fn, *args, **kwargs):
        self.submits += 1
        if self._raise_on_submit:
            raise self._exc
        return _StubFuture(self._exc)


def test_pool_job_round_trips_through_pickle():
    """The pickle boundary is a closed set of plain dataclasses.

    Guards against someone giving ``ResolvedMaterial`` an ORM field or a
    ``Session``: that would fail here rather than in production.
    """
    job = _job(
        offcuts=(_offcut("scrap", 600.0, 400.0),),
        half_spec=BinSpec(
            key="board", width=500.0, height=800.0, thickness=18, cost_per_unit=27.5
        ),
    )

    assert pickle.loads(pickle.dumps(job)) == job


def test_layouts_round_trip_through_pickle():
    """``Piece.area``/``Rectangle.area`` are set in ``__post_init__``, not fields.

    Pickle restores ``__dict__`` without re-running ``__init__``, so they survive
    today — but they would not under a future ``slots=True``.
    """
    layouts = run_pool_job(_job())

    restored = pickle.loads(pickle.dumps(layouts))

    assert _dicts(restored) == _dicts(layouts)


@pytest.mark.slow
def test_run_pool_job_gives_the_same_layouts_in_a_child_process():
    """The determinism claim, exercised across a real process boundary."""
    job = _job()
    inline = run_pool_job(job)

    with ProcessPoolExecutor(max_workers=1, mp_context=parallel._make_context()) as ex:
        in_child = ex.submit(run_pool_job, job).result(timeout=120)

    assert _dicts(in_child) == _dicts(inline)


@pytest.mark.slow
def test_run_pool_jobs_preserves_order(monkeypatch):
    """Ordered by submission, never by completion.

    ``_build_result_payload`` flattens the results in list order and the payload
    is cached under a hash of the *inputs*, so a completion-ordered result would
    make the cache disagree with a serial recompute. The big job goes first so
    completion order differs from submission order.
    """
    monkeypatch.setattr(config, "OPT_POOL_WORKERS", 2)
    jobs = [
        _job("big", pieces=_pieces("big", 40)),
        _job("tiny", pieces=_pieces("tiny", 1)),
        _job("small", pieces=_pieces("small", 3)),
    ]

    parallel_results = run_pool_jobs(jobs)

    # Without this the test is vacuous: every infrastructure failure falls back to
    # the in-process path, which would satisfy the equality below while proving
    # nothing about the parallel one.
    assert parallel._executor is not None and not parallel._breaker_open()
    assert [_dicts(r) for r in parallel_results] == [
        _dicts(run_pool_job(j)) for j in jobs
    ]


def test_single_job_never_touches_the_executor(monkeypatch):
    """One material has nothing to overlap with; don't pay for a forkserver."""
    monkeypatch.setattr(config, "OPT_POOL_WORKERS", 2)
    monkeypatch.setattr(
        parallel, "_get_executor", lambda: pytest.fail("executor was built")
    )
    job = _job()

    assert _dicts(run_pool_jobs([job])[0]) == _dicts(run_pool_job(job))


def test_one_worker_never_touches_the_executor(monkeypatch):
    """``OPT_POOL_WORKERS=1`` is the kill switch: the old path, byte for byte."""
    monkeypatch.setattr(config, "OPT_POOL_WORKERS", 1)
    monkeypatch.setattr(
        parallel, "_get_executor", lambda: pytest.fail("executor was built")
    )
    jobs = [_job("a"), _job("b")]

    assert [_dicts(r) for r in run_pool_jobs(jobs)] == [
        _dicts(run_pool_job(job)) for job in jobs
    ]


@pytest.mark.parametrize(
    "failure",
    [
        BrokenProcessPool("worker died"),
        OSError("fork: Resource temporarily unavailable"),
        TimeoutError("job took too long"),
        pickle.PicklingError("cannot pickle"),
    ],
)
def test_infrastructure_failures_fall_back_in_process(monkeypatch, caplog, failure):
    """A quote must never fail because parallelization failed."""
    monkeypatch.setattr(config, "OPT_POOL_WORKERS", 2)
    stub = _StubExecutor(failure)
    monkeypatch.setattr(parallel, "_get_executor", lambda: stub)
    jobs = [_job("a"), _job("b")]

    with caplog.at_level(logging.WARNING):
        results = run_pool_jobs(jobs)

    assert [_dicts(r) for r in results] == [_dicts(run_pool_job(job)) for job in jobs]
    assert "falling back to in-process optimization" in caplog.text


def test_a_break_opens_the_breaker_so_the_pool_is_not_rebuilt(monkeypatch):
    """Rebuilding on a box that is out of memory just pays fork-and-die forever."""
    monkeypatch.setattr(config, "OPT_POOL_WORKERS", 2)
    stub = _StubExecutor(BrokenProcessPool("worker died"))
    monkeypatch.setattr(parallel, "_get_executor", lambda: stub)
    jobs = [_job("a"), _job("b")]

    run_pool_jobs(jobs)
    assert parallel._breaker_open()

    run_pool_jobs(jobs)

    # Still one submit: the second batch went straight in-process.
    assert stub.submits == 1


def test_domain_errors_are_not_swallowed_by_the_fallback(monkeypatch):
    """A domain error must propagate, not trigger a silent second run.

    This is the test that stops a future ``except Exception`` from creeping in:
    falling back on a ``ValidationError`` would double the work and raise anyway,
    and a genuine engine bug would hide behind nothing but a permanent slowdown.
    """
    monkeypatch.setattr(config, "OPT_POOL_WORKERS", 2)
    stub = _StubExecutor(ValidationError("pieza inválida"), raise_on_submit=False)
    monkeypatch.setattr(parallel, "_get_executor", lambda: stub)
    monkeypatch.setattr(
        parallel, "run_pool_job", lambda job: pytest.fail("fell back on a domain error")
    )

    with pytest.raises(ValidationError):
        run_pool_jobs([_job("a"), _job("b")])


def test_run_pool_job_does_not_mutate_its_inputs():
    """Inline shares the ``Piece`` objects with the caller; a worker gets copies.

    Nothing mutates them today (``packer`` documents it, and ``optimize_pool``'s
    ``auto`` mode relies on it by running two fills over the same list), but the
    two paths would diverge the moment something did.
    """
    job = _job()
    before = [(p.id, p.width, p.height, p.quantity, p.can_rotate) for p in job.pieces]

    run_pool_job(job)

    assert [
        (p.id, p.width, p.height, p.quantity, p.can_rotate) for p in job.pieces
    ] == before


def test_empty_batch_is_a_no_op():
    assert run_pool_jobs([]) == []


@pytest.mark.slow
def test_shutdown_actually_terminates_the_workers(monkeypatch):
    """Measured leak, so it gets a test.

    ``shutdown(wait=False)`` alone returns instantly but leaves the worker alive
    under uvicorn, and since every worker holds a duplicate of the forkserver's
    liveness pipe, the forkserver never sees EOF either — both outlive the server.
    Killing first is also what keeps the teardown instant when a long job is in
    flight, which is what ``docker stop``'s 10-second grace needs.
    """
    monkeypatch.setattr(config, "OPT_POOL_WORKERS", 2)
    run_pool_jobs([_job("a"), _job("b")])
    processes = list(parallel._executor._processes.values())
    assert processes, "the parallel path did not actually run"

    parallel.shutdown_pool_executor()

    for process in processes:
        process.join(timeout=15)
        assert not process.is_alive()
