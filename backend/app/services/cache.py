import json
import logging
import os

import redis.asyncio as redis
from redis.exceptions import RedisError

from app.services.reservations import calculate_total_revenue, get_property

logger = logging.getLogger(__name__)
redis_client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))


async def get_revenue_summary(property_id: str, tenant_id: str, month=None, year=None,
                              db_session=None):
    if db_session is None:
        from app.core.database_pool import db_pool
        await db_pool.initialize()
        async with db_pool.get_session() as session:
            return await get_revenue_summary(property_id, tenant_id, month, year, session)

    # Authorization also runs on cache hits, including after ownership changes.
    prop = await get_property(db_session, property_id, tenant_id)
    identity = [tenant_id, property_id, year, month, prop["timezone"]]
    cache_key = "revenue:v2:" + json.dumps(identity, separators=(",", ":"))
    try:
        cached = await redis_client.get(cache_key)
        if cached:
            result = json.loads(cached)
            if (isinstance(result, dict) and result.get("tenant_id") == tenant_id
                    and result.get("property_id") == property_id
                    and result.get("year") == year and result.get("month") == month
                    and result.get("timezone") == prop["timezone"]):
                return result
    except (RedisError, ValueError, TypeError):
        logger.warning("Revenue cache unavailable or invalid; querying database")

    result = await calculate_total_revenue(property_id, tenant_id, month, year, db_session)
    try:
        await redis_client.setex(cache_key, 300, json.dumps(result))
    except RedisError:
        logger.warning("Revenue cache write failed")
    return result
