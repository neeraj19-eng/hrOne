# REVIEW.md

I went through the starter `app/main.py` line by line, wrote small tests against each rule in `openapi.yaml`, and hit the following issues. Order is roughly the order I found them.

## Helpers

**1. `/health` lies.**
The endpoint always returns `{"status": "ok"}`. It never touches Mongo. If the database is down, the readiness probe still passes and the grader starts firing tests that all fail. Fix: ping the DB and only return 200 when the ping succeeds.

**2. `compute_late_minutes` has no grace period.**
The starter returns minutes whenever they exceed 10. R2 is stricter than that. A punch at exactly 10:00 past shift start is not late. A punch one second later is, and then `late_minutes` is the entire elapsed minutes since shift start, not the minutes since the grace ended. The starter is close but wrong at the boundary.

It also does its math on naive local time. On a machine that isn't set to IST, the result is off by hours. Fix: shift to IST first, compute seconds since shift start, return 0 if <= 600, otherwise the floor of the minutes.

**3. `compute_work_hours` uses `round()`.**
Python rounds half to even. R4 wants half up. The difference shows up with values like 2.675: Python gives 2.67, R4 expects 2.68. Fix: `Decimal` with `ROUND_HALF_UP`.

**4. `compute_overtime` ignores the 30-minute floor.**
R3 counts overtime only when the gap between shift end and punch out is at least 30 minutes. The starter counts everything above zero, so leaving 5 minutes late records 5 minutes of overtime that should be zero. Fix: gate on `>= 30`.

**5. `compute_overtime` uses the wrong day for overnight shifts.**
The starter builds the shift-end instant from the attendance date. For 22:00-06:00 that is the wrong day; the shift ends tomorrow. Fix: if `shift_end <= shift_start`, add a day to the shift end.

**6. `compute_overtime` mixes naive and aware datetimes.**
`datetime.fromisoformat(date_str)` returns naive. Tagging it with `.replace(tzinfo=IST)` gives an aware IST, but the punch_out coming from Mongo is naive UTC. Subtracting them gives a wrong number. I first saw this on an overnight employee, off by exactly 5h30m. Fix: convert both to aware IST before subtracting.

**7. Milliseconds survive where they shouldn't.**
R1 says truncate to whole seconds before any calculation, and store the truncated value. The starter keeps whatever the client sent. I sent `1783312500123` and got that exact value back. Fix: `replace(microsecond=0)` on every instant from the API boundary.

## Employees

**8. `create_employee` does check-then-insert.**
Not safe. Two parallel POSTs with the same code both see "not found", both insert. I fired 5 simultaneous requests and got 5 × 201. Fix: rely on a unique index, catch `DuplicateKeyError`, return 409.

**9. `create_employee` writes a naive local `created_at`.**
`datetime.now()` has no timezone. The epoch ms in the response is wrong whenever the server isn't UTC. Fix: `datetime.now(timezone.utc)`.

**10. `create_employee` allows `shift_start == shift_end`.**
The contract forbids it. The starter accepts it. Fix: check first, 422 if equal.

**11. No unique index on `emp_code`.**
Without it, #8 has no backstop. Fix: create it at startup.

## Pagination

**12. `list_employees` skips the wrong amount.**
`skip = page * page_size` means page 1 starts at item 21. Fix: `(page - 1) * page_size`.

**13. `list_employees` uses the wrong `total`.**
R10 says `total` reflects the filters. `?department=Sales` returns 2 items but still reports `total: 6`. Fix: `count_documents(q)`.

**14. `list_employees` does not bound page values.**
`page_size` must cap at 100, `page` must be at least 1. The starter accepts `page_size=10000`. Fix with Query bounds.

## Punch-in

**15. `punch_in` 500s on unknown employee.**
`emp["shift_start"]` on `None` is a `KeyError`. Fix: check first, 404.

**16. `punch_in` uses local date for the attendance day.**
R1 wants the IST calendar date of the punch. The starter uses `ts.date()` on a naive local datetime. Fix: convert to IST.

**17. `punch_in` ignores the overnight rule.**
An overnight worker punching in at 02:00 IST belongs to the previous day's shift. The starter dates it today. Fix: apply R1's overnight branch.

**18. `punch_in` is not race-safe.**
Two parallel punches both pass the find_one guard. Fix: same unique index as #11, catch `DuplicateKeyError`, return 409.

**19. `punch_in` returns `"id"`.**
Not in the contract. `_id` is internal. Fix: remove it.

**20. `punch_in` does not validate `punched_at`.**
Seconds-looking integers, floats, and strings must all be 422. I sent `12345` and it went through. Fix: bound the Pydantic field to epoch-millis range.

**21. `punch_in` accepts any status string.**
Only PRESENT, WFH, ON_DUTY. Fix: enum validator.

**22. `punch_in` does not initialize all derived fields.**
The response must contain every field in the schema, even when they are defaults. Fix: write them out explicitly.

## Attendance list

**23. `list_attendance` loads everything into memory.**
`list(db.attendance_logs.find(q))` pulls the whole match set into Python, sorts it, then slices. At 100k records this is unusable. Fix: query-side filter, sort, skip, and limit, with `count_documents` for `total`.

**24. `list_attendance` sorts in Python.**
No stable tiebreak and no index use. Fix: `.sort([("date", -1), ("emp_code", 1)])` at the database.

**25. `list_attendance` returns `"id"`.**
Same problem as #19. Fix: remove it.

**26. `list_attendance` allows `date_from > date_to`.**
Spec: 422. Starter: empty list, no error. Fix: explicit 422.

**27. `list_attendance` does not bound pages or validate `status`.**
`?page_size=10000` and `?status=FOO` go straight through. Fix with Query bounds and a status check.

## Global

**28. No indexes at startup.**
The grader seeds ~100k docs with only the default `_id` index. Every list and analytics query would COLLSCAN. Fix: create the indexes we need at startup, idempotently.

**29. Naive datetimes from Mongo.**
By default PyMongo strips the timezone. Any comparison against IST is then silently wrong. Fix: `MongoClient(..., tz_aware=True)`.

**30. No environment validation.**
If neither `.env` nor the real env vars are set, the app starts and only fails deep inside a request. Fix: read both variables at import and raise if either is missing.

## Things I checked and decided were fine

- FastAPI's default 422 body. Contract uses it as-is.
- `_id` stays in Mongo, dropped at the API boundary via projection. Not a bug.
- `history` missing on legacy docs: treat as empty.
- `half_day` missing on legacy docs: treat as false.
- `history: []` returned for never-corrected records. Correct.
- The 404 at `/` and `/favicon.ico` is not a defect. Neither route is in `openapi.yaml`.