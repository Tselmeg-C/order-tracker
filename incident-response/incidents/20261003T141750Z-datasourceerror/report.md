# Incident report: DatasourceError (Order Tracker 5xx responses)

Started: 2026-10-03T14:16:00Z · Responder: on-call (Claude)

## Summary

There were two separate problems:

1. **The alert itself (monitoring, NOT resolved):** Grafana could not evaluate the
   "Order Tracker 5xx responses" rule:
   `failed to execute query [A]: Post "http://prometheus:9090/api/v1/query": dial tcp 10.215.24.3:9090: i/o timeout`.
   Prometheus also cannot scrape the OTel collector: target `otel-collector:8889` is
   `health: down`, `lastError: context deadline exceeded`, and `up == 0`. Because of this the
   evidence bundle was empty (no metrics, logs, or traces) and the 5xx alert cannot fire. This
   is a container network/infra problem, not an application code problem.
2. **The 5xx the alert was hiding (application, FIXED):** the app logs showed repeated
   `GET /api/orders/express-1002 → 500` with `ValueError: day is out of range for month`.

## Root cause (application)

`app/main.py` `order_detail()` computed the express delivery estimate as
`placed_at.replace(day=placed_at.day + 2)`. That builds an invalid date for express
orders placed in the last two days of a month. The seeded order `express-1002` was created
on 2026-09-30, so the code asked for 2026-09-32 and raised `ValueError`. Every lookup of that
order returned 500.

```
File "/app/app/main.py", line 60, in order_detail
    estimated_at = placed_at.replace(day=placed_at.day + 2)
ValueError: day 32 must be in range 1..30 for month 9 in year 2026
```

## Fix

`app/main.py:60`: use date arithmetic, which handles month and year rollover:

```python
estimated_at = placed_at + timedelta(days=2)
```

No data migration is needed. Stored `created_at` values are unchanged, and only the derived
field is computed differently.

## Test

`tests/test_api.py::test_express_estimate_crosses_month_end` is parametrized over
Sep 30 → Oct 2, Feb 27 → Mar 1, and Dec 31 → Jan 2. Each case inserts an express order
and checks that `GET /api/orders/{id}` returns 200 with the right `estimated_delivery`.

- Before fix: all 3 cases failed with `ValueError: day 32/29/33 must be in range ...`.
- After fix: `uv run --frozen pytest -q` → `7 passed, 3 warnings in 0.43s`.

## Verification

`docker compose up --build -d --wait app` → `Container order-tracker-app-1 Healthy`

```
$ curl -s -w '\nHTTP %{http_code}\n' http://localhost:8000/api/orders/express-1002
{"id":"express-1002","customer":"Sam","item":"Headphones","priority":"express","status":"preparing","created_at":"2026-09-30T14:15:45.498438+00:00","estimated_delivery":"2026-10-02"}
HTTP 200
$ curl -s -o /dev/null -w 'healthz HTTP %{http_code}\n' http://localhost:8000/healthz
healthz HTTP 200
```

## Still open: needs escalation (monitoring pipeline)

After the app fix, Prometheus still reports:

```
scrapeUrl: http://otel-collector:8889/metrics  health: down
lastError: Get "http://otel-collector:8889/metrics": context deadline exceeded
up{job="otel-collector"} = 0
```

Grafana → Prometheus and Prometheus → otel-collector both time out on the compose network, even
though every container reports Up/Healthy. No 5xx metrics reach Prometheus, so the 5xx alert
stays blind and the DatasourceError alert will keep firing. Fixing this needs infra
investigation (docker network, collector Prometheus exporter on :8889, possible firewall or
resource starvation). I did not change it, because no code change in this repo is clearly the
safe fix.
