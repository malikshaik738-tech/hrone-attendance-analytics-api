# Employee Attendance & Analytics API

FastAPI + MongoDB implementation for the supplied HROne Software Engineer Trainee assignment contract.

## Requirements

- Python 3.11+
- MongoDB 6+ (MongoDB 7 is recommended to match the grader)

## Run locally

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Set `MONGO_URI` and `MONGO_DB` in your shell or a local `.env` file. Environment variables take precedence over `.env`.

```bash
uvicorn app.main:app --port 8000
```

Then check `http://127.0.0.1:8000/health` and `http://127.0.0.1:8000/docs`. The health endpoint returns 200 only when MongoDB answers a ping. Indexes are created idempotently at application startup.

## Important implementation choices

- `emp_code` is the employee's public natural key. MongoDB `_id` is never included in API responses.
- Attendance is uniquely keyed by `(emp_code, date)`; unique indexes enforce concurrent duplicate protection.
- Stored instants are BSON datetimes in UTC; API timestamps are integer epoch milliseconds. Inputs are truncated to whole seconds.
- Regularization uses a compare-and-set update and appends one immutable history entry; a concurrent edit can receive 409 rather than lose an audit entry.
- Lists filter, sort, paginate, and count in MongoDB. Analytics use aggregation pipelines; the department trend builds its date calendar in MongoDB rather than looping over days in Python.
- Never commit `.env`, credentials, database dumps, virtual environments, or secrets. Do not publish the assignment statement or solution before the deadline specified by the recruiter.

## Before submission

Run the app against MongoDB 7 and test every contract edge case, especially overnight shifts, exact 10-minute grace boundaries, 30-minute overtime threshold, half-up rounding, concurrent requests, audit history, analytics with no logs, and `explain` output on a seeded ~100k-record dataset. This workspace could not be integration-tested here because no MongoDB service is attached to this environment.

## Candidate-kit sample data

The candidate kit includes `sample_data/employees.json`, `sample_data/attendance_logs.json`,
and `sample_seed.py` for local testing. Keep these as local test fixtures if desired; do not
commit data dumps to the public submission repository. The grader supplies its own database
and dataset.
