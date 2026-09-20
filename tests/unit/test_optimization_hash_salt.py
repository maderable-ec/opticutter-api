"""The edge-banding signature that salts the optimization hash.

This exists for one reason: when ``alias`` moved out of the product's
``attributes`` bag into a column of its own, this dict was the single place
where that refactor could have silently invalidated every cached quote. The
digest is ``json.dumps(..., sort_keys=True, separators=(",", ":"))`` over the
whole input, so the same strings under the same keys have to keep producing the
same bytes.

The expected literal below was captured by running the salt's construction
against the PRE-refactor code, before ``alias`` was a column. Writing it
afterwards would have made this test pass trivially and prove nothing.
"""

import json
from types import SimpleNamespace

from src.modules.optimizations.service import edge_banding_salt

# Captured from the shape ``_compute_hash`` produced when the alias still lived
# in ``attributes``. Byte-for-byte what the canonical JSON must keep emitting.
EXPECTED = '{"7":{"alias":"BLN","band_type":"Soft","price":1.25}}'


def _canonical(salt):
    return json.dumps(salt, sort_keys=True, separators=(",", ":"))


def _tape(price=1.25, band_type="Soft", alias="BLN"):
    return SimpleNamespace(
        price=price,
        alias=alias,
        attributes={
            "bandType": band_type,
            "color": "Blanco",
            "width": 22,
            "thickness": 0.45,
        },
    )


def test_the_salt_is_byte_identical_to_the_pre_column_shape():
    assert _canonical(edge_banding_salt({7: _tape()})) == EXPECTED


def test_a_tape_without_an_alias_salts_null_not_a_missing_key():
    """``None`` has to stay ``null`` rather than dropping out: a key that
    disappears changes the canonical JSON as surely as a changed value."""
    salt = edge_banding_salt({7: _tape(alias=None)})
    assert salt["7"]["alias"] is None
    assert '"alias":null' in _canonical(salt)


def test_the_family_is_deliberately_not_in_the_salt():
    """The family coordinates but is never printed, so a change to it moves no
    document and must not cost a recompute. The alias and the band type ARE
    printed (the ``2L1C CS CSH`` notation), which is why they are here."""
    salt = edge_banding_salt({7: _tape()})
    assert set(salt["7"]) == {"price", "band_type", "alias"}


def test_the_bag_no_longer_answers_for_the_alias():
    """Guards the actual regression: reading ``attributes['alias']`` again would
    return None for every product, quietly changing the digest of every quote
    that carries a tapacanto."""
    tape = _tape()
    tape.attributes["alias"] = "STALE"
    assert edge_banding_salt({7: tape})["7"]["alias"] == "BLN"
