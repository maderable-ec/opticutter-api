"""Each placed piece's workshop codes, laid over a plan from its own cut list.

The codes (abisagrado, ranurado, ensamble, división) never enter the hash nor
the cached payload (``WORKSHOP_CODE_FIELDS``): a cache hit would hand back
another request's. So whoever needs them on a placed piece puts them back from
the request it is answering -- the order when it freezes its cutting plan, and
the ``/optimize`` response so the seller's diagram can print them under the
canto, the way the workshop canvas does.
"""

from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from src.modules.optimizations.patterns import piece_instance_ids
from src.modules.optimizations.schemas import WORKSHOP_CODE_FIELDS, has_workshop_codes


def workshop_codes_by_instance(
    requirements: Iterable[dict],
) -> Dict[Tuple[str, str], dict]:
    """``(material_key, instance_id) -> codes`` for every piece that carries any.

    Instance ids are only unique inside ONE material group (two materials can
    both cut a "Puerta"), so the group is part of the key, and each group is
    named exactly as the optimizer named it: same grouping (by ``material_key``,
    request order) and the same ``piece_instance_ids``.
    """
    groups: Dict[str, List[dict]] = {}
    for r in requirements:
        groups.setdefault(r.get("material_key"), []).append(r)
    out: Dict[Tuple[str, str], dict] = {}
    for key, reqs in groups.items():
        entries = [(r.get("label"), r.get("quantity", 1)) for r in reqs]
        for i, uid in piece_instance_ids(entries):
            if has_workshop_codes(reqs[i]):
                out[(key, uid)] = {f: reqs[i].get(f) for f in WORKSHOP_CODE_FIELDS}
    return out


def with_piece_workshop_codes(
    layouts: List[Optional[dict]],
    requirements: Iterable[dict],
    anchor_of: Optional[Mapping[str, str]] = None,
) -> List[Optional[dict]]:
    """``layouts`` with the codes of its cut-list row on every placed piece.

    ``anchor_of`` maps a material to the anchor whose cut list its pieces
    belong to (a pooled retazo cuts its anchor's pieces); without it every
    material is its own anchor, which is exact for any job with no pool. New
    dicts wherever something is laid, never a mutation: the layouts may be the
    cached payload's own. An empty sheet (the editor's ``None``) stays as it is.
    """
    codes = workshop_codes_by_instance(requirements)
    if not codes:
        return layouts
    anchor_of = anchor_of or {}
    out = []
    for layout in layouts:
        if not layout:
            out.append(layout)
            continue
        key = (layout.get("material") or {}).get("material_key")
        anchor = anchor_of.get(key) or key
        pieces = layout.get("placed_pieces") or []
        if not any((anchor, str(p.get("piece_id", ""))) in codes for p in pieces):
            out.append(layout)
            continue
        out.append(
            {
                **layout,
                "placed_pieces": [
                    {**p, **codes.get((anchor, str(p.get("piece_id", ""))), {})}
                    for p in pieces
                ],
            }
        )
    return out
