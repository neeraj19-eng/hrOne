# Employee Attendance & Analytics API

HR attendance backend. FastAPI + MongoDB.

## Requirements

- Python 3.11+
- MongoDB 6.0 or newer (Atlas free tier is fine, or local `mongo:7`)

## Running

    python -m venv .venv
    source .venv/bin/activate        # Windows: .venv\Scripts\Activate.ps1
    pip install -r requirements.txt

Copy `.env.example` to `.env` and set:

    MONGO_URI=mongodb+srv://user:pass@cluster.mongodb.net/?retryWrites=true&w=majority
    MONGO_DB=attendance_db

Real environment variables override `.env`. If you run this in CI or behind a scheduler, leave `.env` out and set the two vars directly.

Seed the sample documents if you want:

    python sample_seed.py

Start:

    uvicorn app.main:app --port 8000

Confirm:

    curl http://127.0.0.1:8000/health
    # {"status":"ok"}

API docs at http://127.0.0.1:8000/docs

## Endpoints

`openapi.yaml` is the contract. The interesting ones:

- `POST /employees`, `GET /employees`
- `POST /attendance/punch-in`, `POST /attendance/punch-out`
- `GET /attendance`, `PATCH /attendance/{emp_code}/{date}`
- `GET /analytics/employees/{emp_code}/monthly`
- `GET /analytics/departments/summary`
- `GET /analytics/leaderboard/late`
- `GET /analytics/departments/{department}/trend`
- `GET /admin/explain/{endpoint}`

## Indexes

Created at startup. `create_index` is idempotent, so restarts are free.

## Concurrency

Punch-in uses the unique index on `(emp_code, date)` and turns duplicate-key errors into 409. Punch-out uses `find_one_and_update` filtered on `punch_out: null`, so only one of two simultaneous punches closes the record. PATCH uses the current `history` array as the optimistic-lock filter; if another writer beat it, the update matches nothing and the request returns 409 to retry with the new state.

## Notes

- Timestamps in the API are epoch milliseconds. In Mongo they are BSON datetimes.
- Calendar dates and shift times are strings in IST. That is intentional, see `DATA_MODEL.md`.
- Analytics run as MongoDB aggregation pipelines. There is no Python loop over records.

## What's missing

Nothing from the contract. All endpoints in `openapi.yaml` are implemented.