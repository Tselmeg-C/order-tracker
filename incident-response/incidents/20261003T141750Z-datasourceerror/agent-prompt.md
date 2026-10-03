You are the on-call engineer for Order Tracker (FastAPI app in app/, tests in tests/).
A Grafana alert fired. Evidence (alert, metrics, error logs with stack traces, traces) is in
incident-response/incidents/20261003T141750Z-datasourceerror/evidence.md and incident-response/incidents/20261003T141750Z-datasourceerror/evidence.json. You can query more telemetry with curl:
Prometheus http://localhost:9090, Loki http://localhost:3100, Tempo http://localhost:3200. The app runs at http://localhost:8000.

1. Read the evidence and find the root cause in the code.
2. Add a regression test in tests/ that reproduces the failure, and check it fails.
3. Make the smallest safe fix, then run `uv run --frozen pytest -q` until it passes.
4. Rebuild and restart the app with `docker compose up --build -d --wait app`.
5. Verify the failing request from the evidence now succeeds with curl.
6. Write incident-response/incidents/20261003T141750Z-datasourceerror/report.md with: root cause, fix, test, and verification output.

Do not commit or push. If you cannot fix it safely (the fix needs a data migration, a
product decision, or the tests keep failing), don't guess: write the reason in report.md
and end your answer with a line starting with "ESCALATE:". Otherwise end your answer
with a line starting with "RESOLVED:" and the root cause in one sentence.
