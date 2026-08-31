from fastapi import APIRouter, Depends

from src.modules.settings.schemas import (
    CompanySettingsResponse,
    CompanySettingsUpdate,
    CuttingSettingsResponse,
    CuttingSettingsUpdate,
    PreOrderSettingsResponse,
    PreOrderSettingsUpdate,
    TaxSettingsResponse,
    TaxSettingsUpdate,
)
from src.modules.settings.service import SettingsService, settings_service
from src.modules.users.dependencies import require_permission
from src.shared.responses import ERROR_RESPONSES, DataResponse, ok

# System configuration: "administrador" only (RESOURCE_ROLES["settings:manage"]).
router = APIRouter(
    prefix="/settings",
    tags=["settings"],
    responses=ERROR_RESPONSES,
    dependencies=[Depends(require_permission("settings:manage"))],
)


@router.get("/cutting", response_model=DataResponse[CuttingSettingsResponse])
def get_cutting_settings(svc: SettingsService = Depends(settings_service)):
    """Returns the current cutting parameters (seeded from config if missing)."""
    return ok(svc.get_or_init())


@router.patch("/cutting", response_model=DataResponse[CuttingSettingsResponse])
def update_cutting_settings(
    data: CuttingSettingsUpdate, svc: SettingsService = Depends(settings_service)
):
    """Partially updates the cutting parameters."""
    return ok(svc.update_cutting(data))


@router.get("/preorders", response_model=DataResponse[PreOrderSettingsResponse])
def get_preorder_settings(svc: SettingsService = Depends(settings_service)):
    """Returns the current pre-order config (seeded from config if missing)."""
    return ok(svc.get_or_init())


@router.patch("/preorders", response_model=DataResponse[PreOrderSettingsResponse])
def update_preorder_settings(
    data: PreOrderSettingsUpdate, svc: SettingsService = Depends(settings_service)
):
    """Partially updates the pre-order validity period and cap."""
    return ok(svc.update_preorders(data))


@router.get("/company", response_model=DataResponse[CompanySettingsResponse])
def get_company_settings(svc: SettingsService = Depends(settings_service)):
    """Returns the company data (proforma letterhead)."""
    return ok(svc.get_company())


@router.patch("/company", response_model=DataResponse[CompanySettingsResponse])
def update_company_settings(
    data: CompanySettingsUpdate, svc: SettingsService = Depends(settings_service)
):
    """Partially updates the company data."""
    svc.update_company(data)
    return ok(svc.get_company())


@router.get("/taxes", response_model=DataResponse[TaxSettingsResponse])
def get_tax_settings(svc: SettingsService = Depends(settings_service)):
    """Returns the current sales tax rate (seeded from config if missing).

    Not readable by the seller on purpose: nothing in the quoting UI needs to
    look the rate up, because every priced response already carries the
    ``taxRate`` it was computed with.
    """
    return ok(svc.get_or_init())


@router.patch("/taxes", response_model=DataResponse[TaxSettingsResponse])
def update_tax_settings(
    data: TaxSettingsUpdate, svc: SettingsService = Depends(settings_service)
):
    """Updates the sales tax rate (admin only)."""
    return ok(svc.update_taxes(data))
