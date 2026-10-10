# Starter-code review

The original starter is intentionally incomplete. The following defects are in the provided `main.py`; the replacement implementation is in `app/main.py`.

| # | Location / area | Defect and impact | Test that exposes it | Fix in replacement |
|---:|---|---|---|---|
| 1 | `health()` | Always returns 200 even if MongoDB is down, so the runner can test an unready app. | Stop MongoDB and call `/health`. | Ping MongoDB and return 503 on failure. |
| 2 | Mongo client/config | No explicit connection timeout; readiness can hang too long. | Point `MONGO_URI` to an unreachable host. | Configure a bounded server-selection timeout. |
| 3 | Startup | No indexes are created. | Start twice and inspect indexes. | Create named indexes idempotently at startup. |
| 4 | `create_employee()` | Check-then-insert is race-prone; duplicate codes can both pass the check. | Fire simultaneous POSTs with the same `emp_code`. | Unique database index; map duplicate-key errors to 409. |
| 5 | `create_employee()` | `created_at` is a naive local datetime rather than a UTC instant returned as epoch milliseconds. | Compare response with contract; vary host timezone. | Store UTC BSON datetime and serialize to epoch milliseconds. |
| 6 | Employee response | Internal Mongo `_id` handling is inconsistent with contract, and the starter returns fields in the wrong timestamp representation. | Assert response keys and types. | Never expose `_id`; return contract fields only. |
| 7 | `list_employees()` | `skip = page * page_size` skips the first page. | Request page 1 with more than 20 employees. | Use `(page - 1) * page_size`. |
| 8 | `list_employees()` | `total` counts all employees even when department filter is applied. | Filter to a department with fewer employees. | Count the filtered query. |
| 9 | `list_employees()` | Missing page/page-size bounds and no explicit sort. | Try page 0, page_size 101; insert out-of-order codes. | Validate pagination and sort by `emp_code`. |
| 10 | `compute_late_minutes()` | Uses naive timestamps and minute arithmetic that does not robustly implement whole-second truncation and the strict grace boundary. | Test 09:40:00.900 and 09:40:01 for a 09:30 shift. | Normalize instants and apply strict >600-second rule. |
| 11 | `compute_work_hours()` | Python `round()` uses ties-to-even behavior and binary floating-point, not decimal half-up. | Use a duration that rounds exactly at a half-cent boundary. | Decimal half-up rounding. |
| 12 | `compute_overtime()` | Does not handle overnight shift end on the next date and has no 30-minute minimum threshold. | Test an overnight shift and 29:59 / 30:00 overtime. | Resolve shift end date and enforce >=30 minutes. |
| 13 | `punch_in()` | Unknown employee is not checked before dereferencing `emp`; this can become a 500. | Punch in with an unknown code. | Return 404. |
| 14 | `punch_in()` | Uses local-time `fromtimestamp`, so attendance date can vary by server timezone; no whole-second truncation. | Submit epoch milliseconds near IST midnight. | Convert UTC instant to IST for date assignment and truncate seconds. |
| 15 | `punch_in()` | Check-then-insert permits duplicate same-day records under concurrency; status is not validated to allowed presence values. | Parallel duplicate requests and invalid status request. | Unique natural-key index and typed status validation. |
| 16 | `punch_in()` | Response adds an `id` field and leaves timestamp types inconsistent with the API contract. | Assert response schema. | Serialize only documented fields and epoch milliseconds. |
| 17 | `list_attendance()` | Loads all matching documents into memory, sorts in Python, and slices afterward; does not scale to ~100k records. | Seed large collection and inspect memory/query plan. | Filter, sort, facet-count, skip and limit in MongoDB. |
| 18 | `list_attendance()` | Sort is only by date, missing `emp_code` tie-break; total is computed from materialized results. | Create multiple employees with same date. | Sort by date descending then code ascending; count in the same aggregation. |
| 19 | `list_attendance()` | Does not validate date ordering, date syntax, status enum, or pagination bounds. | Try invalid dates, reversed range, invalid status, page_size 101. | Validate query parameters and return 422. |
| 20 | Remaining routes | Punch-out, regularization/audit history, analytics, trend, and explain endpoints are absent. | Call each contract path. | Implement all contract paths in `app/main.py`. |

## Inspected starter behaviors that were not themselves defects

- The starter uses `load_dotenv()` with the default non-overriding behavior, so real environment variables retain precedence over `.env` values.
- The starter's natural employee identifier is `emp_code`, rather than a user-supplied MongoDB `_id`.
- The initial `history` value is an empty list, which is the correct initial value for a never-corrected record.
