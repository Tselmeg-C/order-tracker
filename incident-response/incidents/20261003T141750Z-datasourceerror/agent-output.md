The app bug behind this alert is fixed and live, but the alert itself, `DatasourceError`, is a monitoring failure I couldn't fix.

**Why the alert fired:** Grafana timed out connecting to Prometheus, so it couldn't check the 5xx rule. That's also why the evidence files were empty. Prometheus is also still failing to collect metrics from the OpenTelemetry collector (`otel-collector:8889`, `context deadline exceeded`, `up == 0`). That's a container network or infrastructure problem, not something in `app/`. I left it alone because no change in this repo is clearly the safe fix.

**The hidden 5xx:** the app logs showed `GET /api/orders/express-1002` returning 500 with `ValueError: day is out of range for month`.
- **Root cause:** `app/main.py:60` worked out the express delivery estimate with `placed_at.replace(day=placed_at.day + 2)`. For an order placed on Sept 30 that asks for Sept 32, which fails.
- **Fix:** `placed_at + timedelta(days=2)`. No data migration needed.
- **Test:** `test_express_estimate_crosses_month_end` in `tests/test_api.py` covers Sep 30, Feb 27 and Dec 31. All three cases failed before the fix; after it, `uv run --frozen pytest -q` gives 7 passed.
- **Check:** after the rebuild, `curl http://localhost:8000/api/orders/express-1002` returns HTTP 200 with `"estimated_delivery":"2026-10-02"`.

Everything is in `incident-response/incidents/20261003T141750Z-datasourceerror/report.md`. Nothing was committed.

ESCALATE: The app 5xx is fixed (an express order placed at month end got an invalid delivery date from `replace(day=day+2)`), but the alert itself is not: Grafana can't reach Prometheus and Prometheus can't reach the OTel collector, so 5xx alerting stays blind until someone investigates the container networking.
