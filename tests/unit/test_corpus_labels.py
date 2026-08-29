"""Unit: reading the commercial program's board count off an export's filename.

``scripts/corpus_labels`` is the label source for the corpus benchmark, so a
silent misread here does not fail -- it produces a plausible wrong number and
the benchmark reports progress that did not happen. Every case below is a real
filename from the shop's 2026 exports.

The eight-file table is the anchor. Those labels were read and verified by hand
against the XML when ``scripts/bench_shopfiles.py`` was written; the parser has
to reproduce them exactly. Their material lists are **inlined** rather than
parsed from ``pruebas/``: that directory is gitignored (the exports carry client
names), so a test that opened the files would pass locally and skip in CI.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from corpus_labels import (  # noqa: E402
    COUNT_MISMATCH,
    LEFTOVER,
    NO_QUANTITY,
    NO_UNIQUE_MATCH,
    clean_name,
    label,
    material_key,
    segments,
    thickness_of,
)

# filename -> (materials as the XML spells them, boards the commercial billed)
HAND_VERIFIED = [
    (
        "9 BLANCOS RH 15MM_03-06-2026.xml",
        ["BLANCO RH"],
        {"BLANCO RH": 9},
    ),
    (
        "12 Y MEDIO BLANCOS RH 15MM_23-06-26.xml",
        ["BLANCO RH"],
        {"BLANCO RH": 12.5},
    ),
    (
        "8 Y MEDIO RISTRETTO RH15MM_16-06-2026.xml",
        ["RISTRETO RH 15MM"],
        {"RISTRETO RH 15MM": 8.5},
    ),
    (
        "4 RISTRETTOS Y 11 BLANCOS RH 15MM-MACAS_08-07-2026.xml",
        ["RISTRETTO RH 15MM", "BLANCO RH 15MM"],
        {"RISTRETTO RH 15MM": 4, "BLANCO RH 15MM": 11},
    ),
    (
        "5JAPANDI Y 4 BLANCO RH 15MM-28-07-2026.xml",
        ["JAPANDI 15MM", "BLANCO RH 15MM"],
        {"JAPANDI 15MM": 5, "BLANCO RH 15MM": 4},
    ),
    (
        "3JAPANDI_2CASHMERE_5BLANCOS RH 15MM-2-07-2026.xml",
        ["JAPANDI RH 15MM", "CASHMERE RH 15MM", "BLANCO RH 15MM"],
        {"JAPANDI RH 15MM": 3, "CASHMERE RH 15MM": 2, "BLANCO RH 15MM": 5},
    ),
    (
        "3 BLANCOS RH 15MM_1 Y MEDIO JAPANDI RH 15MM_1 CASHEMERE RH 15MM_11-08-2026.xml",
        ["BLANCO RH", "JAPANDI RH", "CASHMERE"],
        {"BLANCO RH": 3, "JAPANDI RH": 1.5, "CASHMERE": 1},
    ),
    (
        "3 JAPANDI Y 6 BLANCO RH15MM_3JAPANDI 36MM_1 BLNORMAL_03-08-2026.xml",
        ["JAPANDI RH 15MM", "BLANCO RH 15MM", "JAPANDI RH 36MM", "BLANCO NORMAL"],
        {
            "JAPANDI RH 15MM": 3,
            "BLANCO RH 15MM": 6,
            "JAPANDI RH 36MM": 3,
            "BLANCO NORMAL": 1,
        },
    ),
]


@pytest.mark.parametrize(
    "filename,materials,expected",
    HAND_VERIFIED,
    ids=[f[:34] for f, _, _ in HAND_VERIFIED],
)
def test_reproduces_the_hand_verified_labels(filename, materials, expected):
    result = label(filename, materials)
    assert result.ok, result.reason
    assert result.boards == expected


def test_thickness_is_not_a_quantity():
    """``15MM_`` followed by an underscore.

    ``\\b`` cannot close after ``MM`` there -- ``_`` is a word character -- so a
    boundary-anchored strip leaves ``15`` behind and it reads as fifteen boards.
    This failed on 405 of 485 files when written the obvious way.
    """
    assert [s.quantity for s in segments("9 BLANCOS RH 15MM_03-06-2026.xml")] == [9]
    assert [s.quantity for s in segments("1 BLANCO RH 15M_25-06-2026.xml")] == [1]
    assert [s.quantity for s in segments("6 Y MEDIO FOLIO 5.5MM _10-01-26.xml")] == [
        6.5
    ]


def test_a_later_medio_is_its_own_quantity():
    """A segment must not swallow the ``MEDIO`` that starts the next one."""
    quantities = [
        s.quantity
        for s in segments("1 BLANCO RH 15MM Y MEDIO BLANCO NORMAL 15MM_21-03-26.xml")
    ]
    assert quantities == [1, 0.5]


def test_matches_by_decor_not_by_position():
    """The XML lists pools in part order, which is not the name's order.

    Positional assignment gets this exact file backwards, and got 6 of 48
    multi-material files backwards before the tokens went in.
    """
    result = label(
        "4 BLANCOS RH 15MM_1 CATANIA RH 15MM_COCINA CAGUANA_19-02-2026.xml",
        ["CATANIA", "BLANCO RH"],
    )
    assert result.boards == {"BLANCO RH": 4, "CATANIA": 1}


def test_thickness_separates_two_pools_of_one_decor():
    """``3JAPANDI 36MM`` is the 36mm pool, not a tie with the 15mm one."""
    result = label(
        "2 JAPANDI RH 15MM_3 JAPANDI RH 36MM_04-02-26.xml",
        ["JAPANDI RH 15MM", "JAPANDI RH 36MM"],
    )
    assert result.boards == {"JAPANDI RH 15MM": 2, "JAPANDI RH 36MM": 3}


def test_tolerates_the_shop_s_spelling():
    """Typed at a saw: CASHEMERE, RISTRETO, VISON without the accent."""
    assert label("1 CASHEMERE RH 15MM.xml", ["CASHMERE"]).boards == {"CASHMERE": 1}
    assert label("2 VISÓN RH 15MM.xml", ["VISON RH"]).boards == {"VISON RH": 2}


def test_refuses_a_leftover_job():
    """No whole sheet was consumed, so there is no board count to compare."""
    assert label("CORTE DE SOBRA RIVERA_12-06-26.xml", ["BLANCO RH"]).reason == LEFTOVER


def test_the_leftover_marker_survives_an_underscore():
    """``\bSOBRA\b`` never opens inside ``JARA_SOBRA``: ``_`` is a word character.

    That one miss let the worst file in the corpus through, where ``237X91CM``
    read as 237 boards -- a single fake win of -236 boards on a -250 total.
    """
    result = label(
        "MARCIA JARA_SOBRA DEL TALLER_PANELA RH 36MM_237X91CM_20-04-26.xml",
        ["PANELA"],
    )
    assert result.reason == LEFTOVER


def test_a_measurement_is_not_a_quantity():
    assert segments("PANELA RH 36MM_237X91CM_20-04-26.xml") == []
    assert [s.quantity for s in segments("2 BLANCO RH_2.44X2.15_10-01-26.xml")] == [2]


def test_a_date_written_in_words_is_not_a_quantity():
    assert segments("DARWIN GUTIERREZ_VIENES RH 15MM_14 MAYO 2026.xml") == []
    assert [s.quantity for s in segments("2PLYWOOD 4MM_23 JULIO26.xml")] == [2]


def test_refuses_a_name_that_states_no_quantity():
    assert label("COLORVISON RH 15MM_10-02-26.xml", ["VISON RH"]).reason == NO_QUANTITY


def test_refuses_a_name_that_does_not_describe_the_file():
    """A copy-pasted name: it claims decors the XML does not contain."""
    result = label(
        "1 Y MEDIO BARROCO AMBAR RH 15MM_2 Y MEDI BLANCO RH 15MM_27-01-2026.xml",
        ["CATANIA RH", "BLANCO RH", "BRUNE RH"],
    )
    assert result.reason == COUNT_MISMATCH


def test_refuses_when_a_claim_fits_two_pools():
    result = label(
        "3 BLANCO 15MM_2 BLANCO 15MM.xml",
        ["BLANCO RH", "BLANCO NORMAL"],
    )
    assert result.reason == NO_UNIQUE_MATCH


def test_one_pool_and_one_claim_is_forced():
    """Nothing to disambiguate, so the decors need not match.

    The shop sometimes names a pool with a code instead of a decor; requiring
    the words to line up would throw those away for no gain.
    """
    assert label("2 TABLEROS_10-01-26.xml", ["MDF 9MM"]).boards == {"MDF 9MM": 2}


def test_material_key_folds_the_spellings_of_one_product():
    assert material_key("BLANCO RH") == material_key("BLANCO RH 15MM")
    assert material_key("BLANCO  RH") == material_key("BLANCO RH")
    assert material_key("VISÓN RH 15") == material_key("VISON RH")


def test_a_bare_number_is_a_thickness_only_if_the_vendor_sells_it():
    """The shop writes both, and they must not be read the same way.

    ``"CASHMERE 36"`` is a 36mm sheet with the unit dropped; ``"AMARETTO RH 1"``
    is that decor's first pool. Reading the counter as a 1mm board splits one
    product across several catalog keys and loses every job that used it.
    """
    assert thickness_of("CASHMERE 36") == 36.0
    assert thickness_of("FOLIO 5.5") == 5.5
    assert thickness_of("AMARETTO RH 1") == 15.0
    assert thickness_of("JAPANDI 2") == 15.0
    assert material_key("JAPANDI 2") == material_key("JAPANDI")
    assert material_key("CASHMERE 36") == material_key("CASHMERE 36MM")


def test_material_key_keeps_thickness_apart():
    """Different sheets at different prices, not one product."""
    assert material_key("RISTRETTO 15MM") != material_key("RISTRETTO 36MM")
    assert thickness_of("BARROCO DORADO 36MM") == 36.0
    assert thickness_of("BLANCO RH") == 15.0
    assert thickness_of("FOLIO 5.5MM") == 5.5


def test_clean_name_drops_dates_before_thicknesses():
    """``15-04-26`` must go as a date, or its ``15`` leaves as a thickness."""
    cleaned = clean_name("COCINA ROMEL _ 6 Y MEDIO BLANCO RH 15MM_15-04-26.xml")
    assert "15-04-26" not in cleaned
    assert [
        s.quantity
        for s in segments("COCINA ROMEL _ 6 Y MEDIO BLANCO RH 15MM_15-04-26.xml")
    ] == [6.5]
