import dataclasses
import hashlib
import json
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

from fastapi import Depends
from sqlalchemy.orm import Session

from src.cutting import (
    ENGINE_VERSION,
    BinSpec,
    CuttingLayout,
    CuttingParameters,
    ExactConfig,
    Piece,
    SearchBudget,
    exact_available,
)
from src.modules.clients.model import ClientModel
from src.modules.optimizations.labels import edge_banding_notation
from src.modules.optimizations.materials import MaterialResolver, ResolvedMaterial
from src.modules.optimizations.parallel import PoolJob, run_pool_jobs
from src.modules.optimizations.patterns import group_layouts
from src.modules.optimizations.pricing import build_pricing
from src.modules.optimizations.schemas import (
    STRATEGY_TO_PACKING,
    EdgeBandingSpec,
    EdgeSide,
    OptimizationStrategy,
    OptimizeRequest,
    OptimizeResponse,
    PricingSummary,
    Requirement,
)
from src.modules.products.model import ProductModel, ProductType
from src.modules.products.service import ProductService
from src.modules.settings.service import SettingsService
from src.shared.cache import cache
from src.shared.config import config
from src.shared.database import get_db
from src.shared.exceptions import (
    BusinessRuleError,
    EntityNotFoundError,
    ValidationError,
)

# Edge relocation when a piece comes out rotated from the optimizer. Convention:
# 90° clockwise rotation (top→right→bottom→left→top). The optimizer only swaps
# width↔height, so we fix this convention to draw the edge band on the correct
# physical side of the already-rotated piece.
_CW_ROTATION = {"top": "right", "right": "bottom", "bottom": "left", "left": "top"}

# Inverse of the above: geometric side → the piece's own (nominal) side. Derived
# rather than written out, so the two can never disagree.
CCW_ROTATION = {v: k for k, v in _CW_ROTATION.items()}

# Canonical order the banded sides are listed in, whichever frame they're in.
_SIDE_ORDER = ("top", "bottom", "left", "right")


def _exact_config() -> ExactConfig:
    """Runtime settings of the CP-SAT endgame, as the search will actually see them.

    ``enabled`` folds in whether OR-Tools is importable at all, so the value that
    salts the cache hash describes the geometry this process can really produce:
    a deployment without the solver never serves — nor poisons — the cache
    entries of one that has it.
    """
    return ExactConfig(
        enabled=config.OPT_EXACT_ENABLED and exact_available(),
        max_pieces=config.OPT_EXACT_MAX_PIECES,
        max_calls=config.OPT_EXACT_MAX_CALLS,
        deterministic_time=config.OPT_EXACT_DETERMINISTIC_TIME,
        root_deterministic_time=config.OPT_EXACT_ROOT_DETERMINISTIC_TIME,
        root_patience=config.OPT_EXACT_ROOT_PATIENCE,
    )


