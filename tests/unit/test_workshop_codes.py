"""Unit: the workshop codes of the cut list (abisagrado, ranurado, ensamble, división).

No DB. The codes are production data the seller types on the cut list, and four
things about them are load-bearing and silent when wrong:

- they never reach the optimization hash, and never as a ``null`` key either --
  that would change the canonical JSON of every quote and empty Redis on deploy;
- they never reach the cached payload, or a cache hit would hand back the codes
  of whichever request computed it;
- the order puts them back from its own request and hangs them on each placed
  piece by the SAME naming rule the optimizer used, per material group and
  through a pooled retazo's anchor;
- they, and not the billed services, are what make an order carry the
  ``additional`` activity.
"""

import pytest
from pydantic import ValidationError
from reportlab.lib.styles import getSampleStyleSheet

from src.modules.optimizations.carrier import DocumentCarrier
from src.modules.optimizations.documents import DocumentService
from src.modules.optimizations.labels import _WORKSHOP_CODE_ABBR, workshop_codes_line
from src.modules.optimizations.schemas import WORKSHOP_CODE_FIELDS, Requirement
from src.modules.optimizations.service import (
    OptimizationService,
    hashable_requirements,
)
from src.modules.orders.model import ACTIVITY_PIECES, ActivityType, OrderModel
from src.modules.orders.service import (
    _PIECE_IN_SET,
    _PIECE_SET_FILTERS,
    _PIECE_SET_QUALIFIER,
    _attach_cutting_plan,
    _build_activities,
    _with_workshop_codes,
    _workshop_codes_by_instance,
)


def _req(**overrides) -> Requirement:
    data = {
        "priority": 0,
        "height": 700,
        "width": 400,
        "quantity": 1,
        "material_key": "b1",
        "label": "Puerta",
    }
    data.update(overrides)
    return Requirement(**data)


def _dumped(material_key="b1", label="Puerta", quantity=1, **codes) -> dict:
    return {
        "material_key": material_key,
        "label": label,
        "quantity": quantity,
        **{f: codes.get(f) for f in WORKSHOP_CODE_FIELDS},
    }


# --------------------------------------------------------------------------- #
# The contract
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("blank", ["", "   ", None])
def test_a_blank_code_is_none(blank):
    assert _req(hinging_code=blank).hinging_code is None


def test_a_code_is_stripped_before_its_length_counts():
    code = "X" * 32
    assert _req(grooving_code=f"  {code}  ").grooving_code == code


def test_a_code_is_capped_at_32():
    with pytest.raises(ValidationError):
        _req(assembly_code="X" * 33)


def test_the_codes_travel_in_camel_case():
    req = Requirement.model_validate(
        {
            "priority": 0,
            "height": 700,
            "width": 400,
            "materialKey": "b1",
            "hingingCode": "B2",
            "groovingCode": "R3",
            "assemblyCode": "E1",
            "divisionCode": "D4",
        }
    )
    assert (
        req.hinging_code,
        req.grooving_code,
        req.assembly_code,
        req.division_code,
    ) == ("B2", "R3", "E1", "D4")


# --------------------------------------------------------------------------- #
# Out of the hash, out of the cache
# --------------------------------------------------------------------------- #
def test_the_codes_never_reach_the_hash():
    with_codes = hashable_requirements([_req(hinging_code="B2", grooving_code="R1")])
    without_codes = hashable_requirements([_req()])
    assert with_codes == without_codes
    # Excluded, not emitted as null: the dump is exactly what it was before the
    # fields existed, so every Redis entry survives the deploy.
    legacy = _req().model_dump(mode="json")
    for field in WORKSHOP_CODE_FIELDS:
        legacy.pop(field)
        assert field not in with_codes[0]
    assert with_codes == [legacy]


def test_the_codes_never_reach_the_cached_payload():
    data = OptimizationService._dump_requirement(_req(assembly_code="E1"), {}, {})
    for field in WORKSHOP_CODE_FIELDS:
        assert field not in data


def test_the_order_puts_the_codes_back_by_position_without_mutating():
    payload_reqs = [{"material_key": "b1", "label": "A"}, {"material_key": "b1"}]
    request = [_req(label="A", hinging_code="B2"), _req(label=None)]
    out = _with_workshop_codes(payload_reqs, request)
    assert out[0]["hinging_code"] == "B2"
    assert out[1]["hinging_code"] is None
    assert "hinging_code" not in payload_reqs[0]


def test_the_positional_pairing_refuses_a_count_mismatch():
    with pytest.raises(ValueError):
        _with_workshop_codes([{"material_key": "b1"}], [_req(), _req()])


# --------------------------------------------------------------------------- #
# Placed pieces: the same names the optimizer gave them
# --------------------------------------------------------------------------- #
def test_codes_follow_the_instance_names_within_a_group():
    codes = _workshop_codes_by_instance(
        [
            _dumped(label="Puerta", quantity=2, hinging_code="B2"),
            _dumped(label="Lateral"),
            _dumped(label="Puerta", grooving_code="R1"),
        ]
    )
    assert codes[("b1", "Puerta#1")]["hinging_code"] == "B2"
    assert codes[("b1", "Puerta#2")]["hinging_code"] == "B2"
    assert codes[("b1", "Puerta#3")] == {
        "hinging_code": None,
        "grooving_code": "R1",
        "assembly_code": None,
        "division_code": None,
    }
    # A piece without codes is simply absent: nothing to hang on it.
    assert ("b1", "Lateral") not in codes


