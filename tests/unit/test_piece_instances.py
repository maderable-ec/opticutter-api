"""Unit: the naming of physical piece instances.

``piece_instance_ids`` is the single place the ``label#N`` rule lives. The
optimizer builds its pieces from it and the order maps every placed piece back
to its cut-list row with it (that is how a placed piece gets its workshop
codes), so a name that changed here would silently hang a code on another piece
-- and orphan every cached payload's ids. The oracle below is the loop
``_build_pieces`` ran before the rule was extracted, kept verbatim so the
extraction is proven, not assumed.
"""

from collections import Counter

import pytest

from src.modules.optimizations.patterns import piece_instance_ids


def _legacy_ids(entries):
    """The pre-extraction loop of ``_build_pieces``, verbatim."""
    base = [label or f"piece_{i+1}" for i, (label, _) in enumerate(entries)]
    totals: Counter = Counter()
    for label, (_, quantity) in zip(base, entries):
        totals[label] += quantity
    seen: Counter = Counter()
    out = []
    for i, (_, quantity) in enumerate(entries):
        for _ in range(quantity):
            seen[base[i]] += 1
            uid = f"{base[i]}#{seen[base[i]]}" if totals[base[i]] > 1 else base[i]
            out.append((i, uid))
    return out


@pytest.mark.parametrize(
    "entries",
    [
        [("Puerta", 1)],
        [("Puerta", 3)],
        [("Puerta", 2), ("Puerta", 2)],
        [(None, 1), (None, 2), ("Lateral", 1)],
        [("Puerta", 2), ("Lateral", 1), ("Puerta", 1), (None, 1)],
        # The known collision: an explicit ``#2`` next to an expanded one.
        [("Puerta#2", 1), ("Puerta", 2)],
    ],
)
def test_instance_ids_match_the_rule_build_pieces_always_used(entries):
    assert list(piece_instance_ids(entries)) == _legacy_ids(entries)


def test_instance_ids_carry_the_row_index_inside_the_group():
    ids = list(piece_instance_ids([("Puerta", 2), ("Lateral", 1), ("Puerta", 1)]))
    assert ids == [(0, "Puerta#1"), (0, "Puerta#2"), (1, "Lateral"), (2, "Puerta#3")]


def test_unlabeled_rows_are_named_by_their_position_in_the_group():
    assert list(piece_instance_ids([(None, 1), (None, 1)])) == [
        (0, "piece_1"),
        (1, "piece_2"),
    ]
