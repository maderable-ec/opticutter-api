"""Fixtures for the UNIT test layer (no database).

Everything under ``tests/unit/`` is automatically marked ``unit`` (hook
``pytest_collection_modifyitems`` in ``tests/conftest.py``) and **never** opens a
PostgreSQL connection: the ``_create_schema`` fixture is no longer ``autouse``, so
only tests that request ``db_session`` (the integration ones) touch the database.

Test patterns without a DB:

- **Pure functions**: exercised directly (e.g. ``hash_token``, ``normalize_family``,
  ``_has_payment``, ``_progress``).
- **Service logic**: build the service with ``mock_session`` and configure per test
  what each method queries::

      mock_session.get.return_value = some_model
      mock_session.query.return_value.filter.return_value.count.return_value = 3

  For id lookups with branch isolation (``get_scoped_or_404``), the cleanest
  approach is to replace the method on the instance: ``svc.get_scoped_or_404 =
  lambda *a, **k: order``.
"""

from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session


@pytest.fixture
def mock_session():
    """Double for ``sqlalchemy.orm.Session`` to isolate logic from persistence.

    ``spec=Session`` limits the surface to the real session interface (``get``,
    ``query``, ``add``, ``commit``, ``refresh``, ``flush``, ``delete``), so a typo
    on a nonexistent method fails loudly instead of passing silently.
    """
    return MagicMock(spec=Session)
