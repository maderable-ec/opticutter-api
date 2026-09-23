from fastapi import APIRouter, Depends

from src.modules.optimizations.schemas import (
    LayoutCandidatesRequest,
    LayoutCandidatesResponse,
    LayoutEvaluateResponse,
    OptimizeRequest,
    OptimizeResponse,
)
from src.modules.optimizations.service import OptimizationService, optimization_service
from src.modules.users.dependencies import require_permission
from src.shared.responses import ERROR_RESPONSES, DataResponse, ok

# Optimizer: "administrador" and "vendedor" (RESOURCE_ROLES["optimizer"]).
router = APIRouter(
    prefix="/optimize",
    tags=["optimize"],
    responses=ERROR_RESPONSES,
    dependencies=[Depends(require_permission("optimizer"))],
)


@router.post("/", response_model=DataResponse[OptimizeResponse])
def optimize(
    request: OptimizeRequest,
    svc: OptimizationService = Depends(optimization_service),
):
    """Runs a cutting optimization (cache-first) and returns the solution."""
    return ok(svc.optimize_response(request))


@router.post("/layout/evaluate", response_model=DataResponse[LayoutEvaluateResponse])
def evaluate_layout(
    request: OptimizeRequest,
    svc: OptimizationService = Depends(optimization_service),
):
    """The plan with the layout editor's working adjustment applied.

    Refuses (422) a sheet that cannot be cut; pieces may stay pending. Returns,
    besides the plan, the working sheets of every pool with their derived
    layouts, the pieces, what is pending and which sheet types can be added.
    """
    return ok(svc.evaluate_layout(request))


@router.post(
    "/layout/candidates", response_model=DataResponse[LayoutCandidatesResponse]
)
def layout_candidates(
    request: LayoutCandidatesRequest,
    svc: OptimizationService = Depends(optimization_service),
):
    """Where a piece can go, how far an offcut can grow, which sizes a sheet takes."""
    return ok(svc.layout_candidates(request))