class OptimizationService:
    """Orchestrates the cutting domain (``cutting``) and caches the result by hash.

    The computation is deterministic and ephemeral: it's cached by a hash of the
    inputs and is **not** persisted to the DB (the order is the durable source of
    truth). The hash is the identifier used to retrieve the proforma. The material
    is source-agnostic: a ``MaterialResolver`` translates catalog/offcut/manual into
    dimensions and cost before optimizing, so ``cutting`` only ever sees geometry.
    """

    def __init__(self, db: Session):
        self.db = db
        self.product_service = ProductService(db)
        self.material_resolver = MaterialResolver(db)
        self.settings_service = SettingsService(db)

    def optimize_response(
        self, request: OptimizeRequest, additional_services: list | None = None
    ) -> OptimizeResponse:
        """Computes (cache-first) and builds the ``POST /optimize`` response.

        The computation is client-agnostic: the client is only resolved (and
        validated) when the request carries a ``client_id``. Without it, the
        response is anonymous. ``additional_services`` (billed services, not cut
        geometry) are folded into the ``pricing`` block after the discount; the
        raw ``/optimize`` endpoint passes none.
        """
        payload, optimization_hash = self.compute(request)
        client = None
        if request.client_id is not None:
            client = self.db.get(ClientModel, request.client_id)
            if client is None:
                raise EntityNotFoundError("Client", request.client_id)
        # The discount is applied outside the geometry cache: every tier reuses
        # the same payload (cache-first) and only differs in the `pricing` block.
        tier = self.settings_service.resolve_price_tier(request.price_tier_code)
        pricing = build_pricing(payload, tier, additional_services)
        return OptimizeResponse(
            id=None,
            client=client,
            optimization_hash=optimization_hash,
            strategy=payload.get("strategy", OptimizationStrategy.default.value),
            variant=payload.get("variant", 0),
            total_boards_used=payload["total_boards_used"],
            total_boards_cost=payload["total_boards_cost"],
            total_edge_banding_cost=payload.get("total_edge_banding_cost", 0.0),
            total_cut_linear_m=payload.get("total_cut_linear_m", 0.0),
            total_edge_banding_linear_m=payload.get("total_edge_banding_linear_m", 0.0),
            layouts=payload["layouts"],
            materials_summary=payload["materials_summary"],
            edge_bandings_summary=payload.get("edge_bandings_summary"),
            layout_groups=payload["layout_groups"],
            pricing=PricingSummary(**pricing),
        )

    def compute(self, request: OptimizeRequest) -> Tuple[dict, str]:
        """Computes (or retrieves from cache) the optimization result.

        Cache-first via a deterministic hash of the inputs (resolved materials +
        requirements + cutting parameters + edge-banding prices). Doesn't write to
        the DB: the orders module reuses it to freeze the snapshot without depending
        on the cache. Returns ``(payload, optimization_hash)``.
        """
        if not request.requirements:
            raise ValidationError("La lista de piezas no puede estar vacía")

        requirements_by_key = self._group_requirements_by_material_key(
            request.requirements
        )

        settings = self.settings_service.get_or_init()
        cutting_params = CuttingParameters(
            kerf=settings.kerf,
            top_trim=settings.top_trim,
            bottom_trim=settings.bottom_trim,
            left_trim=settings.left_trim,
            right_trim=settings.right_trim,
        )
        waste_factor = settings.edge_banding_waste_factor
        half_board_markup_pct = settings.half_board_markup_pct

        # Resolves only the materials actually referenced into dimensions+cost,
        # agnostic of source (catalog/offcut/manual). This is the only point that
        # knows about the catalog; ``cutting`` only ever sees geometry.
        materials_by_key = {m.key: m for m in request.materials}
        resolved: Dict[str, ResolvedMaterial] = {
            key: self.material_resolver.resolve(materials_by_key[key])
            for key in requirements_by_key
        }

        # Pooled offcuts: extra finite stock attached to a referenced catalog
        # board via ``pool_key``. Resolved and added to ``resolved`` so they feed
        # the hash, the materials summary and the order mapping; grouped by the
        # catalog key they supplement so the pool solver can consume them.
        pools: Dict[str, List[ResolvedMaterial]] = defaultdict(list)
        for material in request.materials:
            pool_key = getattr(material, "pool_key", None)
            if pool_key is None or pool_key not in resolved:
                continue
            rm = self.material_resolver.resolve(material)
            resolved[material.key] = rm
            pools[pool_key].append(rm)

        eb_products = self._resolve_edge_banding_products(request.requirements)

        optimization_hash = self._compute_hash(
            request,
            cutting_params,
            resolved,
            eb_products,
            waste_factor,
            half_board_markup_pct,
        )

        cached = cache.get_json(optimization_hash)
        if cached is not None:
            return cached, optimization_hash

        strategy = STRATEGY_TO_PACKING[request.strategy]
        exact_config = _exact_config()

        # The pools are independent, so the request should cost the MAX over its
        # materials rather than the sum. Three phases, and the split is what keeps
        # it safe: (a) build every job in the parent — cheap, pure, and the only
        # place that may touch the DB; (b) run them, in processes or in-process;
        # (c) recombine. Order is preserved *structurally* (one pass builds both
        # lists, ``zip`` pairs them again) rather than restored from completion
        # order, because ``_build_result_payload`` flattens ``results`` in order
        # and the payload is cached under a hash of the inputs.
        jobs: List[PoolJob] = []
        maps: List[Tuple[Dict[str, EdgeBandingSpec], Dict[str, float]]] = []
        for key, reqs in requirements_by_key.items():
            pieces, edge_map, net_map = self._build_pieces(reqs)
            if not pieces:
                # Raised here so no domain error ever crosses a process boundary.
                raise ValidationError("La lista de piezas no puede estar vacía")
            jobs.append(
                PoolJob(
                    material_key=key,
                    pieces=tuple(pieces),
                    material=resolved[key],
                    # Pooled offcuts: extra finite stock for this catalog board.
                    offcuts=tuple(pools.get(key) or ()),
                    cutting_params=cutting_params,
                    strategy=strategy,
                    half_spec=self._half_spec(resolved[key], half_board_markup_pct),
                    budget=SearchBudget.scaled(
                        len(pieces),
                        tries_per_board=config.OPT_TRIES_PER_BOARD,
                        iterations=config.OPT_SEARCH_ITERATIONS,
                    ),
                    seed=request.variant,
                    exact_config=exact_config,
                )
            )
            # The edge/net maps stay in the parent: cheap to build, pure, and the
            # optimizer has no use for them.
            maps.append((edge_map, net_map))

        results = [
            (edge_map, net_map, layouts)
            for (edge_map, net_map), layouts in zip(maps, run_pool_jobs(jobs))
        ]

        payload = self._build_result_payload(
            request, results, resolved, eb_products, waste_factor
        )
        cache.set_json(optimization_hash, payload)
        return payload, optimization_hash

    def _resolve_edge_banding_products(
        self, requirements: List[Requirement]
    ) -> Dict[int, ProductModel]:
        """Resolves and validates the edge-banding products referenced by the pieces.

        Same contract as board validation: 404 if it doesn't exist, business rule
        error if the product isn't of type ``edge_banding``.
        """
        eb_products: Dict[int, ProductModel] = {}
        for req in requirements:
            if req.edge_banding is None:
                continue
            pid = req.edge_banding.product_id
            # Geometry-only edge banding (no product): contributes length but isn't
            # resolved or charged until a product is assigned at quoting time.
            if pid is None or pid in eb_products:
                continue
            product = self.product_service.get(pid)
            if product is None:
                raise EntityNotFoundError("Product", pid)
            if product.type != ProductType.EDGE_BANDING.value:
                raise BusinessRuleError(
                    f"El producto {product.code} no es un tapacanto"
                )
            eb_products[pid] = product
        return eb_products

    def _compute_hash(
        self,
        request: OptimizeRequest,
        cutting_params: CuttingParameters,
        resolved: Dict[str, ResolvedMaterial],
        eb_products: Dict[int, ProductModel],
        waste_factor: float,
        half_board_markup_pct: float,
    ) -> str:
        """Deterministic sha256 hash of the inputs that affect the result.

        Doesn't include ``client_id`` (the computation doesn't depend on the
        client); order dedupe does combine ``client_id`` with this hash. Computed
        over the resolved materials (source, dimensions and cost), the
        requirements, the cutting parameters and the edge bandings, so the
        cache is invalidated whenever any of them changes.

        The edge-banding signature covers price **and** the attributes frozen
        into the payload (band type, design family): they don't move the
        geometry, but they're baked into the notation the PDFs print, so a
        catalog edit that isn't in the hash would stay invisible for a whole
        ``OPT_RESULT_TTL_SECONDS`` — exactly during the setup pass when those
        fields get edited.
        """
        materials = {
            key: {
                "source": rm.source,
                "width": rm.width,
                "height": rm.height,
                "thickness": rm.thickness,
                "cost_per_unit": rm.cost_per_unit,
                "product_id": rm.product_id,
                "quantity": rm.quantity,
                "pool_key": rm.pool_key,
                "fill_order": rm.fill_order.value,
            }
            for key, rm in resolved.items()
        }
        edge_bandings = {
            str(pid): {
                "price": p.price,
                "band_type": (p.attributes or {}).get("bandType"),
                "family": (p.attributes or {}).get("family"),
            }
            for pid, p in eb_products.items()
        }
        digest_input = {
            "materials": materials,
            "requirements": [r.model_dump(mode="json") for r in request.requirements],
            "params": {
                "kerf": cutting_params.kerf,
                "top_trim": cutting_params.top_trim,
                "bottom_trim": cutting_params.bottom_trim,
                "left_trim": cutting_params.left_trim,
                "right_trim": cutting_params.right_trim,
                "edge_banding_waste_factor": waste_factor,
                "half_board_markup_pct": half_board_markup_pct,
                "strategy": request.strategy.value,
                # Anything that changes the produced geometry must invalidate
                # cached results: the engine version, the search budget knobs
                # and the alternative-solution seed.
                "engine_version": ENGINE_VERSION,
                "tries_per_board": config.OPT_TRIES_PER_BOARD,
                "search_iterations": config.OPT_SEARCH_ITERATIONS,
                "exact": dataclasses.asdict(_exact_config()),
                "variant": request.variant,
            },
            "edge_bandings": edge_bandings,
        }
        canonical = json.dumps(digest_input, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _group_requirements_by_material_key(
        self, requirements: List[Requirement]
    ) -> Dict[str, List[Requirement]]:
        """Groups the requirements by the key of the material to optimize."""
        requirements_by_key = defaultdict(list)
        for req in requirements:
            requirements_by_key[req.material_key].append(req)
        return requirements_by_key

    @staticmethod
    def _half_spec(
        material: ResolvedMaterial, half_board_markup_pct: float
    ) -> Optional[BinSpec]:
        """Half-board bin for a catalog material (``None`` for inline sources).

        The business sells half catalog boards split lengthwise: same length,
        width/2, charged ``price/2 * (1 + markup)``. Handing it to the search as
        a cheaper sibling bin makes the half board an optimization objective
        instead of a post-hoc billing check.
        """
        if not material.is_catalog:
            return None
        return BinSpec(
            key=material.key,
            width=material.width / 2.0,
            height=material.height,
            thickness=material.thickness,
            cost_per_unit=round(
                material.cost_per_unit / 2.0 * (1 + half_board_markup_pct), 2
            ),
            half_board=True,
        )

    def _build_pieces(
        self, reqs: List[Requirement]
    ) -> Tuple[List[Piece], Dict[str, EdgeBandingSpec], Dict[str, float]]:
        """Expands the requirements into domain pieces with a unique id per instance.

        The piece id is the identity used to attribute edge banding and length to
        each placed piece, so it **must** be unique: two distinct requirements with
        the same label (e.g. several "Puerta" with different edge banding) no longer
        collapse into a single entry. A ``#N`` suffix is added when a base label has
        more than one physical instance in the group — whether from ``quantity > 1``
        or repeated labels — unifying both cases under a single rule (previously the
        optimizer only suffixed the ``quantity > 1`` case). Returns
        ``(pieces, edge_map, net_map)`` with the maps indexed by that unique id; the
        net length is per instance (``width`` for ``top/bottom``, ``height`` for
        ``left/right``, independent of rotation), not multiplied by quantity.
        """
        base = [p.label or f"piece_{i+1}" for i, p in enumerate(reqs)]
        totals: Counter = Counter()
        for label, p in zip(base, reqs):
            totals[label] += p.quantity

        seen: Counter = Counter()
        pieces: List[Piece] = []
        edge_map: Dict[str, EdgeBandingSpec] = {}
        net_map: Dict[str, float] = {}
        for i, p in enumerate(reqs):
            for _ in range(p.quantity):
                seen[base[i]] += 1
                uid = f"{base[i]}#{seen[base[i]]}" if totals[base[i]] > 1 else base[i]
                try:
                    pieces.append(
                        Piece(
                            id=uid,
                            width=p.width,
                            height=p.height,
                            quantity=1,
                            can_rotate=p.can_rotate,
                            priority=p.priority,
                        )
                    )
                except ValueError as e:
                    raise ValidationError(f"Pieza {i} tiene valores inválidos: {e}")
                if p.edge_banding is not None:
                    edge_map[uid] = p.edge_banding
                    net_map[uid] = sum(
                        p.width if side in (EdgeSide.top, EdgeSide.bottom) else p.height
                        for side in p.edge_banding.sides
                    )
        return pieces, edge_map, net_map

    def _geometric_edges(
        self, spec: EdgeBandingSpec, eb_products: Dict[int, ProductModel], rotated: bool
    ) -> dict:
        """Translates the nominal sides to the geometric sides of the drawn piece.

        Not rotated: identity. Rotated: the optimizer only swaps width↔height (a
        bounding box, with no sense of rotation direction), so we adopt the
        convention of a 90° clockwise turn and relocate each edge band
        (``_CW_ROTATION``). A pure rotation is always physically realizable, so
        asymmetric edge banding doesn't prevent rotation: the sides are simply
        swapped.

        Both frames ship: ``sides`` is the drawn one (paint the band on the right
        physical edge) and ``nominal_sides`` the piece's own (the one the
        requested measurements and the ``L``/``C`` notation refer to). A consumer
        that shows a piece as the client ordered it wants the latter — deriving
        it from ``sides`` means re-implementing this rotation convention.
        """
        nominal = {s.value for s in spec.sides}
        sides = {_CW_ROTATION[s] for s in nominal} if rotated else nominal
        geo = [s for s in _SIDE_ORDER if s in sides]
        product = eb_products.get(spec.product_id)
        attrs = (product.attributes if product else None) or {}
        return {
            "sides": geo,
            "nominal_sides": [s for s in _SIDE_ORDER if s in nominal],
            "product_id": spec.product_id,
            "code": product.code if product else None,
            "color": attrs.get("color"),
            # Canonical type (``Soft``/``Hard``) to differentiate the band in the
            # diagram (soft = solid, hard = hatched). ``None`` in older snapshots.
            "band_type": attrs.get("bandType"),
            # Design family (``CSH``) so the workshop tells two banded designs
            # apart. ``None`` when the product has no family configured.
            "family": attrs.get("family"),
            # Workshop notation computed from the NOMINAL sides (stable under
            # rotation); ``geo`` is only used to draw the bands on the right side.
            # ``attributes`` is persisted in camelCase → ``bandType``.
            "notation": edge_banding_notation(
                nominal, attrs.get("bandType"), attrs.get("family")
            ),
        }

    def _enrich_layout_pieces(
        self,
        layout_dict: dict,
        edge_map: Dict[str, EdgeBandingSpec],
        eb_products: Dict[int, ProductModel],
    ) -> None:
        """Adds ``edges`` (geometric banded sides) to each placed piece.

        ``edge_map`` is indexed by the piece's unique id (see ``_build_pieces``), so
        the lookup uses the exact ``piece_id`` of the placed piece.
        """
        for placed in layout_dict.get("placed_pieces", []):
            spec = edge_map.get(str(placed.get("piece_id", "")))
            if spec is None:
                continue
            placed["edges"] = self._geometric_edges(
                spec, eb_products, bool(placed.get("rotated"))
            )

    def _build_edge_bandings_summary(
        self,
        requirements: List[Requirement],
        eb_products: Dict[int, ProductModel],
        waste_factor: float,
    ) -> Tuple[List[dict], float]:
        """Aggregates edge-banding length by type and returns ``(summary, total)``.

        The net length is the sum of the banded sides (``width`` for
        ``top/bottom``, ``height`` for ``left/right``) times the quantity; it's
        independent of rotation. The configured waste factor is applied and the
        result is billed exactly (net + waste), with no rounding up to a whole
        meter.
        """
        waste = waste_factor
        net_mm: Dict[Optional[int], float] = defaultdict(float)
        for req in requirements:
            spec = req.edge_banding
            if spec is None:
                continue
            per_piece = sum(
                req.width if side in (EdgeSide.top, EdgeSide.bottom) else req.height
                for side in spec.sides
            )
            net_mm[spec.product_id] += per_piece * req.quantity

        summary: List[dict] = []
        total_cost = 0.0
        for pid, mm in net_mm.items():
            # ``pid is None`` = geometry-only edge banding: length is reported but
            # without product identity or price (pending assignment at quoting time).
            product = eb_products.get(pid)
            attrs = (product.attributes if product else None) or {}
            price = product.price if product else 0.0
            net_m = mm / 1000.0
            billed = round(net_m * (1 + waste), 2)
            cost = round(billed * price, 2)
            total_cost += cost
            summary.append(
                {
                    "product_id": pid,
                    "product_code": product.code if product else None,
                    "product_name": product.name if product else None,
                    "thickness": attrs.get("thickness"),
                    "color": attrs.get("color"),
                    "band_type": attrs.get("bandType"),
                    "family": attrs.get("family"),
                    "net_linear_m": round(net_m, 2),
                    "linear_m": billed,
                    "billed_linear_m": billed,
                    "price_per_m": price,
                    "total_cost": cost,
                }
            )
        return summary, round(total_cost, 2)

    def _build_materials_summary(
        self,
        layouts: List[CuttingLayout],
        resolved: Dict[str, ResolvedMaterial],
    ) -> List[dict]:
        """Aggregates the layouts by material with metrics and costs (any source).

        Carries the origin metadata (``material_key``/``source`` and, for catalog
        materials only, ``product_id``/``product_code``/``product_name``). For
        inline materials it falls back to the key as code and the dimensions as a
        readable name, so the proforma renders without special handling.
        """
        # Composite key (material, half?) so full and half boards of the same
        # material end up as separate billing lines (different width, cost, label).
        summary: Dict[Tuple[str, bool], dict] = {}
        for layout in layouts:
            key = layout.material.id
            is_half = layout.material.half_board
            group = (key, is_half)
            if group not in summary:
                rm = resolved.get(key)
                dims_label = f"{layout.material.width:g}×{layout.material.height:g}"
                base_name = rm.name if rm and rm.name else dims_label
                summary[group] = {
                    "material_key": key,
                    "source": rm.source if rm else None,
                    "product_id": rm.product_id if rm else None,
                    "product_code": (rm.code if rm and rm.code else key),
                    "product_name": (
                        f"{base_name} (medio tablero)" if is_half else base_name
                    ),
                    "width": layout.material.width,
                    "height": layout.material.height,
                    "thickness": layout.material.thickness,
                    "count": 0,
                    "total_area_m2": 0.0,
                    "_efficiencies": [],
                    "cost_per_unit": layout.material.cost_per_unit,
                    "total_cost": 0.0,
                    "half_board": is_half,
                }
            entry = summary[group]
            entry["count"] += 1
            entry["total_area_m2"] += round(layout.material.area / 1_000_000, 4)
            entry["_efficiencies"].append(layout.efficiency * 100)
            entry["total_cost"] += layout.material.cost_per_unit

        result = []
        for entry in summary.values():
            effs = entry.pop("_efficiencies")
            entry["avg_efficiency"] = round(sum(effs) / len(effs), 2) if effs else 0.0
            entry["total_area_m2"] = round(entry["total_area_m2"], 4)
            entry["total_cost"] = round(entry["total_cost"], 2)
            result.append(entry)
        return result

    @staticmethod
    def _dump_requirement(
        req: Requirement,
        resolved: Dict[str, ResolvedMaterial],
        eb_products: Dict[int, ProductModel],
    ) -> dict:
        """Dumps a requirement to the payload with the edge-banding display attributes.

        ``product_code`` carries the material's label (catalog code, or
        name/key for inline sources) that the proforma shows in the "Tablero"
        column. ``band_type`` and ``family`` live in the product's attributes,
        not in the ``EdgeBandingSpec``; they're injected here so the proforma can
        build the edge notation (``2L1C CS CSH``) without re-resolving the
        product at render time.
        """
        rm = resolved.get(req.material_key)
        material_label = (rm.code or rm.name) if rm else None
        data = {
            **req.model_dump(mode="json"),
            "product_code": material_label or req.material_key,
            "product_name": (rm.name if rm else None),
        }
        if req.edge_banding is not None and data.get("edge_banding"):
            product = eb_products.get(req.edge_banding.product_id)
            attrs = (product.attributes if product else None) or {}
            # ``attributes`` is persisted in camelCase → ``bandType``.
            data["edge_banding"]["band_type"] = attrs.get("bandType")
            data["edge_banding"]["family"] = attrs.get("family")
        return data

    def _build_result_payload(
        self,
        request: OptimizeRequest,
        results: List[
            Tuple[Dict[str, EdgeBandingSpec], Dict[str, float], List[CuttingLayout]]
        ],
        resolved: Dict[str, ResolvedMaterial],
        eb_products: Dict[int, ProductModel],
        waste_factor: float,
    ) -> dict:
        """Builds the cacheable/serializable payload for the optimization result.

        Same keys consumed by ``proforma`` and the order snapshot. ``results``
        groups by material as ``(edge_map, net_map, layouts)`` (maps indexed by the
        unique piece id from ``_build_pieces``) to enrich each placed piece with its
        banded sides and length without id collisions.
        """
        all_layouts = [layout for _, _, layouts in results for layout in layouts]

        # "Boards used"/cost is what the client buys. A pooled offcut is the
        # client's own material attached to a catalog board, so it's excluded from
        # the headline count/cost (it still shows per sheet in ``layouts``, as its
        # own ``materials_summary`` line and in the diagram). Standalone materials
        # (catalog/manual/non-pooled offcut) keep counting as before.
        def _is_pooled_offcut(layout: CuttingLayout) -> bool:
            rm = resolved.get(layout.material.id)
            return rm is not None and rm.pool_key is not None

        billed_layouts = [
            layout for layout in all_layouts if not _is_pooled_offcut(layout)
        ]
        total_boards_used = len(billed_layouts)
        total_boards_cost = sum(
            layout.material.cost_per_unit for layout in billed_layouts
        )

        # Per-sheet metrics (cut = saw travel; edge banding = net length of the
        # placed pieces) accumulated into overall totals. Injected into the layout
        # statistics before ``group_layouts`` so deduplicated patterns inherit them.
        layout_dicts: List[dict] = []
        total_cut_linear_m = 0.0
        total_edge_banding_linear_m = 0.0
        for edge_map, net_map, layouts in results:
            for layout in layouts:
                layout_dict = layout.to_dict()
                if edge_map:
                    self._enrich_layout_pieces(layout_dict, edge_map, eb_products)
                cut_linear_m = round(layout.cut_length / 1000.0, 2)
                eb_mm = sum(
                    net_map.get(str(p.get("piece_id", "")), 0.0)
                    for p in layout_dict.get("placed_pieces", [])
                )
                eb_linear_m = round(eb_mm / 1000.0, 2)
                stats = layout_dict["statistics"]
                stats["cut_linear_m"] = cut_linear_m
                stats["edge_banding_linear_m"] = eb_linear_m
                total_cut_linear_m += cut_linear_m
                total_edge_banding_linear_m += eb_linear_m
                layout_dicts.append(layout_dict)

        edge_bandings_summary, total_edge_banding_cost = (
            self._build_edge_bandings_summary(
                request.requirements, eb_products, waste_factor
            )
        )
        return {
            "strategy": request.strategy.value,
            "variant": request.variant,
            "total_boards_used": total_boards_used,
            "total_boards_cost": total_boards_cost,
            "total_edge_banding_cost": total_edge_banding_cost,
            "total_cut_linear_m": round(total_cut_linear_m, 2),
            "total_edge_banding_linear_m": round(total_edge_banding_linear_m, 2),
            "materials": [rm.to_dict() for rm in resolved.values()],
            "requirements": [
                self._dump_requirement(r, resolved, eb_products)
                for r in request.requirements
            ],
            "layouts": layout_dicts,
            "materials_summary": self._build_materials_summary(all_layouts, resolved),
            "edge_bandings_summary": edge_bandings_summary,
            "layout_groups": group_layouts(layout_dicts),
        }


def optimization_service(db: Session = Depends(get_db)) -> OptimizationService:
    """``OptimizationService`` provider for route injection."""
    return OptimizationService(db)
