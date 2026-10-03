# Incident: DatasourceError

- Status: firing
- Started: 2026-10-03T14:16:00Z
- Endpoint: [no value]
- Summary: 5xx responses on [no value]
- Description: {{ $labels.http_route }} returned {{ humanize $values.B.Value }} 5xx response(s) in the last 5 minutes (window: 5m).
- Dashboard: http://localhost:3000/d/order-tracker?from=1791033360000&orgId=1&to=1791037070199
- Labels: `{"alertname": "DatasourceError", "datasource_uid": "prometheus", "grafana_folder": "Order Tracker", "ref_id": "A", "rulename": "Order Tracker 5xx responses", "service": "order-tracker", "severity": "page"}`

## Metrics (last 15m)


## Error logs


## Traces

