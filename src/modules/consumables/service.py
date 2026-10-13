"""Registers the changes of the shop's consumables and reads what each one gave.

The rules are in ``rules.py`` (pure); the work a life is measured in comes from
``orders/production_service.py``, the same rows the statistics count, so a
blade's metres and the saw's metres are one number.
"""

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Iterable, Optional

from fastapi import Depends
from sqlalchemy.orm import Session

from src.modules.branches.model import BranchModel
from src.modules.consumables.model import (
    BLADE_TYPES,
    CONSUMABLE_LABELS,
    CONSUMABLE_ROLES,
    REASONS_BY_TYPE,
    BladeCondition,
    ChangeReason,
    ConsumableChangeModel,
    ConsumableType,
)
from src.modules.consumables.rules import (
    MIN_CHANGE_GAP,
    Gate,
    Life,
    Usage,
    average,
    can_undo,
    change_gate,
    lives,
    meters_per_kg,
    usage_by_life,
    worked_seconds,
)
from src.modules.consumables.schemas import (
    BranchConsumableReport,
    BranchConsumables,
    ConditionYield,
    ConsumableChangeCreate,
    ConsumableChangeResponse,
    ConsumableLife,
    ConsumableReport,
    ConsumableStatus,
    ConsumableSummary,
)
from src.modules.orders.production_service import ProductionService
from src.modules.settings.service import SettingsService
from src.shared.audit import Actor
from src.shared.database import get_db
from src.shared.exceptions import (
    AuthorizationError,
    ConflictError,
    EntityNotFoundError,
    ValidationError,
)

# ``(branch_id, type)``: one consumable of one branch.
_Key = tuple[int, ConsumableType]
_Lives = dict[_Key, list[Life[ConsumableChangeModel]]]


