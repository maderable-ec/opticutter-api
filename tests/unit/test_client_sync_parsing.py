"""Unit: the client sync's normalization rules (no DB, no MySQL).

The vendor keeps one free-text name per client, phones typed however they were
typed, and e-mails that sometimes lost their ``@``. Each rule here exists
because of a shape measured in the live table, and the counts in the docstrings
are from that measurement (798 active rows).
"""

import pytest

from src.modules.clients.client_sync import (
    _validate,
    normalize_email,
    normalize_phone,
    split_name,
)
from src.modules.clients.external_clients import ClientSourceRow

# A real cédula and a real company RUC, so the split rules run against numbers
# the validator actually accepts.
CEDULA = "1400171417"
COMPANY_RUC = "1790941450001"
NATURAL_RUC = "1400768998001"


def _row(row_no=1, cedula=CEDULA, nombre="JUAN PEREZ", email="", tel="", tel2=""):
    return ClientSourceRow(
        row_no=row_no,
        cedula=cedula,
        nombre=nombre,
        email=email,
        telefono=tel,
        telefono2=tel2,
    )


class TestSplitName:
    @pytest.mark.parametrize(
        "nombre,first,last",
        [
            # 290 of the 798 live rows have this shape: two names, two surnames.
            ("JHOANA PATRICIA GUAMAN VAZQUEZ", "JHOANA PATRICIA", "GUAMAN VAZQUEZ"),
            # 410 rows: one name, one surname.
            ("MAURICIO DELGADO", "MAURICIO", "DELGADO"),
            # 90 rows: one name, two surnames.
            ("JOSE GUAMAN LOPEZ", "JOSE", "GUAMAN LOPEZ"),
            # Longer ones still keep exactly two surnames.
            (
                "MARIA DEL CARMEN DE LA TORRE VEGA",
                "MARIA DEL CARMEN DE LA",
                "TORRE VEGA",
            ),
        ],
    )
    def test_a_person_keeps_the_last_two_words_as_surnames(self, nombre, first, last):
        assert split_name(nombre, CEDULA) == (first, last)

    def test_a_company_is_never_split(self):
        """An organization has no surname, and only the RUC's 3rd digit says so."""
        assert split_name("ALMACENES MONTANO SAS", COMPANY_RUC) == (
            "ALMACENES MONTANO SAS",
            "",
        )

    def test_a_natural_person_with_a_ruc_is_still_split(self):
        """13 digits alone means nothing: 277 of the live rows are people."""
        assert split_name("JOHN STEVEN SALLO GUAMAN", NATURAL_RUC) == (
            "JOHN STEVEN",
            "SALLO GUAMAN",
        )

    def test_a_single_word_stays_whole(self):
        assert split_name("CONSTRUMAX", CEDULA) == ("CONSTRUMAX", "")

    def test_empty_gives_two_empties(self):
        assert split_name("", CEDULA) == ("", "")

    def test_double_spaces_do_not_create_empty_words(self):
        """`MANUEL CHILLOGALLO  ORDONEZ` is a real row: two spaces, three words."""
        assert split_name("MANUEL CHILLOGALLO  ORDONEZ", CEDULA) == (
            "MANUEL",
            "CHILLOGALLO ORDONEZ",
        )


class TestNormalizePhone:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("0981105628", "0981105628"),
            # Real shapes from the table: the same number written five ways.
            ("099 109 5477", "0991095477"),
            ("0996 920 0139", "09969200139"),
            ("099461 6578", "0994616578"),
            ("(07) 274-0486", "072740486"),
        ],
    )
    def test_separators_are_dropped(self, raw, expected):
        """A number differing only in spacing would look like a change on every
        re-sync, and would never match what the shop dials."""
        assert normalize_phone(raw, "") == expected

    @pytest.mark.parametrize("raw", ["N/A", "-", "0", "", "   ", "123"])
    def test_a_placeholder_is_not_a_phone(self, raw):
        """`require_phone` only checks for a non-blank value, so storing "N/A"
        would sail through the gate that stops a proforma with no contact."""
        assert normalize_phone(raw, "") is None

    def test_tel2_is_a_fallback_not_a_second_number(self):
        assert normalize_phone("", "0987654321") == "0987654321"
        assert normalize_phone("0981105628", "0987654321") == "0981105628"

    def test_it_is_capped_to_the_column_width(self):
        assert len(normalize_phone("9" * 60, "")) == 32