def test_two_materials_can_cut_the_same_label():
    codes = _workshop_codes_by_instance(
        [
            _dumped(material_key="b1", label="Puerta", hinging_code="B2"),
            _dumped(material_key="b2", label="Puerta"),
        ]
    )
    assert ("b1", "Puerta") in codes
    assert ("b2", "Puerta") not in codes


def _layout(material_key, *piece_ids):
    return {
        "material": {"material_key": material_key, "width": 2440, "height": 2070},
        "placed_pieces": [
            {"piece_id": pid, "x": 0, "y": 0, "width": 400, "height": 700}
            for pid in piece_ids
        ],
    }


def test_a_piece_cut_on_a_pooled_retazo_takes_its_anchors_codes():
    payload = {
        "materials": [{"material_key": "b1"}, {"material_key": "off1"}],
        "requirements": [_dumped(label="Puerta", quantity=2, assembly_code="E1")],
        "layouts": [_layout("b1", "Puerta#1"), _layout("off1", "Puerta#2")],
    }
    order = OrderModel()
    _attach_cutting_plan(order, payload, anchor_of={"b1": "b1", "off1": "b1"})
    pieces = [p for board in order.boards for p in board.pieces]
    assert [p.assembly_code for p in pieces] == ["E1", "E1"]


def test_without_anchors_every_material_is_its_own_group():
    """The lazy path: exact for any job with no pool."""
    payload = {
        "materials": [{"material_key": "b1"}],
        "requirements": [
            _dumped(label="Puerta", hinging_code="B2"),
            _dumped("b1", "Z"),
        ],
        "layouts": [_layout("b1", "Puerta", "Z")],
    }
    order = OrderModel()
    _attach_cutting_plan(order, payload)
    by_id = {p.piece_id: p for p in order.boards[0].pieces}
    assert by_id["Puerta"].hinging_code == "B2"
    assert by_id["Z"].hinging_code is None


# --------------------------------------------------------------------------- #
# The additional activity
# --------------------------------------------------------------------------- #
def _types(snapshot) -> list:
    return [a.type for a in _build_activities(snapshot)]


def test_a_billed_service_is_not_work_for_the_shop():
    snapshot = {
        "requirements": [_dumped()],
        "additional_services": [{"name": "Perforación", "quantity": 4}],
    }
    assert ActivityType.additional.value not in _types(snapshot)


def test_one_code_on_one_piece_is_enough():
    snapshot = {"requirements": [_dumped(label="A"), _dumped(grooving_code="R1")]}
    assert ActivityType.additional.value in _types(snapshot)


def test_a_division_alone_is_enough():
    snapshot = {"requirements": [_dumped(division_code="D1")]}
    assert ActivityType.additional.value in _types(snapshot)


def test_every_activity_names_a_complete_piece_set():
    for activity in ActivityType:
        piece_set = ACTIVITY_PIECES[activity]
        assert piece_set in _PIECE_SET_FILTERS
        assert piece_set in _PIECE_IN_SET
        assert piece_set in _PIECE_SET_QUALIFIER
    assert ACTIVITY_PIECES[ActivityType.additional] == "worked"


# --------------------------------------------------------------------------- #
# How they print
# --------------------------------------------------------------------------- #
def test_the_line_names_each_service_and_skips_the_missing():
    assert workshop_codes_line({"hinging_code": "B2", "grooving_code": "R1"}) == (
        "Abis B2 · Ran R1"
    )
    assert workshop_codes_line({"assembly_code": None}) == ""


def test_the_line_reads_abisagrado_ranurado_ensamble_division():
    codes = {
        "division_code": "D1",
        "assembly_code": "E1",
        "grooving_code": "R1",
        "hinging_code": "B1",
    }
    assert workshop_codes_line(codes) == "Abis B1 · Ran R1 · Ens E1 · Div D1"


def test_the_printed_names_cover_every_code_in_order():
    """One list decides the order; the abbreviations must not drift from it."""
    assert [field for field, _ in _WORKSHOP_CODE_ABBR] == list(WORKSHOP_CODE_FIELDS)


_CELL = getSampleStyleSheet()["BodyText"]


def _table(*requirements):
    carrier = DocumentCarrier(
        reference="ORD-2026-0001", client=None, requirements=list(requirements)
    )
    return DocumentService._build_requirements_table(carrier, _CELL)


def _req_row(**codes):
    return {"height": 700, "width": 400, "quantity": 1, "product_code": "MEL", **codes}


def test_an_order_without_codes_prints_the_table_it_always_did():
    table = _table(_req_row())
    assert table._cellvalues[0] == [
        "#",
        "Alto",
        "Ancho",
        "Cant.",
        "Material",
        "Cantos",
        "Etiqueta",
    ]


def test_a_code_adds_the_taller_column_escaped():
    table = _table(_req_row(hinging_code="<B2>"), _req_row())
    header = table._cellvalues[0]
    assert header[6] == "Taller"
    assert header[-1] == "Etiqueta"
    assert "&lt;B2&gt;" in table._cellvalues[1][6].text
    assert table._cellvalues[2][6].text == "-"
