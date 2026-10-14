# DECISIONS.md

## 1. Indexes

Four indexes, each with a specific job.

`employees.emp_code` unique. That is what makes the duplicate-create rule work, and it turns every employee lookup into a single seek.

`attendance_logs (emp_code, date)` unique. Same job for punch-in, and it serves punch-out, PATCH, and the monthly per-employee aggregation.

`attendance_logs (date desc, emp_code asc)` matches the list endpoint sort exactly. Without it, pagination sorts the whole match set on every request.

`employees.department` covers the department filter and the outer match in the department summary pipeline.

I looked at a single-field index on `status` and rejected it. Five values, poor selectivity, never queried alone. Same story for `punch_in` on its own. I added a compound `(emp_code, punch_out, punch_in desc)` for punch-out because otherwise the "most recent open record" lookup falls back to a sort in memory.

## 2. Punch-in race

Two identical punch-ins arrive together. Both handlers read the same employee, both compute the same attendance date, and both call `insert_one` with the same `(emp_code, date)`. There is no guard in front. I removed the find_one check that the starter used, because it does not protect anything under load.

MongoDB's unique index lets exactly one insert commit. The other raises `DuplicateKeyError`, which my handler catches and converts to 409. The winner returns 201. This holds under client retries too, because a second insert for the same key can never beat the unique constraint.

## 3. Ties

The pipeline groups by `emp_code`, sums late minutes, then runs `$setWindowFields` with `$rank` ordered by the total descending. `$rank` implements competition ranking, so 1, 2, 2, 4 comes straight out of the database. No post-processing.

After that I filter `{ rank: { $lte: limit } }`. The filter is on the rank value, not on the row position. If three employees tie at rank 2 and `limit=2`, the response has four rows: the rank-1 employee plus all three at rank 2. Rows come back sorted by `total_late_minutes` desc, then `emp_code` asc, so the order is deterministic.

## 4. Headcount

The department summary pipeline starts on `employees`, not on `attendance_logs`. It matches `joined_on <= last day of month`, groups by department, and only then `$lookup`s into `attendance_logs` for that department's records in the month.

Because the employee side is the outer side of the join, an employee with zero logs still contributes 1 to `headcount`. They contribute 0 to `present_days` and `late_count`, and they are excluded from the work-hours average, which is over records that actually carry a `work_hours` value. A department whose headcount is 0 after the join is dropped by a final `$match`.

## 5. If this had to serve 100x the data

Analytics would stop running against raw logs. A nightly job would materialise per-employee-per-month summaries into their own collection, and the read endpoints would hit that.

Beyond that: shard `attendance_logs` on hashed `emp_code` so writes and reads spread across shards, add a covering index on `(date, status, late_minutes)` for the leaderboard and trend, and cache the department summary in Redis with a short TTL invalidated by punch and PATCH. Punch-in and punch-out would go through a queue so bursts don't contend on the unique index. Older logs move to cold storage past a retention window.  