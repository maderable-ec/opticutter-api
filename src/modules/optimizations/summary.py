"""Aggregation of layouts into the billing lines (``materials_summary``).

Pure layer (no DB, no framework, no ``src.cutting`` import): it consumes the
**serialized** layouts (``CuttingLayout.to_dict()``) and the serialized resolved
materials (``ResolvedMaterial.to_dict()``), both of which already live in the
optimization payload.

It reads dicts rather than domain objects on purpose: the summary has to be
rebuilt *after* the payload comes back from the cache — by
``whole_boards.apply_whole_boards`` — where the ``CuttingLayout`` objects no
longer exist. Keeping one implementation here is what stops the merge rule
(composite ``(key, half)`` line, the "(medio tablero)" suffix, the efficiency
average) from being written twice.
"""

from typing import Dict, List, Tuple


def build_materials_summary(layouts: List[dict], materials: List[dict]) -> List[dict]:
    """Aggregates the layouts by material with metrics and costs (any source).

    Carries the origin metadata (``material_key``/``source`` and, for catalog
    materials only, ``product_id``/``product_code``/``product_name``). For
    inline materials it falls back to the key as code and the dimensions as a
    readable name, so the proforma renders without special handling.
    """
    # Composite key (material, half?) so full and half boards of the same
    # material end up as separate billing lines (different width, cost, label).
    by_key = {m.get("material_key"): m for m in materials or []}
    summary: Dict[Tuple[str, bool], dict] = {}
    for layout in layouts:
        material = layout.get("material") or {}
        key = material.get("material_key")
        is_half = bool(material.get("half_board", False))
        group = (key, is_half)
        if group not in summary:
            rm = by_key.get(key)
            width = material.get("width", 0.0)
            height = material.get("height", 0.0)
            dims_label = f"{width:g}×{height:g}"
            base_name = (rm or {}).get("product_name") or dims_label
            summary[group] = {
                "material_key": key,
                "source": (rm or {}).get("source"),
                "product_id": (rm or {}).get("product_id"),
                "product_code": ((rm or {}).get("product_code") or key),
                "product_name": (
                    f"{base_name} (medio tablero)" if is_half else base_name
                ),
                "width": width,
                "height": height,
                "thickness": material.get("thickness", 0.0),
                "count": 0,
                "total_area_m2": 0.0,
                "_efficiencies": [],
                "cost_per_unit": material.get("cost_per_unit", 0.0),
                "total_cost": 0.0,
                "half_board": is_half,
            }
        entry = summary[group]
        entry["count"] += 1
        area = material.get("area", 0.0)
        entry["total_area_m2"] += round(area / 1_000_000, 4)
        # Recomputed from the raw areas rather than read from
        # ``statistics.efficiency``: that one is already rounded to 2 decimals
        # (``CuttingLayout.to_dict``), and averaging rounded values would shift
        # every ``avg_efficiency`` this module has ever produced.
        used_area = (layout.get("statistics") or {}).get("used_area", 0.0)
        entry["_efficiencies"].append((used_area / area * 100) if area else 0.0)
        entry["total_cost"] += material.get("cost_per_unit", 0.0)

    result = []
    for entry in summary.values():
        effs = entry.pop("_efficiencies")
        entry["avg_efficiency"] = round(sum(effs) / len(effs), 2) if effs else 0.0
        entry["total_area_m2"] = round(entry["total_area_m2"], 4)
        entry["total_cost"] = round(entry["total_cost"], 2)
        result.append(entry)
    return result
