# Design Decisions

**1. Indexes:** Unique indexes on `employees.emp_code` and `attendance_logs.emp_code + date` prevent duplicates and support lookups. Compound indexes support department filtering, date ranges, status searches, and latest punch-in queries. Stable names make index creation idempotent.

**2. Concurrent writes:** Unique constraints prevent duplicate records. Duplicate-key errors return HTTP 409. Punch-out uses an atomic update requiring `punch_out` to be null. Regularization checks existing values and records changes in audit history; conflicting updates return 409.

**3. Leaderboard ties:** MongoDB `$rank` assigns equal late-minute totals the same rank using competition ranking. Results are ordered by late minutes descending and employee code ascending. Ties at the limit may return extra rows.

**4. Headcount:** Headcount starts from eligible employees, not attendance logs. Employees who joined by the period end count even without attendance records; future joiners are excluded. Daily trends evaluate eligibility for each date.

**5. Scaling:** At 100x scale, measure query plans and latency using realistic data. Bound analytics by date and index lookup keys. Consider pre-aggregated daily summaries for expensive queries, with reliable updates and reconciliation. Add timeouts, metrics, structured logs, and load tests before considering sharding or caching.