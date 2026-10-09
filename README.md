## Bugs fixed

- **Tenant isolation:** revenue cache keys now include the tenant, property, reporting period, and timezone. Property ownership is checked before cache reads, and database queries filter by tenant. The property selector loads only the signed-in client's properties.
- **Authentication:** removed the mock-token bypass and password-free login fallback. Tenant identity comes from server-controlled metadata; missing membership is rejected. Token signatures and expiration are checked before cached authentication is used.
- **Monthly revenue:** replaced the unused monthly placeholder with database calculations using the property's local check-in month. Inclusive start and exclusive end boundaries account for daylight-saving changes. The dashboard sends the selected month and year.
- **Financial accuracy:** amounts remain decimal through aggregation and final rounding, then travel as strings to the browser. Currencies are reported separately. Database failures no longer produce fabricated revenue; the async connection setup now uses the configured database URL.
