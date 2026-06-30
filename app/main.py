import json
import os
import sqlite3
import time
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, PositiveInt, constr

from . import db

app = FastAPI()


class CreditPayload(BaseModel):
    amount: PositiveInt
    reason: constr(min_length=1, max_length=256)


class PurchasePayload(BaseModel):
    itemId: constr(min_length=1, max_length=128)
    price: PositiveInt


class ClaimPayload(BaseModel):
    playerId: constr(min_length=1, max_length=128)


class WalletResponse(BaseModel):
    balance: int
    inventory: List[str]
    claimedRewards: List[str]


def get_db():
    conn = db.get_connection()
    try:
        yield conn
    finally:
        conn.close()


@app.on_event("startup")
def startup_event() -> None:
    db.init_db()


def parse_idempotency_key(idempotency_key: Optional[str]) -> str:
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing Idempotency-Key header on mutating request.",
        )
    if len(idempotency_key.strip()) < 8 or len(idempotency_key) > 128:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key must be between 8 and 128 characters.",
        )
    return idempotency_key.strip()


def load_idempotency_record(conn: sqlite3.Connection, key: str, request_hash: str, method: str, path: str):
    row = conn.execute(
        "SELECT method, path, request_hash, status_code, response_body FROM idempotency_keys WHERE key = ?",
        (key,),
    ).fetchone()
    if not row:
        return None
    if row["method"] != method or row["path"] != path:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key reused with different endpoint.",
        )
    if row["request_hash"] != request_hash:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key reused with different payload.",
        )
    return {
        "status_code": row["status_code"],
        "response_body": json.loads(row["response_body"]),
    }


def save_idempotent_response(
    conn: sqlite3.Connection,
    key: str,
    method: str,
    path: str,
    request_hash: str,
    status_code: int,
    response_body: dict,
):
    conn.execute(
        "INSERT OR REPLACE INTO idempotency_keys (key, method, path, request_hash, status_code, response_body) VALUES (?, ?, ?, ?, ?, ?)",
        (key, method, path, request_hash, status_code, json.dumps(response_body, separators=(",", ":"))),
    )


def ensure_player_exists(conn: sqlite3.Connection, player_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO players (player_id, balance) VALUES (?, 0)",
        (player_id,),
    )


def add_ledger_entry(
    conn: sqlite3.Connection,
    request_key: str,
    player_id: Optional[str],
    event_type: str,
    amount: Optional[int] = None,
    item_id: Optional[str] = None,
    reward_id: Optional[str] = None,
):
    conn.execute(
        "INSERT INTO ledger_entries (request_key, player_id, event_type, amount, item_id, reward_id) VALUES (?, ?, ?, ?, ?, ?)",
        (request_key, player_id, event_type, amount, item_id, reward_id),
    )


@app.post("/v1/wallets/{playerId}/credit")
def credit_wallet(
    playerId: str,
    payload: CreditPayload,
    request: Request,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    conn: sqlite3.Connection = Depends(get_db),
):
    key = parse_idempotency_key(idempotency_key)
    method = "POST"
    path = request.url.path
    request_hash = db.compute_request_hash(method, path, payload.model_dump())

    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = load_idempotency_record(conn, key, request_hash, method, path)
        if existing:
            conn.execute("COMMIT")
            return JSONResponse(status_code=existing["status_code"], content=existing["response_body"])

        ensure_player_exists(conn, playerId)
        conn.execute(
            "UPDATE players SET balance = balance + ? WHERE player_id = ?",
            (payload.amount, playerId),
        )
        add_ledger_entry(conn, key, playerId, "credit", amount=payload.amount)
        response = {"balance": conn.execute("SELECT balance FROM players WHERE player_id = ?", (playerId,)).fetchone()[0]}
        save_idempotent_response(conn, key, method, path, request_hash, status.HTTP_200_OK, response)
        conn.execute("COMMIT")
        return response
    except sqlite3.OperationalError as exc:
        conn.execute("ROLLBACK")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database busy, retry later.") from exc
    except Exception:
        conn.execute("ROLLBACK")
        raise


