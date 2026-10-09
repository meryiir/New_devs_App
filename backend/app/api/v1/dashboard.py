from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import text

from app.services.cache import get_revenue_summary
from app.core.auth import authenticate_request
from app.core.database_pool import get_db_session
from app.models.auth import AuthenticatedUser

router = APIRouter()


def require_tenant(current_user: AuthenticatedUser = Depends(authenticate_request)):
    if not current_user.tenant_id:
        raise HTTPException(403, "Tenant required")
    return current_user.tenant_id


@router.get("/dashboard/properties")
async def get_dashboard_properties(response: Response, tenant_id: str = Depends(require_tenant),
                                   session=Depends(get_db_session)):
    response.headers["Cache-Control"] = "no-store"
    result = await session.execute(text("""
        SELECT id, name, timezone FROM properties
        WHERE tenant_id = :tenant_id ORDER BY id
    """), {"tenant_id": tenant_id})
    return [dict(row) for row in result.mappings()]


@router.get("/dashboard/summary")
async def get_dashboard_summary(
    property_id: str, response: Response,
    month: int | None = Query(None, ge=1, le=12),
    year: int | None = Query(None, ge=1, le=9998),
    tenant_id: str = Depends(require_tenant), session=Depends(get_db_session),
):
    if (month is None) != (year is None):
        raise HTTPException(422, "Month and year must be provided together")
    response.headers["Cache-Control"] = "no-store"
    data = await get_revenue_summary(property_id, tenant_id, month, year, session)
    return {
        "property_id": data["property_id"], "total_revenue": data["total"],
        "currency": data["currency"], "reservations_count": data["count"],
        "totals_by_currency": data["totals_by_currency"],
        "month": data["month"], "year": data["year"], "timezone": data["timezone"],
    }
