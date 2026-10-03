import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "orders.db")
    with TestClient(main.app) as test_client:
        yield test_client


def test_health_and_seeded_orders(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    orders = client.get("/api/orders").json()
    assert len(orders) == 3
    assert {order["priority"] for order in orders} == {"standard", "express"}


def test_create_and_update_order(client):
    response = client.post(
        "/api/orders",
        json={"customer": "Taylor", "item": "Mug", "priority": "standard"},
    )
    assert response.status_code == 201
    order_id = response.json()["id"]
    assert client.get(f"/api/orders/{order_id}").json()["status"] == "received"
    updated = client.patch(f"/api/orders/{order_id}", json={"status": "shipped"})
    assert updated.status_code == 200
    assert updated.json()["status"] == "shipped"


def test_missing_order(client):
    assert client.get("/api/orders/missing").status_code == 404


@pytest.mark.parametrize(
    ("created_at", "expected"),
    [
        ("2026-09-30T10:00:00+00:00", "2026-10-02"),
        ("2026-02-27T10:00:00+00:00", "2026-03-01"),
        ("2026-12-31T10:00:00+00:00", "2027-01-02"),
    ],
)
def test_express_estimate_crosses_month_end(client, created_at, expected):
    with main.connect() as db:
        db.execute(
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
            ("express-month-end", "Jo", "Lamp", "express", "preparing", created_at),
        )
    response = client.get("/api/orders/express-month-end")
    assert response.status_code == 200
    assert response.json()["estimated_delivery"] == expected


def test_request_metric_uses_route_and_status(client, monkeypatch):
    from app import telemetry

    recorded = []
    monkeypatch.setattr(telemetry.request_counter, "add", lambda value, attrs: recorded.append(attrs))
    client.get("/api/orders/standard-1001")
    client.get("/api/orders/missing")
    assert recorded == [
        {"http.request.method": "GET", "http.route": "/api/orders/{order_id}", "http.response.status_code": 200},
        {"http.request.method": "GET", "http.route": "/api/orders/{order_id}", "http.response.status_code": 404},
    ]
