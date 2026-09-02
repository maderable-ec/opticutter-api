import dataclasses
import hashlib
import json
import logging
import time
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Sequence, Tuple

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
from src.modules.optimizations.engine_info import backend_name
from src.modules.optimizations.labels import edge_banding_notation
from src.modules.optimizations.materials import MaterialResolver, ResolvedMaterial
from src.modules.optimizations.parallel import (
    PoolJob,
    PoolResult,
    run_pool_jobs,
    runs_in_process,
)
from src.modules.optimizations.patterns import group_layouts, order_sheets
from src.modules.optimizations.price_levels import apply_price_level, price_at_level
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
from src.modules.optimizations.summary import build_materials_summary
from src.modules.optimizations.whole_boards import apply_whole_boards
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

logger = logging.getLogger(__name__)

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
        geometry) are folded into the ``pricing`` block; the raw ``/optimize``
        endpoint passes none.
        """
        payload, optimization_hash = self.compute(request)
        client = None
        if request.client_id is not None:
            client = self.db.get(ClientModel, request.client_id)
            if client is None:
                raise EntityNotFoundError("Client", request.client_id)
        # ``compute`` already re-priced the marked boards at the requested level
        # (outside the geometry cache, so every level reuses the same cut plan),
        # leaving only the services and the tax to add on top.
        pricing = build_pricing(
            payload,
            request.price_level,
            additional_services,
            self.settings_service.get_tax_rate(),
        )
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

        Timed from here — before the cache lookup, not around the search — because
        the question the log has to answer is "why was that quote slow", and a
        Redis that has started failing open (``src/shared/cache.py`` swallows every
        ``RedisError``) turns every request into a cold compute without any other
        symptom.
        """
        started = time.perf_counter()
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
            self._log_compute(optimization_hash, started, hit=True, jobs=(), results=())
            return (
                self._apply_commercial_overrides(
                    cached, request, resolved, half_board_markup_pct
                ),
                optimization_hash,
            )

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
                    min_usable_offcut=config.OPT_MIN_USABLE_OFFCUT_MM,
                )
            )
            # The edge/net maps stay in the parent: cheap to build, pure, and the
            # optimizer has no use for them.
            maps.append((edge_map, net_map))

        pool_results = run_pool_jobs(jobs)
        results = [
            (edge_map, net_map, pool.layouts)
            for (edge_map, net_map), pool in zip(maps, pool_results)
        ]

        payload = self._build_result_payload(
            request, results, resolved, eb_products, waste_factor
        )
        # Cached BEFORE the overrides: Redis has to hold the canonical payload
        # for this hash, or the next request with the flag off would be served a
        # whole board nobody asked for, for a whole OPT_RESULT_TTL_SECONDS.
        cache.set_json(optimization_hash, payload)
        self._log_compute(
            optimization_hash, started, hit=False, jobs=jobs, results=pool_results
        )
        return (
            self._apply_commercial_overrides(
                payload, request, resolved, half_board_markup_pct
            ),
            optimization_hash,
        )

    @staticmethod
    def _apply_commercial_overrides(
        payload: dict,
        request: OptimizeRequest,
        resolved: Dict[str, ResolvedMaterial],
        half_board_markup_pct: float,
    ) -> dict:
        """Reshapes the cached plan for the flags that live outside the hash.

        Applied on BOTH paths (cache hit and cold compute) through this single
        helper so the two can't drift, and after ``_log_compute`` so a bug here
        can never break the log's promise that it never fails a quote. Two
        passes today, and the ORDER MATTERS: the price level first, because
        ``apply_whole_boards`` reads a material's ``cost_per_unit`` as the whole
        sheet's price when it promotes a half, and that has to already be the
        level's price.

        ``resolved`` carries the levels (never serialized into the payload, so
        they can't go stale behind the cache); the tax is added afterwards, by
        ``build_pricing`` over the summary these rebuilt.
        """
        leveled = request.leveled_material_keys
        level_prices = {
            key: price_at_level(
                material.cost_per_unit,
                material.price_2,
                material.price_3,
                request.price_level,
            )
            for key, material in resolved.items()
            if material.is_catalog and key in leveled
        }
        payload = apply_price_level(payload, level_prices, half_board_markup_pct)
        return apply_whole_boards(payload, request.whole_board_material_keys)

    def _log_compute(
        self,
        optimization_hash: str,
        started: float,
        *,
        hit: bool,
        jobs: Sequence[PoolJob],
        results: Sequence[PoolResult],
    ) -> None:
        """One greppable line per optimization: where the time went, and why.

        Emitted on both the cache hit and the cold compute, because the ratio
        between them is the first thing worth knowing when quotes get slow — a
        collapsed hit rate and a genuinely heavier job look identical from the
        outside, and only one of them is fixed by touching the engine.

        Level is INFO normally and WARNING past ``OPT_SLOW_LOG_SECONDS``:
        production runs at ``LOG_LEVEL=WARNING``, so the slow tail has to raise
        its own level or it is never seen. Never let this raise — a broken log
        line must not fail a computed quote.
        """
        try:
            elapsed = time.perf_counter() - started
            pools = " ".join(
                f"{job.material_key}={len(job.pieces)}pzs/{result.seconds:.1f}s"
                for job, result in zip(jobs, results)
            )
            threshold = config.OPT_SLOW_LOG_SECONDS
            slow = threshold > 0 and elapsed >= threshold
            logger.log(
                logging.WARNING if slow else logging.INFO,
                "optimize %s cache=%s pools=%d path=%s backend=%s %.2fs%s",
                optimization_hash[:12],
                "hit" if hit else "miss",
                len(jobs),
                "-" if hit else ("in-process" if runs_in_process(jobs) else "pool"),
                backend_name(),
                elapsed,
                f" · {pools}" if pools else "",
            )
        except Exception:  # noqa: BLE001 - observability must never break a quote
            logger.debug("could not log the optimization timing", exc_info=True)

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
        into the payload (band type, alias): they don't move the geometry, but
        they're baked into the notation the PDFs print, so a catalog edit that
        isn't in the hash would stay invisible for a whole
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
                "alias": (p.attributes or {}).get("alias"),
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
                # Never moves a piece or a board, but it does decide the cut
                # tree and therefore the leftovers this payload reports.
                "min_usable_offcut": config.OPT_MIN_USABLE_OFFCUT_MM,
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
            # Short alias (``CSH``) so the workshop tells two banded designs
            # apart. ``None`` when the product has no alias configured.
            # Independent of ``family``, which stays the coordination key.
            "alias": attrs.get("alias"),
            # Workshop notation computed from the NOMINAL sides (stable under
            # rotation); ``geo`` is only used to draw the bands on the right side.
            # ``attributes`` is persisted in camelCase → ``bandType``.
            "notation": edge_banding_notation(
                nominal, attrs.get("bandType"), attrs.get("alias")
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
                    "alias": attrs.get("alias"),
                    "net_linear_m": round(net_m, 2),
                    "linear_m": billed,
                    "billed_linear_m": billed,
                    "price_per_m": price,
                    "total_cost": cost,
                }
            )
        return summary, round(total_cost, 2)

    @staticmethod
    def _dump_requirement(
        req: Requirement,
        resolved: Dict[str, ResolvedMaterial],
        eb_products: Dict[int, ProductModel],
    ) -> dict:
        """Dumps a requirement to the payload with the edge-banding display attributes.

        ``product_code`` carries the material's label (catalog code, or
        name/key for inline sources) that the proforma shows in the "Tablero"
        column. ``band_type`` and ``alias`` live in the product's attributes,
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
            data["edge_banding"]["alias"] = attrs.get("alias")
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
            # Each `results` entry is one material's pool, so ordering inside
            # the loop puts a material's half board last without interleaving
            # two materials. See `order_sheets`.
            for layout in order_sheets(layouts):
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
        # Serialized once: the payload carries them AND the summary aggregates
        # over them (``build_materials_summary`` reads dicts so it can also run
        # after a cache read, where no ``CuttingLayout`` exists).
        material_dicts = [rm.to_dict() for rm in resolved.values()]
        return {
            "strategy": request.strategy.value,
            "variant": request.variant,
            "total_boards_used": total_boards_used,
            "total_boards_cost": total_boards_cost,
            "total_edge_banding_cost": total_edge_banding_cost,
            "total_cut_linear_m": round(total_cut_linear_m, 2),
            "total_edge_banding_linear_m": round(total_edge_banding_linear_m, 2),
            "materials": material_dicts,
            "requirements": [
                self._dump_requirement(r, resolved, eb_products)
                for r in request.requirements
            ],
            "layouts": layout_dicts,
            "materials_summary": build_materials_summary(layout_dicts, material_dicts),
            "edge_bandings_summary": edge_bandings_summary,
            "layout_groups": group_layouts(layout_dicts),
        }


def optimization_service(db: Session = Depends(get_db)) -> OptimizationService:
    """``OptimizationService`` provider for route injection."""
    return OptimizationService(db)
