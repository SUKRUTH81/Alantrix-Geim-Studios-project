import sqlite3
import threading
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.db import get_connection, init_db


@pytest.fixture(autouse=True)
def use_test_db(tmp_path, monkeypatch):
    db_path = tmp_path / "economy_test.db"
    monkeypatch.setenv("ECONOMY_DB_PATH", str(db_path))
    from importlib import reload

    reload(__import__("app.db", fromlist=["*"]))
    init_db()
    yield


@pytest.fixture
def client():
    return TestClient(app)


def test_credit_idempotent(client):
    player_id = "player-1"
    key = str(uuid.uuid4())
    response1 = client.post(
        f"/v1/wallets/{player_id}/credit",
        json={"amount": 100, "reason": "battle payout"},
        headers={"Idempotency-Key": key},
    )
    assert response1.status_code == 200
    assert response1.json()["balance"] == 100

    response2 = client.post(
        f"/v1/wallets/{player_id}/credit",
        json={"amount": 100, "reason": "battle payout"},
        headers={"Idempotency-Key": key},
    )
    assert response2.status_code == 200
    assert response2.json() == response1.json()

    wallet = client.get(f"/v1/wallets/{player_id}")
    assert wallet.json()["balance"] == 100


def test_purchase_concurrent(client):
    player_id = "player-race"
    client.post(
        f"/v1/wallets/{player_id}/credit",
        json={"amount": 100, "reason": "battle payout"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )

    results = []
    lock = threading.Lock()

    def attempt_purchase(key_suffix):
        response = client.post(
            f"/v1/wallets/{player_id}/purchase",
            json={"itemId": f"sword-{key_suffix}", "price": 100},
            headers={"Idempotency-Key": f"purchase-{key_suffix}"},
        )
        with lock:
            results.append((response.status_code, response.json()))

    threads = [threading.Thread(target=attempt_purchase, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    successes = [r for r in results if r[0] == 200]
    failures = [r for r in results if r[0] == 409]
    assert len(successes) == 1
    assert len(failures) == 3

    wallet = client.get(f"/v1/wallets/{player_id}")
    assert wallet.json()["balance"] == 0
    assert len(wallet.json()["inventory"]) == 1


def test_persistence_after_restart(tmp_path, monkeypatch):
    db_path = tmp_path / "economy_persist.db"
    monkeypatch.setenv("ECONOMY_DB_PATH", str(db_path))
    from importlib import reload

    reload(__import__("app.db", fromlist=["*"]))
    init_db()

    conn = get_connection()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO players (player_id, balance) VALUES (?, ?)", ("player-persist", 500))
    conn.execute("COMMIT")
    conn.close()

    client = TestClient(app)
    response = client.post(
        "/v1/wallets/player-persist/credit",
        json={"amount": 50, "reason": "battle payout"},
        headers={"Idempotency-Key": "persist-key"},
    )
    assert response.status_code == 200
    assert response.json()["balance"] == 550

    client = TestClient(app)
    wallet = client.get("/v1/wallets/player-persist")
    assert wallet.status_code == 200
    assert wallet.json()["balance"] == 550
