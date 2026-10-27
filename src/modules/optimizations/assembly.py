"""Ensamble: the layers of one assembled piece, cut alike and large to be squared.

An assembled piece (two or more boards glued face to face, banded once it is
whole) is typed as one cut-list row per layer, and the rows that share an
``assembly_code`` are one assembly. The shop cuts every layer large on each
measure to square the glued block afterwards -- 5 mm by default, a setting
(``settings.assembly_allowance_mm``, see ``cut_rules``) -- so the seller still
types the FINAL size and this module is where it grows.

The canto goes on the assembled piece, so the seller puts it on ONE row (the
metres are billed once) -- yet a hard tape still takes its millimetre off every
layer, or the layers would come out of the saw at different sizes. Each
assembly therefore takes, per axis, the largest discount any of its rows gets
from ``hard_edges`` and every layer is cut ``final + allowance - that``: at the
defaults, 600×400 with ``2L CD`` and its bare twin are both cut 605×403.

Only rows marked ``assembly_allowance`` follow the rule. The web marks every
row whose Ens code is typed from now on; a quote saved before carries codes
with no mark and keeps its plan. The rows of one assembly are named by their
code normalized (``assembly_key``), so ``E 1`` and ``e1`` are the same one.

Pure: no DB, no framework. ``Layer`` is what a row is to this rule; the live
requirement has its adapter here, and an order's frozen pieces build theirs in
``orders/pieces_export.py``.
"""

from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.modules.optimizations.cut_rules import CutRules
from src.modules.optimizations.hard_edges import requirement_cut_size
from src.modules.optimizations.schemas import Requirement
from src.modules.products.model import ProductModel
from src.shared.exceptions import BusinessRuleError

Size = Tuple[int, int]


class AssemblyMismatchError(BusinessRuleError):
    """A write whose assemblies do not hold together. Its own code so a client
    can tell it from any other 422."""

    code = "ASSEMBLY_MISMATCH"


def assembly_key(code: Optional[str]) -> Optional[str]:
    """The code as the rule compares it: no whitespace, casefolded."""
    key = "".join((code or "").split()).casefold()
    return key or None


@dataclass(frozen=True)
class Layer:
    """One cut-list row as the assembly rule sees it."""

    # ``assembly_key`` of the row's code, or ``None`` when the row is not under
    # the rule (no code, or a code saved before the mark existed).
    key: Optional[str]
    # ``(height, width)`` the seller typed.
    final: Size
    # ``(height, width)`` the row is cut at on its own (``hard_edges``).
    cut: Size
    quantity: int
    # Carries a tape on some side (the Canto or a canto especial).
    banded: bool
    # The code as typed, to name the assembly in a message.
    code: Optional[str] = None


def requirement_layers(
    requirements: Iterable[Requirement],
    own_cuts: Optional[Sequence[Size]] = None,
) -> List[Layer]:
    """The layers of a live cut list, in its order.

    ``own_cuts``, parallel, is each row's cut on its own (``hard_edges``).
    Without it every row's own cut is its final size: enough for
    ``assembly_issues`` and ``assembly_groups``, which never read the cut.
    """
    requirements = list(requirements)
    if own_cuts is None:
        own_cuts = [(r.height, r.width) for r in requirements]
    return [
        Layer(
            key=assembly_key(r.assembly_code) if r.assembly_allowance else None,
            final=(r.height, r.width),
            cut=cut,
            quantity=r.quantity,
            banded=bool(r.side_products()),
            code=r.assembly_code,
        )
        for r, cut in zip(requirements, own_cuts)
    ]


def requirement_cut_sizes(
    requirements: Sequence[Requirement],
    eb_products: Mapping[int, ProductModel],
    rules: CutRules,
) -> List[Size]:
    """``(height, width)`` the saw cuts for each row of a live cut list.

    Each row's hard tapes first, then the assembly rule over the WHOLE list,
    since an assembly may span materials.
    """
    own = [
        requirement_cut_size(r, eb_products, rules.hard_edge_cut_mm)
        for r in requirements
    ]
    return assembly_cut_sizes(
        requirement_layers(requirements, own), rules.assembly_allowance_mm
    )


def _group_discounts(layers: Sequence[Layer]) -> Dict[str, Size]:
    """``{key: (dh, dw)}``: per axis, the largest discount (most negative
    ``cut - final``) any row of the assembly gets from its hard tapes."""
    discounts: Dict[str, Size] = {}
    for layer in layers:
        if layer.key is None:
            continue
        dh = layer.cut[0] - layer.final[0]
        dw = layer.cut[1] - layer.final[1]
        h0, w0 = discounts.get(layer.key, (0, 0))
        discounts[layer.key] = (min(h0, dh), min(w0, dw))
    return discounts


def assembly_cut_sizes(layers: Sequence[Layer], allowance_mm: int) -> List[Size]:
    """``(height, width)`` the saw cuts for each row, parallel to ``layers``.

    A row outside the rule is cut at its own size. A row of an assembly is cut
    at its final size plus ``allowance_mm`` (``settings.assembly_allowance_mm``)
    on each measure, less the assembly's discount. Never
    raises: rows that disagree on their final size (a quote read back, which
    must not fail -- see ``assembly_issues`` for the writes) each grow from
    their own.
    """
    discounts = _group_discounts(layers)
    out: List[Size] = []
    for layer in layers:
        if layer.key is None:
            out.append(layer.cut)
            continue
        dh, dw = discounts[layer.key]
        height, width = layer.final
        out.append((height + allowance_mm + dh, width + allowance_mm + dw))
    return out


def assembly_groups(layers: Sequence[Layer]) -> List[Optional[int]]:
    """Each row's assembly as an index by first appearance (``None`` outside).

    What the optimization hash keeps of the codes: which rows go together,
    not what the seller called them, so renaming an assembly re-runs nothing.
    """
    index: Dict[str, int] = {}
    return [
        None if layer.key is None else index.setdefault(layer.key, len(index))
        for layer in layers
    ]


def _size(size: Size) -> str:
    return f"{size[0]}×{size[1]}"


def assembly_issues(layers: Sequence[Layer]) -> List[str]:
    """What is wrong with each assembly, one message per problem.

    For the writes only (a quote saved or sent, an order minted): an assembly's
    layers share their final size and their quantity, the canto goes on one of
    them, and there are at least two. A read never runs this.
    """
    groups: Dict[str, List[Layer]] = {}
    for layer in layers:
        if layer.key is not None:
            groups.setdefault(layer.key, []).append(layer)
    issues: List[str] = []
    for members in groups.values():
        name = f"Ens {(members[0].code or '').strip()}"
        if len(members) < 2:
            issues.append(
                f"{name}: tiene una sola capa; agrega la otra con el mismo código"
            )
            continue
        sizes = list(dict.fromkeys(m.final for m in members))
        if len(sizes) > 1:
            issues.append(
                f"{name}: las capas no tienen la misma medida "
                f"({' / '.join(_size(s) for s in sizes)})"
            )
        quantities = list(dict.fromkeys(m.quantity for m in members))
        if len(quantities) > 1:
            issues.append(
                f"{name}: las capas no tienen la misma cantidad "
                f"({' / '.join(str(q) for q in quantities)})"
            )
        banded = sum(1 for m in members if m.banded)
        if banded > 1:
            issues.append(f"{name}: el canto va en una sola capa y lo tienen {banded}")
    return issues
