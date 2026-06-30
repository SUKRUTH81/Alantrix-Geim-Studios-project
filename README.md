# Durable Game Economy Service

A small wallet service for a game economy. Supports crediting currency, purchasing items, claiming one-time rewards, and reading wallet state.

## Build & run

Install requirements:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Run locally:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Run in Docker:

```bash
docker build -t economy-service .
docker run --rm -p 8000:8000 economy-service
```

SQLite is used as the durable datastore, so no additional container is required.

## Examples

Credit a wallet:

```bash
curl -X POST http://localhost:8000/v1/wallets/player1/credit \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: key-123" \
  -d '{"amount": 100, "reason": "battle payout"}'
```

Purchase an item:

```bash
curl -X POST http://localhost:8000/v1/wallets/player1/purchase \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: key-456" \
  -d '{"itemId": "sword", "price": 50}'
```

Claim a reward:

```bash
curl -X POST http://localhost:8000/v1/rewards/reward-1/claim \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: key-789" \
  -d '{"playerId": "player1"}'
```

Read a wallet:

```bash
curl http://localhost:8000/v1/wallets/player1
```

## API contract

All mutating requests require `Idempotency-Key` header.

- POST `/v1/wallets/{playerId}/credit`
  - body: `{ "amount": int>0, "reason": string }`
  - response: `{ "balance": int }`
- POST `/v1/wallets/{playerId}/purchase`
  - body: `{ "itemId": string, "price": int>0 }`
  - response: `{ "balance": int, "itemId": string }`
  - 409 if insufficient funds: `{ "detail": { "error": "insufficient_funds", "balance": int } }`
- POST `/v1/rewards/{rewardId}/claim`
  - body: `{ "playerId": string }`
  - response: `{ "rewardId": string }`
  - 409 if already claimed: `{ "detail": { "error": "already_claimed", "rewardId": string } }`
- GET `/v1/wallets/{playerId}`
  - response: `{ "balance": int, "inventory": [string], "claimedRewards": [string] }`

## Tests

Run tests with:

```bash
pytest
```
