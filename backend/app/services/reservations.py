"""Revenue recognized in the property's local check-in month, per currency."""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import text


# Supported reporting currencies; never silently guess a currency's minor unit.
CURRENCY_PLACES = {"USD": 2, "EUR": 2, "GBP": 2, "CAD": 2, "AUD": 2,
                   "CHF": 2, "JPY": 0, "KWD": 3, "BHD": 3, "TND": 3}


def monthly_bounds(month: int, year: int, timezone_name: str):
    zone = ZoneInfo(timezone_name)
    start = datetime(year, month, 1, tzinfo=zone)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=zone)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def format_money(amount: Decimal, currency: str) -> str:
    if currency not in CURRENCY_PLACES:
        raise HTTPException(422, "Unsupported reporting currency")
    quantum = Decimal(1).scaleb(-CURRENCY_PLACES[currency])
    return format(amount.quantize(quantum, rounding=ROUND_HALF_UP), "f")


async def get_property(session, property_id: str, tenant_id: str):
    if not tenant_id:
        raise HTTPException(403, "Tenant required")
    result = await session.execute(text("""
        SELECT id, name, timezone FROM properties
        WHERE id = :property_id AND tenant_id = :tenant_id
    """), {"property_id": property_id, "tenant_id": tenant_id})
    prop = result.mappings().first()
    if prop is None:
        raise HTTPException(404, "Property not found")
    return prop


async def calculate_total_revenue(property_id: str, tenant_id: str,
                                  month=None, year=None, db_session=None):
    if db_session is None:
        from app.core.database_pool import db_pool
        await db_pool.initialize()
        async with db_pool.get_session() as session:
            return await calculate_total_revenue(property_id, tenant_id, month, year, session)

    prop = await get_property(db_session, property_id, tenant_id)
    params = {"property_id": property_id, "tenant_id": tenant_id}
    period_filter = ""
    if (month is None) != (year is None):
        raise HTTPException(422, "Month and year must be provided together")
    if month is not None:
        if not 1 <= month <= 12 or not 1 <= year <= 9998:
            raise HTTPException(422, "Invalid reporting period")
        params["start"], params["end"] = monthly_bounds(month, year, prop["timezone"])
        period_filter = "AND check_in_date >= :start AND check_in_date < :end"
    result = await db_session.execute(text(f"""
        SELECT currency, SUM(total_amount) AS total, COUNT(*) AS count
        FROM reservations
        WHERE property_id = :property_id AND tenant_id = :tenant_id
        {period_filter}
        GROUP BY currency ORDER BY currency
    """), params)
    totals = [{"currency": row.currency, "total": format_money(row.total, row.currency),
               "count": row.count} for row in result]
    return {
        "property_id": property_id, "tenant_id": tenant_id,
        "month": month, "year": year, "timezone": prop["timezone"],
        "totals_by_currency": totals,
        # Preserve scalar fields for single-currency clients; never add currencies.
        "total": totals[0]["total"] if len(totals) == 1 else ("0.00" if not totals else None),
        "currency": totals[0]["currency"] if len(totals) == 1 else None,
        "count": sum(item["count"] for item in totals),
    }


async def calculate_monthly_revenue(property_id: str, month: int, year: int,
                                    tenant_id: str, db_session=None) -> Decimal:
    result = await calculate_total_revenue(property_id, tenant_id, month, year, db_session)
    if result["total"] is None:
        raise HTTPException(422, "Multiple currencies require separate totals")
    return Decimal(result["total"])
