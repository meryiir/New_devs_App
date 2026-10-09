-- Independent check using PostgreSQL's timezone conversion, not the Python implementation.
SELECT p.tenant_id, p.id, p.timezone,
       COALESCE(SUM(r.total_amount), 0) AS march_total, COUNT(r.id) AS reservations
FROM properties p
LEFT JOIN reservations r ON r.property_id = p.id AND r.tenant_id = p.tenant_id
  AND r.check_in_date >= TIMESTAMP '2024-03-01' AT TIME ZONE p.timezone
  AND r.check_in_date < TIMESTAMP '2024-04-01' AT TIME ZONE p.timezone
GROUP BY p.tenant_id, p.id, p.timezone
ORDER BY p.tenant_id, p.id;

SELECT id, check_in_date, check_in_date AT TIME ZONE 'Europe/Paris' AS paris_check_in,
       total_amount, currency
FROM reservations WHERE id = 'res-tz-1';