class TestNormalizeEmail:
    def test_it_is_lowercased(self):
        assert normalize_email("  Juan.Perez@Gmail.COM ") == "juan.perez@gmail.com"

    @pytest.mark.parametrize(
        "raw",
        [
            # The three live rows that lost their `@`.
            "msucua.sucua.gob.ec",
            "nelusata04gmail.com",
            "grafimac989hotmail.com",
            "@gmail.com",
            "juan@",
        ],
    )
    def test_something_that_is_not_an_address_becomes_nothing(self, raw):
        """A plausible-looking wrong address gets used; a blank gets chased."""
        assert normalize_email(raw) is None

    def test_blank_is_none(self):
        assert normalize_email("   ") is None


class TestValidate:
    def test_a_good_row_imports(self):
        valid, issues, warnings = _validate(
            [_row(nombre="JHOANA PATRICIA GUAMAN VAZQUEZ", tel="0981105628")]
        )
        assert issues == [] and warnings == []
        assert valid[0].first_name == "JHOANA PATRICIA"
        assert valid[0].last_name == "GUAMAN VAZQUEZ"
        assert valid[0].phone == "0981105628"

    @pytest.mark.parametrize(
        "cedula,nombre",
        [
            ("9999999999", "Consumidor final"),
            ("85230421", "FELIX SEGUNDO POLO PARDO"),
            ("1400048947", "PAOLA KAJEKAI"),
            ("", "SIN CEDULA"),
        ],
    )
    def test_an_unusable_cedula_skips_the_row_and_reports_it(self, cedula, nombre):
        valid, issues, _ = _validate([_row(cedula=cedula, nombre=nombre)])
        assert valid == []
        assert len(issues) == 1
        # The report has to carry what is searched for in the external system.
        assert issues[0].name == nombre

    def test_a_row_with_no_name_is_skipped(self):
        valid, issues, _ = _validate([_row(nombre="   ")])
        assert valid == []
        assert "nombre" in issues[0].message

    def test_a_duplicate_cedula_is_skipped_not_silently_overwritten(self):
        """The upsert keys on the cédula: the second row would look like an
        update nobody made."""
        valid, issues, _ = _validate(
            [_row(row_no=1, nombre="JUAN PEREZ"), _row(row_no=2, nombre="OTRO NOMBRE")]
        )
        assert len(valid) == 1
        assert valid[0].first_name == "JUAN"
        assert "fila 1" in issues[0].message

    def test_a_bad_phone_warns_but_still_imports_the_client(self):
        valid, issues, warnings = _validate([_row(tel="N/A")])
        assert len(valid) == 1 and valid[0].phone == ""
        assert issues == []
        assert len(warnings) == 1 and "teléfono" in warnings[0].message

    def test_a_bad_email_warns_but_still_imports_the_client(self):
        valid, issues, warnings = _validate([_row(email="nelusata04gmail.com")])
        assert len(valid) == 1 and valid[0].email == ""
        assert issues == []
        assert len(warnings) == 1 and "correo" in warnings[0].message

    def test_an_empty_phone_column_is_not_a_warning(self):
        """67 live rows simply have no phone. That is missing data, not bad data."""
        _, issues, warnings = _validate([_row(tel="", tel2="")])
        assert issues == [] and warnings == []

    def test_a_name_longer_than_the_column_is_trimmed_not_skipped(self):
        long_name = "GOBIERNO AUTONOMO DESCENTRALIZADO MUNICIPAL DEL CANTON " + "X" * 40
        valid, issues, warnings = _validate(
            [_row(cedula=COMPANY_RUC, nombre=long_name)]
        )
        assert issues == []
        assert len(valid[0].first_name) == 64
        assert len(warnings) == 1

    def test_a_cedula_longer_than_the_column_is_skipped(self):
        valid, issues, _ = _validate([_row(cedula="1" * 40)])
        assert valid == [] and len(issues) == 1
