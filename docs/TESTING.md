# Testing

The suite combines two layers with different purposes:

| Layer | Marker | Where | Hits PostgreSQL | Purpose |
|-------|--------|-------|------------------|---------|
| **Unit** | `unit` | `tests/unit/` | **No** (mocks / pure functions) | Isolated business logic: state machines, gates, mappings, calculations |
| **Integration** | `integration` | `tests/*.py` (everything else) | **Yes** (dedicated `cutter_test_db`) | HTTP endpoints, real queries, constraints, end-to-end flows |

Marking is **automatic by path** (the `pytest_collection_modifyitems` hook in
`tests/conftest.py`): anything under `tests/unit/` is `unit`; everything else
is `integration`. No manual annotation needed.

Two markers are applied by hand, both on the cutting engine's benchmarks:
`slow` (full search budget — seconds per test) and **`benchmark`**, which is
the one CI deselects: a `benchmark` test is one whose answer is a property of
the **OR-Tools build**, so `make benchmark` runs it against a `linux/amd64`
image. Anything that must hold on *every* build (layout validity, nothing left
unplaced, never worse than the heuristics alone) goes in an unmarked test that
runs everywhere.

Note where the commercial claims live: pre-order 21 at **kerf 4** (5 boards) and
pre-order 20 at both kerfs are unmarked and run in CI, because the heuristics
reach those numbers on their own — no solver, no build dependence. Kerf 4 is the
shop's actual blade (confirmed 2026-08-13), which is why the board counts are
pinned there. At kerf 5 pre-order 21 bills the heuristic ceiling since
`ENGINE_VERSION` 6 metered the solver, a trade taken deliberately (see
`ExactConfig` in `src/cutting/search.py`), so the only kerf-5 assertion left is
"never worse than the heuristics".

CP-SAT picks arbitrarily among equally optimal packings, and the pick is a
property of the binary rather than of the pinned version: macOS arm64 and
manylinux x86_64 once disagreed on pre-order 21 at kerf 5 (5 boards vs 6). The
engine no longer inherits that pick — `exact._build_fill` reconstructs a
canonical layout from the solver's answer — and
`test_preorder_21_bills_the_same_under_any_solver_tie_break` guards it by
perturbing the solver's tie-break 12 ways and requiring one single bill. It runs
at kerf 5 on purpose: that is where the solver still decides this cut list, so a
sweep at kerf 4 would pass without testing anything. Cut length can still differ
between tied optima; it is the last tiebreak of the search objective, so it
changes the diagram, never the bill.

## Commands

```bash
# Full suite (integration + unit) against cutter_test_db, with the 80% coverage gate
make tests            # in Docker
make tests-local      # local (PostgreSQL on localhost:5433)

# Fast loop, unit only: no Postgres, no coverage gate
DATABASE_URL=postgresql://cutter:cutter@localhost:5433/cutter_test_db \
  pytest -m unit --no-cov

# Integration only
pytest -m integration

# Everything CI runs (the parity benchmarks are slow and opt-in, see above)
pytest -q -m "not benchmark"

# The parity benchmarks, against the build that ships (linux/amd64, emulated on
# Apple Silicon). Includes the 12-way solver tie-break sweep; ~2 min.
make benchmark
```

Notes:

- `pytest -m unit` **does not require a live Postgres**, but `DATABASE_URL`
  must still be **set** (`src/shared/config.py` reads it on import). It can
  point at an unreachable host — unit tests never open a connection.
- `--no-cov` is used in the unit fast loop because the global gate
  (`--cov-fail-under=80`, in `pyproject.toml`) would fail over a subset. The
  gate is enforced by `make tests`.
- `BCRYPT_ROUNDS=4` is set by the `Makefile` in test targets (valid bcrypt
  hashes, ~16x faster). The default in dev/prod is 12.

## Writing tests

### Unit — pure function (no DB, no mock)

```python
from src.modules.products.service import ProductService

def test_design_key():
    assert ProductService._design_key("MDP-SL-CSH-15") == "SL-CSH"
```

### Unit — service logic with `mock_session`

The `mock_session` fixture (in `tests/unit/conftest.py`) is a
`MagicMock(spec=Session)`. Build the service with it and configure per-test
what each method queries:

```python
def test_queued_requires_payment(mock_session):
    order = OrderModel(status="confirmed", banding_status="not_applicable")
    svc = OrderService(mock_session)
    svc.get_scoped_or_404 = lambda *a, **k: order   # load by id -> object in hand
    with pytest.raises(ValidationError):
        svc.transition(1, OrderStatus.queued, actor=admin, payment=None)
    mock_session.commit.assert_not_called()          # the rejection doesn't persist
```

For queries: `mock_session.query.return_value.filter.return_value.count.return_value = 2`.

### Integration

Same as before: the `client` (an admin-authenticated `TestClient`) /
`anon_client` / `db_session` fixtures from the root `tests/conftest.py`.
Reserve this layer for endpoints, real queries, constraints and transactional
idempotency.

## Rule of thumb

- New logic (validations, states, calculations) → **unit test** first.
- Anything that depends on a real query, a constraint, or an HTTP flow →
  **integration**.

### Migration candidates (backlog)

Logic that's only covered via integration today and would benefit from a
unit test: the optimization's deterministic hash
(`OptimizationService._compute_hash`), material resolution by source
(`MaterialResolver.resolve`), and pre-order expiry/cap enforcement
(`PreOrderService._expire_if_stale` / `_enforce_open_cap`).
