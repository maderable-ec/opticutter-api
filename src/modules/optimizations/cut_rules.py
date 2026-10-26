"""How many millimetres the two shop rules move a piece's cut off its final size.

A hard tape cuts its side short (``hard_edges``) and every layer of an assembly
is cut large, to be squared once it is glued (``assembly``). How much each one
takes is the shop's call, so both are settings (``settings.hard_edge_cut_mm``
and ``settings.assembly_allowance_mm``, under the cutting parameters) and a
computation reads them once (``OptimizationService._prepare``).

A plan says which values it was cut with: ``cut_rules`` in its payload, holding
the rules in play -- the very dict the optimization hash keys on -- so the
PDF's legend and the order's export read the plan's own values, never the
settings of the day: a document never reads the settings, and an order keeps
cutting what it froze.

Pure: no DB, no framework.
"""

from dataclasses import dataclass
from typing import Mapping, Sequence

from src.modules.optimizations.hard_edges import hard_edge_rule_applies
from src.modules.optimizations.schemas import Requirement
from src.modules.products.model import ProductModel


@dataclass(frozen=True)
class CutRules:
    # Millimetres the saw cuts short per side with a hard tape.
    hard_edge_cut_mm: int
    # Millimetres every layer of an assembly is cut large, on each measure.
    assembly_allowance_mm: int


# What every plan was cut with before the two became settings (2026-10-27), and
# so what a payload with no ``cut_rules`` reads as: one computed before, or one
# no rule touched, where the values move nothing.
LEGACY_CUT_RULES = CutRules(hard_edge_cut_mm=1, assembly_allowance_mm=5)


def rules_in_play(
    requirements: Sequence[Requirement],
    eb_products: Mapping[int, ProductModel],
    rules: CutRules,
) -> dict:
    """The rules some row of the cut list is under, with their values.

    What the hash keys on and the payload keeps as ``cut_rules``. Empty for a
    cut list neither rule touches, which is what keeps the canonical JSON (and
    the payload) of every such quote byte-identical whatever the settings say.
    A rule set to 0 is still in play: its rows are cut at another size than
    the default would cut them.
    """
    out: dict = {}
    if any(hard_edge_rule_applies(r, eb_products) for r in requirements):
        out["hard_edge_cut_mm"] = rules.hard_edge_cut_mm
    if any(r.assembly_allowance for r in requirements):
        out["assembly_allowance_mm"] = rules.assembly_allowance_mm
    return out


def payload_cut_rules(payload: Mapping) -> CutRules:
    """The rules a plan was cut with, read off its payload (or an order's
    snapshot). A value it does not carry is one no row of it was under."""
    stored = payload.get("cut_rules") or {}
    return CutRules(
        hard_edge_cut_mm=stored.get(
            "hard_edge_cut_mm", LEGACY_CUT_RULES.hard_edge_cut_mm
        ),
        assembly_allowance_mm=stored.get(
            "assembly_allowance_mm", LEGACY_CUT_RULES.assembly_allowance_mm
        ),
    )
