"""Unit: Ecuadorian cédula/RUC validation (no DB, no MySQL).

The valid numbers below are real cédulas/RUCs taken from the vendor's live
client table, and the invalid ones are the five that table actually holds — so
this suite pins the exact split the sync will see, not a synthetic one.
"""

import pytest

from src.modules.clients.tax_id import identifier_error, is_company_ruc, tax_id_error

# Real cédulas (10 digits), one per province, spanning Azuay to Morona.
VALID_CEDULAS = [
    "0105929863",
    "0201434537",
    "0301137915",
    "0501441604",
    "0600642219",
    "0704016500",
    "1400171417",
]

# Real RUCs: natural person (3rd digit < 6), public entity (6), company (9).
VALID_NATURAL_RUCS = ["0707015046001", "1400768998001"]
VALID_PUBLIC_RUCS = ["1460000880001"]
VALID_PRIVATE_RUCS = ["1790941450001", "1490820368001"]


@pytest.mark.parametrize("value", VALID_CEDULAS)
def test_real_cedulas_pass(value):
    assert tax_id_error(value) is None


@pytest.mark.parametrize(
    "value", VALID_NATURAL_RUCS + VALID_PUBLIC_RUCS + VALID_PRIVATE_RUCS
)
def test_real_rucs_pass(value):
    assert tax_id_error(value) is None


def test_province_30_is_accepted():
    """Ecuadorians registered abroad get province 30, outside the 1-24 range.

    The vendor's table has none, so this is built rather than sampled: take a
    real cédula's body and swap the province, then repair the check digit with
    the same algorithm the validator uses. What is asserted is that 30 is not
    rejected *as a province* — 99 (the "Consumidor final" row) is.
    """
    body = "30" + "05929863"[:-1]
    accepted = [d for d in "0123456789" if tax_id_error(body + d) is None]
    assert len(accepted) == 1, "exactly one check digit closes a cédula"
    assert tax_id_error("99" + "05929863") is not None


@pytest.mark.parametrize(
    "value,reason",
    [
        # The five the live table holds, with the reason each one fails.
        ("9999999999", "province"),  # "Consumidor final"
        ("85230421", "length"),  # 8 digits
        ("1400048947", "check digit"),
        ("1498310921001", "check digit"),
        ("1793224079001", "check digit"),
    ],
)
def test_the_rows_sifac_actually_holds_are_rejected(value, reason):
    assert tax_id_error(value) is not None, reason


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "010203040",  # 9 digits: a mistyped cédula
        "14001714170",  # 11 digits
        "AB123456",  # letters
        "1400171417A",
        "14.001.714-17",
    ],
)
def test_anything_that_is_not_ten_or_thirteen_digits_is_rejected(value):
    assert tax_id_error(value) is not None


def test_a_wrong_check_digit_is_caught():
    """The whole point: a mistyped cédula passes any length-only rule."""
    good = "1400171417"
    bad = good[:-1] + str((int(good[-1]) + 1) % 10)
    assert tax_id_error(good) is None
    assert tax_id_error(bad) is not None


@pytest.mark.parametrize("value", VALID_PUBLIC_RUCS + VALID_PRIVATE_RUCS)
def test_company_rucs_are_recognized_as_companies(value):
    assert is_company_ruc(value) is True


@pytest.mark.parametrize("value", VALID_CEDULAS + VALID_NATURAL_RUCS)
def test_people_are_not_companies(value):
    """A natural person's RUC is 13 digits too — only the 3rd digit tells them
    apart, which is why length alone can't decide whether to split a name."""
    assert is_company_ruc(value) is False


@pytest.mark.parametrize("value", ["", "AB123456", "no-es-un-ruc"])
def test_non_numeric_is_never_a_company(value):
    assert is_company_ruc(value) is False


class TestIdentifierError:
    """The rule the dashboard form and the API enforce."""

    @pytest.mark.parametrize("value", ["AB123456", "P1234567", "X-99/8"])
    def test_a_document_with_letters_is_accepted_as_a_passport(self, value):
        assert identifier_error(value) is None

    @pytest.mark.parametrize("value", ["010203040", "9999999999", "85230421"])
    def test_an_all_digits_document_is_held_to_the_full_rule(self, value):
        """Nine digits is a mistyped cédula, not a passport."""
        assert identifier_error(value) is not None

    def test_empty_is_left_to_the_field_constraint(self):
        """``min_length=1`` already rejects it; a second message would be noise."""
        assert identifier_error("") is None
