"""Unit: the ensamble rule -- the layers of an assembly cut alike and large.

No DB. At the default millimetres (5 large, 1 short per hard side) unless a
test says otherwise. Five things are load-bearing and silent when wrong:

- every layer of an assembly comes out of the saw at the same size, even when
  only one of them carries the hard tape that takes millimetres off;
- a row saved before the rule (a code with no mark) keeps its size, its dump
  and its hash, so no existing quote moves;
- the hash keeps which rows go together, never what they are called;
- a write refuses an assembly whose layers disagree, and a read never does;
- the millimetres are the settings', and a plan keeps the ones it was cut with.
"""

from types import SimpleNamespace

import pytest

from src.modules.optimizations.assembly import (
    Layer,
    assembly_groups,
    assembly_issues,
    assembly_key,
    requirement_cut_sizes,
    requirement_layers,
)
from src.modules.optimizations.cut_rules import (
    LEGACY_CUT_RULES,
    CutRules,
    payload_cut_rules,
    rules_in_play,
)
from src.modules.optimizations.schemas import Requirement
from src.modules.optimizations.service import _requirement_dump, hashable_requirements
from src.modules.optimizations.visualization import (
    _cut_short,
    _enlarged,
    _legend_entries,
)
from src.modules.orders.pieces_export import pieces_csv

_HARD, _SOFT = 1, 2
_TAPES = {
    _HARD: SimpleNamespace(attributes={"bandType": "Hard"}),
    _SOFT: SimpleNamespace(attributes={"bandType": "Soft"}),
}


def _req(code=None, mark=True, sides=None, tape=_HARD, **kw):
    fields = dict(
        materialKey="m", height=600, width=400, quantity=3, priority=0, label="Tapa"
    )
    if code is not None:
        fields.update(assemblyCode=code, assemblyAllowance=mark)
    if sides:
        fields["edgeBanding"] = {"sides": sides, "productId": tape}
    fields.update(kw)
    return Requirement.model_validate(fields)


def _cuts(*reqs, rules=LEGACY_CUT_RULES):
    return requirement_cut_sizes(reqs, _TAPES, rules)


def test_the_default_is_five_millimetres_per_measure():
    assert LEGACY_CUT_RULES.assembly_allowance_mm == 5


def test_the_millimetres_are_the_settings():
    banded = _req("E1", sides=["left", "right"])
    rules = CutRules(hard_edge_cut_mm=2, assembly_allowance_mm=3)
    # 600 + 3 on the largo; 400 + 3 - 2×2 on the ancho, for BOTH layers.
    assert _cuts(banded, _req("E1"), rules=rules) == [(603, 399), (603, 399)]
    # No allowance: the layers still match, the hard discount shared.
    no_allowance = CutRules(hard_edge_cut_mm=1, assembly_allowance_mm=0)
    assert _cuts(banded, _req("E1"), rules=no_allowance) == [(600, 398), (600, 398)]


def test_the_rules_in_play_are_the_ones_some_row_is_under():
    rules = CutRules(hard_edge_cut_mm=2, assembly_allowance_mm=3)
    assert rules_in_play([_req(), _req("E1", mark=False)], _TAPES, rules) == {}
    assert rules_in_play([_req(sides=["left"], tape=_SOFT)], _TAPES, rules) == {}
    assert rules_in_play([_req(sides=["left"])], _TAPES, rules) == {
        "hard_edge_cut_mm": 2
    }
    assert rules_in_play([_req("E1"), _req("E1")], _TAPES, rules) == {
        "assembly_allowance_mm": 3
    }
    # At 0 a rule is still in play: its rows are cut otherwise than at 1.
    zero = CutRules(hard_edge_cut_mm=0, assembly_allowance_mm=0)
    assert rules_in_play([_req("E1", sides=["left"]), _req("E1")], _TAPES, zero) == {
        "hard_edge_cut_mm": 0,
        "assembly_allowance_mm": 0,
    }


def test_a_plan_reads_back_the_rules_it_was_cut_with():
    assert payload_cut_rules({"cut_rules": {"hard_edge_cut_mm": 2}}) == CutRules(
        hard_edge_cut_mm=2, assembly_allowance_mm=5
    )
    # A plan from before the setting, or one neither rule touched.
    assert payload_cut_rules({}) == LEGACY_CUT_RULES


def test_both_layers_take_the_hard_discount_of_the_one_with_the_canto():
    # 2L CD on the first layer: 2 mm off the ANCHO of BOTH, +5 on each measure.
    banded = _req("E1", sides=["left", "right"])
    twin = _req("E1")
    assert _cuts(banded, twin) == [(605, 403), (605, 403)]
    # Order does not matter: the bare layer may come first.
    assert _cuts(twin, banded) == [(605, 403), (605, 403)]


