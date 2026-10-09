"""Run inside Compose: python -m unittest discover -s tests -v."""
import json
import os
import time
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import jwt
import psycopg2
import requests
from fastapi import HTTPException
from redis.exceptions import ConnectionError

from app.services import cache
from app.services.reservations import format_money, monthly_bounds
from app.core.tenant_resolver import TenantResolver


class MoneyAndDatesTests(unittest.TestCase):
    def test_round_once_after_exact_sum(self):
        self.assertEqual(format_money(sum(map(Decimal, ['333.333', '333.333', '333.334'])), 'USD'), '1000.00')
        self.assertEqual(format_money(Decimal('1.005'), 'USD'), '1.01')
        self.assertEqual(format_money(Decimal('-1.005'), 'USD'), '-1.01')
        self.assertEqual(format_money(Decimal('1.500'), 'JPY'), '2')
        self.assertEqual(format_money(Decimal('1.005'), 'KWD'), '1.005')
        with self.assertRaises(HTTPException):
            format_money(Decimal('10'), 'UNKNOWN')

    def test_month_boundaries_and_daylight_saving(self):
        for zone, start, end in [
            ('Europe/Paris', '2024-02-29T23:00:00+00:00', '2024-03-31T22:00:00+00:00'),
            ('America/New_York', '2024-03-01T05:00:00+00:00', '2024-04-01T04:00:00+00:00'),
        ]:
            with self.subTest(zone=zone):
                bounds = monthly_bounds(3, 2024, zone)
                self.assertEqual(tuple(d.isoformat() for d in bounds), (start, end))
        self.assertEqual(monthly_bounds(12, 2024, 'UTC')[1], datetime(2025, 1, 1, tzinfo=timezone.utc))
        start, end = monthly_bounds(2, 2024, 'UTC')
        self.assertEqual((end - start).days, 29)

    def test_user_metadata_and_missing_tenant_are_not_authority(self):
        self.assertIsNone(TenantResolver.resolve_tenant_from_token({'user_metadata': {'tenant_id': 'tenant-a'}}))
        self.assertIsNone(TenantResolver.resolve_tenant_from_token({}))
        self.assertEqual(TenantResolver.resolve_tenant_from_token({
            'user_metadata': {'tenant_id': 'tenant-a'}, 'app_metadata': {'tenant_id': 'tenant-b'}
        }), 'tenant-b')


class CacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.entries = {}
        self.redis = AsyncMock()
        self.redis.get.side_effect = lambda key: self.entries.get(key)
        self.redis.setex.side_effect = lambda key, ttl, value: self.entries.__setitem__(key, value)
        self.prop = patch.object(cache, 'get_property', new=AsyncMock(return_value={'timezone': 'UTC'}))
        self.auth = self.prop.start()
        self.addCleanup(self.prop.stop)
        self.rp = patch.object(cache, 'redis_client', self.redis)
        self.rp.start()
        self.addCleanup(self.rp.stop)

        async def calculate(prop, tenant, month, year, session):
            return {'property_id': prop, 'tenant_id': tenant, 'month': month, 'year': year,
                    'timezone': 'UTC', 'total': '1.01', 'currency': 'USD', 'count': 1}
        self.cp = patch.object(cache, 'calculate_total_revenue', new=AsyncMock(side_effect=calculate))
        self.calculate = self.cp.start()
        self.addCleanup(self.cp.stop)

    async def test_tenants_periods_and_old_keys_never_share_results(self):
        self.entries['revenue:prop-001'] = json.dumps({'tenant_id': 'tenant-a', 'total': '999'})
        for tenant, month, year in [('tenant-a', 3, 2024), ('tenant-b', 3, 2024),
                                    ('tenant-a', 4, 2024), ('tenant-a', 3, 2025),
                                    ('tenant-a', None, None)]:
            for _ in range(2):
                result = await cache.get_revenue_summary('prop-001', tenant, month, year, object())
                self.assertEqual((result['tenant_id'], result['month'], result['year']), (tenant, month, year))
        self.assertEqual(self.calculate.await_count, 5)

    async def test_ownership_checked_on_cache_hit(self):
        await cache.get_revenue_summary('prop-001', 'tenant-a', 3, 2024, object())
        self.auth.side_effect = HTTPException(404, 'Property not found')
        with self.assertRaises(HTTPException):
            await cache.get_revenue_summary('prop-001', 'tenant-a', 3, 2024, object())

    async def test_wrong_tenant_payload_and_corrupt_json_are_discarded(self):
        for payload in ['{broken', json.dumps({'tenant_id': 'tenant-b'})]:
            self.redis.get.side_effect = None
            self.redis.get.return_value = payload
            result = await cache.get_revenue_summary('prop-001', 'tenant-a', 3, 2024, object())
            self.assertEqual(result['tenant_id'], 'tenant-a')

    async def test_cache_failure_uses_database_but_database_failure_propagates(self):
        self.redis.get.side_effect = ConnectionError()
        self.redis.setex.side_effect = ConnectionError()
        result = await cache.get_revenue_summary('prop-001', 'tenant-a', 3, 2024, object())
        self.assertEqual(result['total'], '1.01')
        self.calculate.side_effect = RuntimeError('database unavailable')
        with self.assertRaises(RuntimeError):
            await cache.get_revenue_summary('prop-001', 'tenant-a', 3, 2024, object())


class DatabaseFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_never_fabricates_revenue_on_database_failure(self):
        from app.services.reservations import calculate_total_revenue
        session = AsyncMock()
        session.execute.side_effect = OSError('connection refused')
        with self.assertRaises(OSError):
            await calculate_total_revenue('prop-001', 'tenant-a', 3, 2024, session)

    async def test_database_dependency_returns_service_unavailable(self):
        from app.core.database_pool import db_pool, get_db_session
        with patch.object(db_pool, 'initialize', new=AsyncMock()), \
                patch.object(db_pool, 'session_factory', None):
            with self.assertRaises(HTTPException) as error:
                await anext(get_db_session())
            self.assertEqual(error.exception.status_code, 503)


class LiveAPITests(unittest.TestCase):
    """Full HTTP -> auth -> Redis -> PostgreSQL checks. Requires seeded Compose."""
    base = os.getenv('TEST_API_URL', 'http://localhost:8000/api/v1')

    @classmethod
    def setUpClass(cls):
        cls.headers = {}
        for tenant, email, password in [('a', 'sunset@propertyflow.com', 'client_a_2024'),
                                        ('b', 'ocean@propertyflow.com', 'client_b_2024')]:
            response = requests.post(cls.base + '/auth/login', json={'email': email, 'password': password}, timeout=15)
            response.raise_for_status()
            cls.headers[tenant] = {'Authorization': 'Bearer ' + response.json()['access_token']}
        cls.db = psycopg2.connect(os.environ['DATABASE_URL'])
        cls.db.autocommit = True
        cls.property_id = 'regression-' + uuid4().hex
        cls.addClassCleanup(cls.cleanup)
        with cls.db.cursor() as cur:
            for tenant, zone in [('tenant-a', 'Europe/Paris'), ('tenant-b', 'America/New_York')]:
                cur.execute('INSERT INTO properties (id, tenant_id, name, timezone) VALUES (%s,%s,%s,%s)',
                            (cls.property_id, tenant, 'Regression fixture', zone))
            # Exactly outside/inside both local month boundaries, including DST offset change.
            for tenant, dates in [
                ('tenant-a', ['2024-02-29T22:59:59Z', '2024-02-29T23:00:00Z', '2024-03-31T21:59:59Z', '2024-03-31T22:00:00Z']),
                ('tenant-b', ['2024-03-01T04:59:59Z', '2024-03-01T05:00:00Z', '2024-04-01T03:59:59Z', '2024-04-01T04:00:00Z']),
            ]:
                for date, amount in zip(dates, ['100', '1.005', '2.000', '200']):
                    cls.insert(cur, tenant, date, amount, 'USD')
            for amount, currency in [('333.333', 'EUR'), ('333.333', 'EUR'), ('333.334', 'EUR'),
                                     ('1.500', 'JPY'), ('1.005', 'KWD')]:
                cls.insert(cur, 'tenant-a', '2024-03-15T12:00:00Z', amount, currency)

    @classmethod
    def insert(cls, cur, tenant, date, amount, currency):
        cur.execute('''INSERT INTO reservations
            (id, property_id, tenant_id, check_in_date, check_out_date, total_amount, currency)
            VALUES (%s,%s,%s,%s,%s::timestamptz + interval '1 day',%s,%s)''',
                    (uuid4().hex, cls.property_id, tenant, date, date, amount, currency))

    @classmethod
    def cleanup(cls):
        with cls.db.cursor() as cur:
            cur.execute('DELETE FROM reservations WHERE property_id=%s', (cls.property_id,))
            cur.execute('DELETE FROM properties WHERE id=%s', (cls.property_id,))
        cls.db.close()

    def summary(self, tenant, prop='prop-001', month=3, year=2024, **kwargs):
        return requests.get(self.base + '/dashboard/summary', headers=self.headers[tenant],
                            params={'property_id': prop, 'month': month, 'year': year}, timeout=15, **kwargs)

    def test_all_seed_properties_for_both_accounts(self):
        for tenant, prop, total, count in [
            ('a', 'prop-001', '2250.00', 4), ('a', 'prop-002', '4975.50', 4),
            ('a', 'prop-003', '6100.50', 2), ('b', 'prop-001', '0.00', 0),
            ('b', 'prop-004', '1776.50', 4), ('b', 'prop-005', '3256.00', 3),
        ]:
            with self.subTest(tenant=tenant, property=prop):
                response = self.summary(tenant, prop)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['total_revenue'], total)
                self.assertEqual(response.json()['reservations_count'], count)
                self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_alternating_and_parallel_refreshes(self):
        from concurrent.futures import ThreadPoolExecutor
        def check(tenant):
            response = self.summary(tenant)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['total_revenue'], '2250.00' if tenant == 'a' else '0.00')
        for tenant in ['a', 'b'] * 10:
            check(tenant)
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(check, ['b', 'a'] * 10))

    def test_foreign_properties_forbidden_even_with_warm_cache(self):
        for owner, stranger, prop in [('a', 'b', 'prop-002'), ('b', 'a', 'prop-004')]:
            self.assertEqual(self.summary(owner, prop).status_code, 200)
            self.assertEqual(self.summary(stranger, prop).status_code, 404)
        self.assertEqual(self.summary('a', 'nonexistent').status_code, 404)

    def test_property_lists_are_isolated(self):
        for tenant, expected, first_name in [
            ('a', {'prop-001', 'prop-002', 'prop-003'}, 'Beach House Alpha'),
            ('b', {'prop-001', 'prop-004', 'prop-005'}, 'Mountain Lodge Beta'),
        ]:
            response = requests.get(self.base + '/dashboard/properties', headers=self.headers[tenant], timeout=15)
            self.assertEqual(response.status_code, 200)
            rows = [row for row in response.json() if row['id'] != self.property_id]
            self.assertEqual({row['id'] for row in rows}, expected)
            self.assertEqual(rows[0]['name'], first_name)

    def test_real_sql_boundaries_and_currency_rounding(self):
        for tenant in ['a', 'b']:
            response = self.summary(tenant, self.property_id)
            self.assertEqual(response.status_code, 200, response.text)
            totals = {item['currency']: item['total'] for item in response.json()['totals_by_currency']}
            self.assertEqual(totals['USD'], '3.01')
            if tenant == 'a':
                self.assertEqual(totals, {'USD': '3.01', 'EUR': '1000.00', 'JPY': '2', 'KWD': '1.005'})
                self.assertIsNone(response.json()['total_revenue'])
            for month, expected in [(2, '100.00'), (4, '200.00')]:
                self.assertEqual(self.summary(tenant, self.property_id, month).json()['total_revenue'], expected)

    def test_invalid_periods(self):
        for params in [{'month': 13, 'year': 2024}, {'month': 0, 'year': 2024},
                       {'month': 3}, {'year': 2024}, {'month': 3, 'year': 9999}]:
            response = requests.get(self.base + '/dashboard/summary', headers=self.headers['a'],
                                    params={'property_id': 'prop-001', **params}, timeout=15)
            self.assertEqual(response.status_code, 422)

    def test_invalid_authentication(self):
        for token in ['', 'mock-token-123', jwt.encode({'email': 'candidate@propertyflow.com'}, 'wrong-secret', algorithm='HS256')]:
            response = requests.get(self.base + '/dashboard/summary', params={'property_id': 'prop-001'},
                                    headers={'Authorization': 'Bearer ' + token}, timeout=15)
            self.assertEqual(response.status_code, 401)
        for email in ['candidate@propertyflow.com', 'sunset@propertyflow.com', 'ocean@propertyflow.com']:
            response = requests.post(self.base + '/auth/login', json={'email': email, 'password': 'wrong'}, timeout=15)
            self.assertEqual(response.status_code, 401)

    def test_missing_tenant_and_user_editable_metadata_rejected(self):
        token = jwt.encode({'id': 'unknown', 'email': 'unknown@example.com', 'aud': 'authenticated',
                            'exp': int(time.time()) + 60, 'user_metadata': {'tenant_id': 'tenant-a'}},
                           os.environ['SECRET_KEY'], algorithm='HS256')
        response = requests.get(self.base + '/dashboard/summary', params={'property_id': 'prop-001'},
                                headers={'Authorization': 'Bearer ' + token}, timeout=15)
        self.assertEqual(response.status_code, 403)

    def test_expired_token_rejected_after_cache_warmup(self):
        token = jwt.encode({'id': 'expiry-test', 'email': 'expiry@example.com', 'aud': 'authenticated',
                            'exp': int(time.time()) + 2, 'app_metadata': {'tenant_id': 'tenant-a'}},
                           os.environ['SECRET_KEY'], algorithm='HS256')
        headers = {'Authorization': 'Bearer ' + token}
        url = self.base + '/dashboard/summary?property_id=prop-001'
        self.assertEqual(requests.get(url, headers=headers, timeout=15).status_code, 200)
        time.sleep(3)
        self.assertEqual(requests.get(url, headers=headers, timeout=15).status_code, 401)

    def test_simulation_header_cannot_change_tenant(self):
        headers = {**self.headers['b'], 'X-Simulated-Tenant': 'tenant-a'}
        response = requests.get(self.base + '/dashboard/summary', params={'property_id': 'prop-001'}, headers=headers, timeout=15)
        self.assertEqual(response.json()['total_revenue'], '0.00')


if __name__ == '__main__':
    unittest.main()