class ConsumableService:
    def __init__(self, db: Session):
        self.db = db
        self.production = ProductionService(db)
        self.settings = SettingsService(db)

    # ------------------------------------------------------------------ reads
    def _changes(
        self, branch_ids: Iterable[int], kind: Optional[ConsumableType] = None
    ) -> list[ConsumableChangeModel]:
        """The changes in force of ``branch_ids``, oldest first."""
        query = self.db.query(ConsumableChangeModel).filter(
            ConsumableChangeModel.branch_id.in_(list(branch_ids)),
            ConsumableChangeModel.voided_at.is_(None),
        )
        if kind is not None:
            query = query.filter(ConsumableChangeModel.type == kind.value)
        return query.order_by(
            ConsumableChangeModel.changed_at, ConsumableChangeModel.id
        ).all()

    def _lives(self, branch_ids: Iterable[int]) -> _Lives:
        by_key: dict[_Key, list[ConsumableChangeModel]] = defaultdict(list)
        for change in self._changes(branch_ids):
            by_key[(change.branch_id, ConsumableType(change.type))].append(change)
        return {key: lives(changes) for key, changes in by_key.items()}

    def _branches(
        self, branch_scope: Optional[int], branch_id: Optional[int] = None
    ) -> list[BranchModel]:
        """The viewer's branch, or the active ones (narrowed to ``branch_id``)."""
        query = self.db.query(BranchModel)
        if branch_scope is not None:
            query = query.filter(BranchModel.id == branch_scope)
        else:
            query = query.filter(BranchModel.is_active.is_(True))
            if branch_id is not None:
                query = query.filter(BranchModel.id == branch_id)
        return query.order_by(BranchModel.id).all()

    @staticmethod
    def _may_register(actor: Actor, kind: ConsumableType) -> bool:
        return not actor.roles or bool(
            set(actor.roles) & {role.value for role in CONSUMABLE_ROLES[kind]}
        )

    def _usage(
        self, all_lives: _Lives, wanted: _Lives, now: datetime
    ) -> dict[int, Usage]:
        """``opening change id -> its work``, exact for the lives in ``wanted``.

        The production is read once per machine over the span the wanted lives
        cover, and dealt against ALL the lives of each consumable: a life's
        work ends where the next one starts, wanted or not.
        """
        upto = now + timedelta(seconds=1)
        usage: dict[int, Usage] = {}
        for blades, load in ((True, self._sheets), (False, self._bandings)):
            keys = [
                key
                for key, items in wanted.items()
                if items and (key[1] in BLADE_TYPES) is blades
            ]
            if not keys:
                continue
            start = min(life.start for key in keys for life in wanted[key])
            end = max(life.end or upto for key in keys for life in wanted[key])
            branch_ids = {branch_id for branch_id, _ in keys}
            events = load(
                start, end, next(iter(branch_ids)) if len(branch_ids) == 1 else None
            )
            for key in keys:
                dealt = usage_by_life(
                    [life.start for life in all_lives[key]], events.get(key[0], ())
                )
                for life, work in zip(all_lives[key], dealt):
                    usage[life.opening.id] = work
        return usage

    def _sheets(self, start: datetime, end: datetime, branch_id: Optional[int]) -> dict:
        """``branch_id -> (done_at, metres cut, board weight)`` of the sheets closed."""
        events: dict[int, list] = defaultdict(list)
        for board in self.production.processed_boards(start, end, branch_id):
            events[board.branch_id].append(
                (board.done_at, board.cut_linear_m, board.weight)
            )
        return events

    def _bandings(
        self, start: datetime, end: datetime, branch_id: Optional[int]
    ) -> dict:
        """``branch_id -> (closed at, metres banded, 1)`` of the bandings closed."""
        events: dict[int, list] = defaultdict(list)
        for close in self.production.banding_closes(start, end, branch_id):
            events[close.branch_id].append((close.finished_at, close.banded_m, 1.0))
        return events

    # ----------------------------------------------------------------- status
    def status(
        self,
        viewer: Actor,
        branch_scope: Optional[int],
        branch_id: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> list[BranchConsumables]:
        """What is on each machine and what it has done since it went on.

        Only the consumables the viewer may register: the operator sees the
        blades, the bander the glue.
        """
        now = now or datetime.utcnow()
        branches = self._branches(branch_scope, branch_id)
        kinds = [kind for kind in ConsumableType if self._may_register(viewer, kind)]
        if not branches or not kinds:
            return []
        all_lives = self._lives(branch.id for branch in branches)
        in_use = {
            key: items[-1:] for key, items in all_lives.items() if key[1] in kinds
        }
        usage = self._usage(all_lives, in_use, now)
        schedule = self.production.schedule
        glue_load = self.settings.get_glue_load_kg()

        report = []
        for branch in branches:
            items = []
            for kind in kinds:
                current = in_use.get((branch.id, kind))
                last = current[0].opening if current else None
                work = usage.get(last.id) if last else None
                blade = kind in BLADE_TYPES
                items.append(
                    ConsumableStatus(
                        type=kind,
                        gate=change_gate(
                            last.changed_at if last else None, now, schedule
                        ),
                        last_change=(
                            ConsumableChangeResponse.model_validate(last)
                            if last
                            else None
                        ),
                        meters=round(work.meters, 2) if work else None,
                        boards=round(work.units, 1) if work and blade else None,
                        orders=int(work.units) if work and not blade else None,
                        worked_hours=(
                            round(
                                worked_seconds(last.changed_at, now, schedule) / 3600,
                                2,
                            )
                            if last
                            else None
                        ),
                        can_undo=bool(last)
                        and can_undo(
                            last.created_at, last.changed_by, viewer.user_id, True, now
                        ),
                        standard_quantity=None if blade else glue_load,
                    )
                )
            report.append(
                BranchConsumables(
                    branch_id=branch.id, branch_name=branch.name, items=items
                )
            )
        return report

    def _branch_status(
        self, viewer: Actor, branch_id: int, now: datetime
    ) -> BranchConsumables:
        """One branch as ``viewer`` sees it: what a write answers with."""
        return self.status(viewer, branch_scope=branch_id, now=now)[0]

    # ----------------------------------------------------------------- writes
    def _ensure_role(self, actor: Actor, kind: ConsumableType) -> None:
        if not self._may_register(actor, kind):
            raise AuthorizationError(
                f"Tu rol no puede registrar cambios de {CONSUMABLE_LABELS[kind]}"
            )

    def _locked_branch(
        self, requested: Optional[int], branch_scope: Optional[int]
    ) -> BranchModel:
        """The branch a change goes to, its row locked for the transaction.

        The lock is what makes the gate hold with two tablets on one saw: the
        second registration waits, and then reads the first one.
        """
        if branch_scope is not None:
            if requested is not None and requested != branch_scope:
                raise AuthorizationError("Solo puedes registrar insumos de tu sucursal")
            branch_id = branch_scope
        elif requested is None:
            raise ValidationError("Indica la sucursal", field="branchId")
        else:
            branch_id = requested
        branch = (
            self.db.query(BranchModel)
            .filter(BranchModel.id == branch_id)
            .with_for_update()
            .first()
        )
        if branch is None:
            raise EntityNotFoundError("Sucursal", branch_id)
        return branch

    def register(
        self,
        data: ConsumableChangeCreate,
        actor: Actor,
        branch_scope: Optional[int],
        can_manage: bool = False,
        now: Optional[datetime] = None,
    ) -> BranchConsumables:
        """Registers a blade mounted or the glue pot loaded.

        Every gate runs before anything is written. A change entered for the
        past (``changedAt``, the admin's) skips the early gate -- it is a
        correction, not a tap on the shop floor -- but still cannot land on
        top of another one.
        """
        now = now or datetime.utcnow()
        kind = data.type
        self._ensure_role(actor, kind)
        label = CONSUMABLE_LABELS[kind]

        condition: Optional[BladeCondition] = None
        quantity: Optional[float] = None
        if kind in BLADE_TYPES:
            if data.condition is None:
                raise ValidationError(
                    "Indica si el disco es nuevo o afilado", field="condition"
                )
            condition = data.condition
        else:
            quantity = (
                data.quantity
                if data.quantity is not None
                else self.settings.get_glue_load_kg()
            )
        if data.reason is not None and data.reason not in REASONS_BY_TYPE[kind]:
            raise ValidationError(
                f"Ese motivo no aplica a un cambio de {label}", field="reason"
            )

        branch = self._locked_branch(data.branch_id, branch_scope)
        in_force = self._changes([branch.id], kind)
        if data.changed_at is not None:
            if not can_manage:
                raise AuthorizationError(
                    "Solo el administrador registra un cambio pasado"
                )
            if data.changed_at > now:
                raise ValidationError(
                    "La fecha del cambio no puede ser futura", field="changedAt"
                )
            changed_at = data.changed_at
            if any(
                abs(change.changed_at - changed_at) <= MIN_CHANGE_GAP
                for change in in_force
            ):
                raise ConflictError(
                    f"Ya hay un cambio de {label} registrado a esa hora"
                )
        else:
            changed_at = now
            last_at = in_force[-1].changed_at if in_force else None
            gate = change_gate(last_at, now, self.production.schedule)
            if gate is Gate.blocked:
                raise ConflictError(
                    f"Ya se registró un cambio de {label} hace un momento"
                )
            if gate is Gate.early and data.reason is None:
                raise ValidationError(
                    f"El cambio anterior de {label} es reciente: indica el motivo",
                    field="reason",
                )

        self.db.add(
            ConsumableChangeModel(
                branch_id=branch.id,
                type=kind.value,
                changed_at=changed_at,
                changed_by=actor.user_id,
                changed_by_label=actor.label,
                condition=condition.value if condition else None,
                quantity=quantity,
                reason=data.reason.value if data.reason else None,
                note=(data.note or "").strip() or None,
                # One instant for the change and its registration: the undo
                # window counts from here.
                created_at=now,
                created_by=actor.user_id,
            )
        )
        branch_id = branch.id
        self.db.commit()
        return self._branch_status(actor, branch_id, now)

    def void(
        self,
        change_id: int,
        reason: Optional[str],
        actor: Actor,
        branch_scope: Optional[int],
        can_manage: bool = False,
        now: Optional[datetime] = None,
    ) -> BranchConsumables:
        """Takes a change back. The row stays, marked.

        Whoever registered it undoes it on their own for a few minutes, while
        it is still the latest (``rules.can_undo``); past that it is the
        admin's, with the reason. Idempotent.
        """
        now = now or datetime.utcnow()
        change = self.db.get(ConsumableChangeModel, change_id)
        if change is None or (
            branch_scope is not None and change.branch_id != branch_scope
        ):
            raise EntityNotFoundError("Cambio de insumo", change_id)
        if change.voided_at is None:
            kind = ConsumableType(change.type)
            self._ensure_role(actor, kind)
            reason = (reason or "").strip() or None
            in_force = self._changes([change.branch_id], kind)
            is_latest = bool(in_force) and in_force[-1].id == change.id
            if not can_undo(
                change.created_at, change.changed_by, actor.user_id, is_latest, now
            ):
                if not can_manage:
                    raise AuthorizationError(
                        "Solo puedes deshacer tu último cambio a los pocos minutos "
                        "de registrarlo; pide al administrador que lo anule"
                    )
                if reason is None:
                    raise ValidationError(
                        "Indica por qué se anula el cambio", field="reason"
                    )
            change.voided_at = now
            change.voided_by = actor.user_id
            change.voided_by_label = actor.label
            change.void_reason = reason
            change.updated_by = actor.user_id
        branch_id = change.branch_id
        self.db.commit()
        return self._branch_status(actor, branch_id, now)

    # ----------------------------------------------------------------- report
    def report(
        self,
        start: datetime,
        end: datetime,
        branch_id: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> ConsumableReport:
        """Consumption and yield over ``[start, end)``, per branch and consumable.

        A life is placed in the range by its END -- the day the blade came
        off -- like every other figure of the statistics. The ones in use come
        along whatever the range: they are today's snapshot.
        """
        now = now or datetime.utcnow()
        branches = self._branches(None, branch_id)
        all_lives = self._lives(branch.id for branch in branches)
        wanted = {
            key: [life for life in items if life.end is None or start <= life.end < end]
            for key, items in all_lives.items()
        }
        usage = self._usage(all_lives, wanted, now)
        schedule = self.production.schedule
        names = {branch.id: branch.name for branch in branches}

        def row(key: _Key, life: Life[ConsumableChangeModel]) -> ConsumableLife:
            opening, closing = life.opening, life.closing
            work = usage.get(opening.id, Usage())
            blade = key[1] in BLADE_TYPES
            return ConsumableLife(
                change_id=opening.id,
                branch_id=key[0],
                branch_name=names[key[0]],
                type=key[1],
                started_at=life.start,
                started_by_label=opening.changed_by_label,
                condition=opening.condition,
                quantity=opening.quantity,
                ended_at=life.end,
                ended_by_label=closing.changed_by_label if closing else None,
                end_reason=closing.reason if closing else None,
                end_note=closing.note if closing else None,
                meters=round(work.meters, 2),
                boards=round(work.units, 1) if blade else None,
                orders=None if blade else int(work.units),
                worked_hours=round(
                    worked_seconds(life.start, life.end or now, schedule) / 3600, 2
                ),
                in_use=life.end is None,
            )

        rows = {
            key: [row(key, life) for life in items] for key, items in wanted.items()
        }
        report = []
        for branch in branches:
            items = []
            for kind in ConsumableType:
                key = (branch.id, kind)
                mounted = [
                    life.opening
                    for life in all_lives.get(key, ())
                    if start <= life.start < end
                ]
                items.append(self._summary(kind, mounted, rows.get(key, [])))
            report.append(
                BranchConsumableReport(
                    branch_id=branch.id, branch_name=branch.name, items=items
                )
            )
        listed = [life for items in rows.values() for life in items]
        listed.sort(key=lambda life: (life.in_use, life.ended_at or life.started_at))
        return ConsumableReport(branches=report, lives=listed[::-1])

    @staticmethod
    def _summary(
        kind: ConsumableType,
        mounted: list[ConsumableChangeModel],
        rows: list[ConsumableLife],
    ) -> ConsumableSummary:
        blade = kind in BLADE_TYPES
        closed = [life for life in rows if not life.in_use]
        # A blade that broke says nothing of how long one lasts.
        sound = [life for life in closed if life.end_reason != ChangeReason.damaged]

        def yield_of(condition: BladeCondition) -> ConditionYield:
            group = [life for life in sound if life.condition == condition]
            return ConditionYield(
                lives=len(group),
                avg_meters=_rounded(average([life.meters for life in group])),
                avg_boards=_rounded(average([life.boards or 0.0 for life in group]), 1),
            )

        def count(condition: BladeCondition) -> int:
            return sum(1 for change in mounted if change.condition == condition.value)

        return ConsumableSummary(
            type=kind,
            changes=len(mounted),
            new_count=count(BladeCondition.new) if blade else None,
            sharpened_count=count(BladeCondition.sharpened) if blade else None,
            quantity=(
                None
                if blade
                else round(sum(change.quantity or 0.0 for change in mounted), 2)
            ),
            closed_lives=len(closed),
            damaged_lives=len(closed) - len(sound),
            avg_meters=_rounded(average([life.meters for life in sound])),
            avg_boards=(
                _rounded(average([life.boards or 0.0 for life in sound]), 1)
                if blade
                else None
            ),
            new=yield_of(BladeCondition.new) if blade else None,
            sharpened=yield_of(BladeCondition.sharpened) if blade else None,
            meters_per_kg=(
                None
                if blade
                else _rounded(
                    meters_per_kg((life.meters, life.quantity) for life in closed)
                )
            ),
            current=next((life for life in rows if life.in_use), None),
        )


def _rounded(value: Optional[float], digits: int = 2) -> Optional[float]:
    return None if value is None else round(value, digits)


def consumable_service(db: Session = Depends(get_db)) -> ConsumableService:
    """``ConsumableService`` provider for injection into routes."""
    return ConsumableService(db)