def test_a_short_side_takes_its_millimetre_off_the_largo():
    assert _cuts(_req("E1", sides=["top"]), _req("E1")) == [(604, 405), (604, 405)]


def test_an_assembly_with_no_canto_or_a_soft_one_only_grows():
    assert _cuts(_req("E1"), _req("E1")) == [(605, 405), (605, 405)]
    soft = _req("E1", sides=["left", "right"], tape=_SOFT)
    assert _cuts(soft, _req("E1")) == [(605, 405), (605, 405)]


def test_a_layer_opted_out_of_the_hard_cut_gives_no_discount():
    banded = _req("E1", sides=["left", "right"], hardEdgeCut=False)
    assert _cuts(banded, _req("E1")) == [(605, 405), (605, 405)]


def test_an_assembly_spans_materials():
    banded = _req("E1", sides=["left", "right"], materialKey="melamina")
    backing = _req("E1", materialKey="mdp")
    assert _cuts(banded, backing) == [(605, 403), (605, 403)]


def test_two_banded_layers_read_back_take_the_largest_discount():
    # Refused on a write; a read must still produce a plan, never raise.
    a = _req("E1", sides=["left"])
    b = _req("E1", sides=["left", "right"])
    assert _cuts(a, b) == [(605, 403), (605, 403)]


def test_layers_that_disagree_on_their_size_each_grow_from_their_own():
    a = _req("E1", sides=["left", "right"])
    b = _req("E1", width=398)
    assert _cuts(a, b) == [(605, 403), (605, 401)]


def test_a_row_saved_before_the_rule_keeps_its_own_cut():
    legacy = _req("E1", mark=False, sides=["left", "right"])
    plain = _req(sides=["left", "right"])
    assert _cuts(legacy, _req("E1", mark=False)) == [(600, 398), (600, 400)]
    assert _cuts(plain) == [(600, 398)]


def test_the_mark_without_a_code_is_coerced_off():
    req = Requirement.model_validate(
        dict(
            materialKey="m",
            height=600,
            width=400,
            priority=0,
            assemblyCode="  ",
            assemblyAllowance=True,
        )
    )
    assert req.assembly_code is None
    assert req.assembly_allowance is False


def test_the_code_is_compared_without_spaces_or_case():
    assert assembly_key(" E 1 ") == assembly_key("e1") == "e1"
    assert assembly_key("") is None
    assert assembly_key(None) is None
    assert _cuts(_req("E 1", sides=["left", "right"]), _req("e1")) == [
        (605, 403),
        (605, 403),
    ]


def test_an_unmarked_row_dumps_and_hashes_exactly_as_before():
    legacy = _req("E1", mark=False, sides=["left", "right"])
    assert "assembly_allowance" not in _requirement_dump(legacy)
    assert hashable_requirements([legacy]) == [_requirement_dump(legacy)]
    assert "assembly_group" not in hashable_requirements([legacy])[0]


def test_a_marked_row_carries_its_mark_and_its_group():
    dumped = _requirement_dump(_req("E1"))
    assert dumped["assembly_allowance"] is True
    # The code itself never: it is still a workshop code.
    assert "assembly_code" not in dumped
    hashed = hashable_requirements([_req("E2"), _req(), _req("E1"), _req("E2")])
    assert [h.get("assembly_group") for h in hashed] == [0, None, 1, 0]


def test_renaming_an_assembly_keeps_the_hash_and_regrouping_moves_it():
    before = hashable_requirements([_req("E1"), _req("E1"), _req("E2"), _req("E2")])
    renamed = hashable_requirements([_req("A"), _req("A"), _req("B"), _req("B")])
    regrouped = hashable_requirements([_req("E1"), _req("E2"), _req("E1"), _req("E2")])
    assert renamed == before
    assert regrouped != before


def test_groups_are_indexed_by_first_appearance():
    layers = [
        Layer(key=k, final=(1, 1), cut=(1, 1), quantity=1, banded=False)
        for k in ("b", None, "a", "b")
    ]
    assert assembly_groups(layers) == [0, None, 1, 0]


def _issues(*reqs):
    return assembly_issues(requirement_layers(reqs))


def test_a_sound_assembly_has_no_issue():
    assert _issues(_req("E1", sides=["left", "right"]), _req("E1")) == []
    # Rows outside the rule are never judged.
    assert _issues(_req("E1", mark=False), _req()) == []


