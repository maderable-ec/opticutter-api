from fastapi import APIRouter, Depends

from src.modules.settings.schemas import (
    CompanySettingsResponse,
    CompanySettingsUpdate,
    ConsumableSettingsResponse,
    ConsumableSettingsUpdate,
    CuttingSettingsResponse,
    CuttingSettingsUpdate,
    PreOrderSettingsResponse,
    PreOrderSettingsUpdate,
    StockSettingsResponse,
    StockSettingsUpdate,
    TaxSettingsResponse,
    TaxSettingsUpdate,
    WorkScheduleSettings,
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
    """Returns the company data (document letterhead)."""
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
    ``taxRate`` it was computed with, and every product its prices with tax
    (``ProductService.with_tax``).
    """
    return ok(svc.get_or_init())


@router.patch("/taxes", response_model=DataResponse[TaxSettingsResponse])
def update_tax_settings(
    data: TaxSettingsUpdate, svc: SettingsService = Depends(settings_service)
):
    """Updates the sales tax rate (admin only)."""
    return ok(svc.update_taxes(data))


@router.get("/stock", response_model=DataResponse[StockSettingsResponse])
def get_stock_settings(svc: SettingsService = Depends(settings_service)):
    """Returns the low-stock thresholds (seeded from config if missing).

    Read straight off the singleton row: the response field names are the
    column names minus the ``stock_threshold_`` prefix, which is what
    ``_STOCK_FIELD_MAP`` translates on the way in.
    """
    return ok(svc.get_stock_thresholds())


@router.patch("/stock", response_model=DataResponse[StockSettingsResponse])
def update_stock_settings(
    data: StockSettingsUpdate, svc: SettingsService = Depends(settings_service)
):
    """Partially updates the low-stock thresholds (admin only)."""
    svc.update_stock(data)
    return ok(svc.get_stock_thresholds())


@router.get("/consumables", response_model=DataResponse[ConsumableSettingsResponse])
def get_consumable_settings(svc: SettingsService = Depends(settings_service)):
    """Returns the consumables settings: the standard glue load, in kg."""
    return ok({"glue_load_kg": svc.get_glue_load_kg()})


@router.patch("/consumables", response_model=DataResponse[ConsumableSettingsResponse])
def update_consumable_settings(
    data: ConsumableSettingsUpdate, svc: SettingsService = Depends(settings_service)
):
    """Updates the standard glue load (admin only).

    It only moves what the shop floor's control starts at: every load already
    registered keeps the kg it took.
    """
    return ok({"glue_load_kg": svc.update_consumables(data)})


@router.get("/work-schedule", response_model=DataResponse[WorkScheduleSettings])
def get_work_schedule_settings(svc: SettingsService = Depends(settings_service)):
    """Returns the shop's working hours (the default until somebody edits them)."""
    return ok(WorkScheduleSettings.from_stored(svc.get_work_schedule().to_dict()))


@router.patch("/work-schedule", response_model=DataResponse[WorkScheduleSettings])
def update_work_schedule_settings(
    data: WorkScheduleSettings, svc: SettingsService = Depends(settings_service)
):
    """Replaces the working hours (admin only).

    Every hour the statistics report is read against them, past periods
    included: changing the schedule changes what last month's hours say.
    """
    schedule = svc.update_work_schedule(data)
    return ok(WorkScheduleSettings.from_stored(schedule.to_dict()))