@app.post("/v1/wallets/{playerId}/purchase")
def purchase_item(
    playerId: str,
    payload: PurchasePayload,
    request: Request,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    conn: sqlite3.Connection = Depends(get_db),
):
    key = parse_idempotency_key(idempotency_key)
    method = "POST"
    path = request.url.path
    request_hash = db.compute_request_hash(method, path, payload.model_dump())

    test_pause = request.headers.get("X-Test-Pause-Before-Commit")
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = load_idempotency_record(conn, key, request_hash, method, path)
        if existing:
            conn.execute("COMMIT")
            return JSONResponse(status_code=existing["status_code"], content=existing["response_body"])

        ensure_player_exists(conn, playerId)
        balance_row = conn.execute(
            "SELECT balance FROM players WHERE player_id = ?",
            (playerId,),
        ).fetchone()
        if balance_row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Player not found.")
        balance = balance_row[0]
        if balance < payload.price:
            response = {"error": "insufficient_funds", "balance": balance}
            save_idempotent_response(conn, key, method, path, request_hash, status.HTTP_409_CONFLICT, response)
            conn.execute("COMMIT")
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=response)

        conn.execute(
            "UPDATE players SET balance = balance - ? WHERE player_id = ?",
            (payload.price, playerId),
        )
        conn.execute(
            "INSERT INTO inventory_items (player_id, item_id) VALUES (?, ?)",
            (playerId, payload.itemId),
        )
        add_ledger_entry(conn, key, playerId, "purchase", amount=-payload.price, item_id=payload.itemId)

        if test_pause == "1":
            time.sleep(0.5)

        balance = conn.execute("SELECT balance FROM players WHERE player_id = ?", (playerId,)).fetchone()[0]
        response = {"balance": balance, "itemId": payload.itemId}
        save_idempotent_response(conn, key, method, path, request_hash, status.HTTP_200_OK, response)
        conn.execute("COMMIT")
        return response
    except sqlite3.OperationalError as exc:
        conn.execute("ROLLBACK")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database busy, retry later.") from exc
    except HTTPException:
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise


@app.post("/v1/rewards/{rewardId}/claim")
def claim_reward(
    rewardId: str,
    payload: ClaimPayload,
    request: Request,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    conn: sqlite3.Connection = Depends(get_db),
):
    key = parse_idempotency_key(idempotency_key)
    method = "POST"
    path = request.url.path
    request_hash = db.compute_request_hash(method, path, payload.model_dump())

    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = load_idempotency_record(conn, key, request_hash, method, path)
        if existing:
            conn.execute("COMMIT")
            return JSONResponse(status_code=existing["status_code"], content=existing["response_body"])

        ensure_player_exists(conn, payload.playerId)
        already = conn.execute(
            "SELECT 1 FROM claimed_rewards WHERE player_id = ? AND reward_id = ?",
            (payload.playerId, rewardId),
        ).fetchone()
        if already:
            response = {"error": "already_claimed", "rewardId": rewardId}
            save_idempotent_response(conn, key, method, path, request_hash, status.HTTP_409_CONFLICT, response)
            conn.execute("COMMIT")
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=response)

        conn.execute(
            "INSERT INTO claimed_rewards (player_id, reward_id) VALUES (?, ?)",
            (payload.playerId, rewardId),
        )
        add_ledger_entry(conn, key, payload.playerId, "reward_claim", reward_id=rewardId)
        response = {"rewardId": rewardId}
        save_idempotent_response(conn, key, method, path, request_hash, status.HTTP_200_OK, response)
        conn.execute("COMMIT")
        return response
    except sqlite3.OperationalError as exc:
        conn.execute("ROLLBACK")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database busy, retry later.") from exc
    except HTTPException:
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise


@app.get("/v1/wallets/{playerId}", response_model=WalletResponse)
def get_wallet(playerId: str, conn: sqlite3.Connection = Depends(get_db)):
    ensure_player_exists(conn, playerId)
    balance = conn.execute(
        "SELECT balance FROM players WHERE player_id = ?",
        (playerId,),
    ).fetchone()[0]
    inventory = [row["item_id"] for row in conn.execute(
        "SELECT item_id FROM inventory_items WHERE player_id = ? ORDER BY id",
        (playerId,),
    ).fetchall()]
    claimed = [row["reward_id"] for row in conn.execute(
        "SELECT reward_id FROM claimed_rewards WHERE player_id = ? ORDER BY claimed_at",
        (playerId,),
    ).fetchall()]
    return {"balance": balance, "inventory": inventory, "claimedRewards": claimed}


@app.exception_handler(HTTPException)
def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