@pytest.mark.parametrize(
    "rows, message",
    [
        ([_req("E1")], "Ens E1: tiene una sola capa"),
        (
            [_req("E1"), _req("E1", width=398)],
            "Ens E1: las capas no tienen la misma medida (600×400 / 600×398)",
        ),
        (
            [_req("E1"), _req("E1", quantity=2)],
            "Ens E1: las capas no tienen la misma cantidad (3 / 2)",
        ),
        (
            [_req("E1", sides=["left"]), _req("E1", sides=["top"])],
            "Ens E1: el canto va en una sola capa y lo tienen 2",
        ),
        (
            # A canto especial counts as a canto.
            [
                _req("E1", sides=["left"]),
                _req("E1", specialEdges=[{"side": "top", "productId": _SOFT}]),
            ],
            "Ens E1: el canto va en una sola capa y lo tienen 2",
        ),
    ],
)
def test_each_problem_is_named(rows, message):
    [issue] = _issues(*rows)
    assert issue.startswith(message)


def test_the_diagram_legend_tells_an_assembly_from_a_hard_tape():
    def placed(h, w, oh, ow):
        return {"height": h, "width": w, "original_height": oh, "original_width": ow}

    # 605×405: grown, nothing taken off.
    assert _enlarged(placed(605, 405, 600, 400))
    assert not _cut_short(placed(605, 405, 600, 400), 5)
    # 605×403: grown, and a hard tape ate 2 mm of the allowance.
    assert _enlarged(placed(605, 403, 600, 400))
    assert _cut_short(placed(605, 403, 600, 400), 5)
    # A plain hard tape is short and not grown.
    assert _cut_short(placed(600, 398, 600, 400), 5)
    assert not _enlarged(placed(600, 398, 600, 400))
    # Against the plan's own allowance: grown by 3 of 3 is not short.
    assert not _cut_short(placed(603, 403, 600, 400), 3)

    texts = [e[3] for e in _legend_entries(set(), enlarged=True)]
    assert "Ensamble (+5 mm por medida para rectificar)" in texts
    assert not any("Ensamble" in t for t in [e[3] for e in _legend_entries(set())])
    rules = CutRules(hard_edge_cut_mm=1, assembly_allowance_mm=3)
    texts = [e[3] for e in _legend_entries(set(), enlarged=True, rules=rules)]
    assert "Ensamble (+3 mm por medida para rectificar)" in texts


def _piece(id, **kw):
    fields = dict(
        id=id,
        product_name="Melamina",
        product_code="MEL",
        label="Tapa",
        height=600,
        width=400,
        quantity=3,
        can_rotate=True,
        hard_edge_cut=True,
        edges=None,
        hinging_code=None,
        grooving_code=None,
        assembly_code=None,
        division_code=None,
        assembly_allowance=False,
    )
    fields.update(kw)
    return SimpleNamespace(**fields)


def test_the_export_cuts_every_frozen_layer_alike():
    hard = {"sides": ["left", "right"], "band_type": "Hard", "alias": "BLN"}
    pieces = [
        _piece(1, edges=hard, assembly_code="E1", assembly_allowance=True),
        _piece(2, assembly_code="E1", assembly_allowance=True),
        # Frozen before the rule: its code moves nothing.
        _piece(3, edges=hard, assembly_code="E2"),
    ]

    def sizes(rules):
        rows = pieces_csv(pieces, rules).decode("utf-8-sig").splitlines()[1:]
        return [tuple(r.split(",")[1:3]) for r in rows]

    assert sizes(LEGACY_CUT_RULES) == [("605", "403"), ("605", "403"), ("600", "398")]
    # The order's own millimetres (its snapshot's), whatever they were.
    rules = CutRules(hard_edge_cut_mm=2, assembly_allowance_mm=3)
    assert sizes(rules) == [("603", "399"), ("603", "399"), ("600", "396")]


def test_a_layer_too_large_for_the_board_says_it_grew():
    from src.modules.optimizations.unplaced import unplaced_message

    entry = {
        "material_key": "m",
        "label": "Tapa",
        "quantity": 1,
        "height": 2440,
        "width": 400,
        "cut_height": 2445,
        "cut_width": 405,
        "material_name": "Melamina",
        "usable_height": 2420,
        "usable_width": 1200,
        "reason": "larger_than_sheet",
    }
    assert "1 «Tapa» de 2440×400 mm (se corta a 2445×405 mm por el ensamble)" in (
        unplaced_message([entry])
    )
    # A hard tape only takes off: its message is the one it always was.
    short = {**entry, "cut_height": 2438, "cut_width": 400}
    assert "de 2440×400 mm en Melamina" in unplaced_message([short])


def test_the_document_draws_the_legend_with_the_plans_own_millimetres():
    from src.modules.optimizations.carrier import DocumentCarrier

    payload = {"cut_rules": {"hard_edge_cut_mm": 2, "assembly_allowance_mm": 3}}
    carrier = DocumentCarrier.from_payload(payload, client=None, reference="ORD-1")
    assert carrier.cut_rules == CutRules(hard_edge_cut_mm=2, assembly_allowance_mm=3)
    assert (
        DocumentCarrier.from_payload({}, client=None, reference="ORD-1").cut_rules
        == LEGACY_CUT_RULES
    )
