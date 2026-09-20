"""The data moves of ``000000000010`` and ``000000000011``, run against real rows.

``000000000010`` lifts the board<->tapacanto coordination out of each product's
``attributes`` bag into ``product_families`` + ``products.family_id``, and the
alias into ``products.alias``. ``000000000011`` then strips the two keys out of
the JSON, once the new code has been running long enough to trust.

On an empty database — the only place a migration is normally exercised — every
one of these statements is a no-op, which is why they live as module constants.

Note the shape of the test: the schema here is built by ``Base.metadata.create_all``,
i.e. the POST-migration shape, so it inserts the LEGACY form by raw SQL (family
and alias inside the bag, columns blank) and replays the constants over it.
Same device as ``tests/test_order_piece_material_backfill.py``.
"""

import importlib.util
import json
import pathlib

from sqlalchemy import text

_VERSIONS = pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _module(filename, name):
    spec = importlib.util.spec_from_file_location(name, _VERSIONS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _mig_10():
    return _module("000000000010_product_families.py", "_families_mig")


def _mig_11():
    return _module("000000000011_strip_family_from_attributes.py", "_strip_mig")


def _legacy_product(db_session, *, code, type_, attributes, name=None):
    """A product as it looked before the columns existed."""
    db_session.execute(
        text(
            "INSERT INTO products (type, code, name, price, is_active, attributes, "
            "created_at, updated_at) VALUES (:t, :c, :n, 10.0, true, "
            "CAST(:a AS json), now(), now())"
        ),
        {
            "t": type_,
            "c": code,
            "n": name or f"Producto {code}",
            "a": json.dumps(attributes),
        },
    )
    db_session.commit()


def _replay_10(db_session):
    mig = _mig_10()
    for statement in (
        mig.SEED_FAMILIES_FROM_ATTRIBUTES,
        mig.LINK_PRODUCTS_TO_FAMILIES,
        mig.COPY_ALIAS_TO_COLUMN,
    ):
        db_session.execute(text(statement))
    db_session.commit()


def _rows(db_session):
    return {
        r[0]: r
        for r in db_session.execute(
            text(
                "SELECT code, family_id, alias, attributes::text FROM products "
                "ORDER BY code"
            )
        ).all()
    }


def _families(db_session):
    return db_session.execute(
        text("SELECT name, normalized_name FROM product_families ORDER BY name")
    ).all()


def test_the_backfill_creates_one_family_per_design_and_links_both_sides(db_session):
    _legacy_product(
        db_session,
        code="B15",
        type_="board",
        attributes={
            "height": 2800,
            "width": 2070,
            "thickness": 15,
            "family": "Cashmere",
        },
    )
    _legacy_product(
        db_session,
        code="T19",
        type_="edge_banding",
        attributes={
            "width": 19,
            "thickness": 0.45,
            "family": "Cashmere",
            "alias": "CSH",
        },
    )
    _replay_10(db_session)

    assert _families(db_session) == [("Cashmere", "cashmere")]
    rows = _rows(db_session)
    assert rows["B15"][1] == rows["T19"][1] is not None
    assert rows["B15"][2] is None  # a board has no alias
    assert rows["T19"][2] == "CSH"


def test_spellings_of_one_design_collapse_into_a_single_family(db_session):
    """The key is casefolded, so "CASHMERE" and "  cashmere " are one design.

    ``min()`` picks the display name, which is what makes the result the same on
    every machine rather than whatever the scan happened to read last.
    """
    for n, spelling in enumerate(("CASHMERE", "  cashmere ", "Cashmere")):
        _legacy_product(
            db_session,
            code=f"B{n}",
            type_="board",
            attributes={
                "height": 2800,
                "width": 2070,
                "thickness": 15,
                "family": spelling,
            },
        )
    _replay_10(db_session)

    assert _families(db_session) == [("CASHMERE", "cashmere")]
    assert len({r[1] for r in _rows(db_session).values()}) == 1


def test_a_product_with_no_family_is_left_unlinked(db_session):
    """76 of the catalog's 210 boards are in this state — plywood, OSB, MDF
    fondo. The key is PRESENT with a JSON null, so ``->>`` yields SQL NULL rather
    than '', which is why the backfill needs the ``coalesce`` and not a bare
    ``<> ''``.
    """
    _legacy_product(
        db_session,
        code="PLY",
        type_="board",
        attributes={"height": 2440, "width": 1220, "thickness": 15, "family": None},
    )
    _replay_10(db_session)

    assert _families(db_session) == []
    assert _rows(db_session)["PLY"][1] is None


def test_the_alias_is_copied_verbatim(db_session):
    """It is one of the three fields salted into the optimization hash, so a
    value that changes by a character re-costs every cached quote carrying that
    tapacanto. No trimming, no empty-to-null coercion."""
    _legacy_product(
        db_session,
        code="T19",
        type_="edge_banding",
        attributes={
            "width": 19,
            "thickness": 0.45,
            "family": "Cashmere",
            "alias": "CSH",
        },
    )
    _replay_10(db_session)
    assert _rows(db_session)["T19"][2] == "CSH"


def test_the_strip_runs_after_the_reads_and_leaves_the_rest_of_the_bag_alone(
    db_session,
):
    """``000000000011`` is a separate migration precisely so this ordering is a
    deploy decision, not a race: while the keys are still there the old code
    keeps working and the release can be rolled back."""
    bag = {
        "width": 19,
        "thickness": 0.45,
        "bandType": "Soft",
        "color": "Blanco",
        "length": 50000,
        "subtype": "Solid",
        "family": "Cashmere",
        "alias": "CSH",
    }
    _legacy_product(db_session, code="T19", type_="edge_banding", attributes=bag)
    _replay_10(db_session)

    # Still there after 010: that is what makes the rollback window real.
    assert json.loads(_rows(db_session)["T19"][3])["family"] == "Cashmere"

    db_session.execute(text(_mig_11().STRIP_FAMILY_AND_ALIAS))
    db_session.commit()

    row = _rows(db_session)["T19"]
    remaining = json.loads(row[3])
    assert "family" not in remaining and "alias" not in remaining
    # Everything the VENDOR owns survives the jsonb round trip untouched.
    assert remaining == {k: v for k, v in bag.items() if k not in ("family", "alias")}
    # ...and the values are safe in the columns.
    assert row[1] is not None and row[2] == "CSH"


def test_the_downgrade_puts_both_keys_back(db_session):
    """A rollback has to land on data the pre-010 code can read."""
    _legacy_product(
        db_session,
        code="T19",
        type_="edge_banding",
        attributes={
            "width": 19,
            "thickness": 0.45,
            "family": "Cashmere",
            "alias": "CSH",
        },
    )
    _replay_10(db_session)
    mig = _mig_11()
    db_session.execute(text(mig.STRIP_FAMILY_AND_ALIAS))
    db_session.execute(text(mig.RESTORE_FAMILY_AND_ALIAS))
    db_session.execute(text(mig.RESTORE_ORPHAN_ALIAS))
    db_session.commit()

    restored = json.loads(_rows(db_session)["T19"][3])
    assert restored["family"] == "Cashmere"
    assert restored["alias"] == "CSH"


def test_the_downgrade_recovers_an_alias_with_no_family(db_session):
    """A tape can carry an alias while belonging to no design. Rare — but the
    join that restores the family would skip it, so it gets its own statement."""
    _legacy_product(
        db_session,
        code="T22",
        type_="edge_banding",
        attributes={"width": 22, "thickness": 0.45, "alias": "SOL"},
    )
    _replay_10(db_session)
    mig = _mig_11()
    db_session.execute(text(mig.STRIP_FAMILY_AND_ALIAS))
    db_session.execute(text(mig.RESTORE_FAMILY_AND_ALIAS))
    db_session.execute(text(mig.RESTORE_ORPHAN_ALIAS))
    db_session.commit()

    assert json.loads(_rows(db_session)["T22"][3])["alias"] == "SOL"
