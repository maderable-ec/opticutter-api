"""The engine has to be able to say what it is, especially when it degraded.

Deliberately NOT in ``test_rust_backend.py``: that module skips wholesale when
the wheel is absent, and the case worth pinning hardest here is precisely the
one where it *is* absent. These tests must pass on a box with the wheel, on a
box without it, and in CI's ``OPT_ENGINE_BACKEND=python`` run.
"""

import logging

import pytest

from src.cutting import rust_backend
from src.modules.optimizations.engine_info import (
    backend_name,
    degraded_warning,
    engine_summary,
    probe_worker,
)


@pytest.fixture(autouse=True)
def _restore_backend():
    yield
    rust_backend.set_enabled(None)


def test_status_separates_what_was_asked_from_what_runs(monkeypatch):
    """``requested`` and ``effective`` are two different questions.

    Collapsing them into one boolean is what made the degradation invisible: a
    box running Python because someone chose it and a box running Python because
    its wheel is missing looked identical.
    """
    monkeypatch.setenv("OPT_ENGINE_BACKEND", "python")
    rust_backend.set_enabled(None)

    status = rust_backend.status()

    assert status["requested"] == "python"
    assert status["effective"] == "python"
    assert rust_backend.degraded() is False
    assert degraded_warning() is None


def test_a_demanded_wheel_that_cannot_be_imported_is_reported(monkeypatch):
    """The silent failure this whole reporting path exists for.

    ``_resolve`` checks the wheel BEFORE the environment variable, so demanding
    ``rust`` on a box without it is dropped with no error, no warning and no
    exception — while the packing kernel runs ~3x slower.
    """
    monkeypatch.setattr(rust_backend, "opticutter_core", None)
    monkeypatch.setenv("OPT_ENGINE_BACKEND", "rust")
    rust_backend.set_enabled(None)

    status = rust_backend.status()

    assert status["requested"] == "rust"
    assert status["effective"] == "python"
    assert status["wheel_importable"] is False
    assert status["wheel_version"] is None
    assert rust_backend.degraded() is True
    assert "OPT_ENGINE_BACKEND=rust" in degraded_warning()


def test_an_unset_backend_is_auto_and_never_counts_as_degraded(monkeypatch):
    """``auto`` accepts the Python path as a correct outcome, not a fault."""
    monkeypatch.delenv("OPT_ENGINE_BACKEND", raising=False)
    monkeypatch.setattr(rust_backend, "opticutter_core", None)
    rust_backend.set_enabled(None)

    assert rust_backend.status()["requested"] == "auto"
    assert rust_backend.degraded() is False
    assert degraded_warning() is None


def test_backend_name_tracks_the_resolved_backend(monkeypatch):
    monkeypatch.setenv("OPT_ENGINE_BACKEND", "python")
    rust_backend.set_enabled(None)

    assert backend_name() == "python"


def test_engine_summary_is_one_greppable_line_naming_both_engines():
    """It is read with ``grep motor:`` in a production container, so: one line."""
    summary = engine_summary()

    assert "\n" not in summary
    assert summary.startswith("motor: ")
    assert "packing=" in summary
    assert "CP-SAT=" in summary
    assert "ENGINE_VERSION=" in summary


def test_engine_summary_survives_the_production_log_level(caplog):
    """Production runs at WARNING; an INFO line would never be seen."""
    with caplog.at_level(logging.WARNING):
        logging.getLogger(__name__).warning("%s", engine_summary())

    assert any("motor: packing=" in r.getMessage() for r in caplog.records)


def test_probe_worker_answers_for_its_own_process():
    """The warm-up payload: the parent cannot answer for a forkserver child."""
    probe = probe_worker()

    assert probe["packing"] in ("rust", "python")
    assert isinstance(probe["cpsat"], bool)
    assert probe["pid"] > 0
