# Design

## Datastore choice

I chose SQLite with WAL mode and `PRAGMA synchronous=FULL` for durability. SQLite is lightweight, durable on disk, and supports transactional isolation for this service slice. For a small economy service, it provides authoritative persistence without needing an external database container.

## Exactly-once strategy

Mutating endpoints require an `Idempotency-Key` header. The service stores a record of each key in the `idempotency_keys` table alongside:

- endpoint method
- request path
- request body hash
- response status code
- serialized response body

On a duplicate request with the same key, endpoint, and payload, the service returns the previously stored response without applying any effect again. If the key is reused with a different endpoint or payload, the request is rejected.

Idempotency records are retained indefinitely in this implementation. This ensures exact replay semantics for all mutually identical retries.

## Atomicity and durability

All mutating operations run inside a SQLite transaction with `BEGIN IMMEDIATE`.

- `credit`: create player row, update balance, insert ledger entry, store idempotency record.
- `purchase`: create player row, verify balance, debit player, insert inventory, insert ledger entry, store idempotency record.
- `claim`: create player row, enforce single claim via unique key, insert claimed reward, store idempotency record.

Because all writes are in one transaction, a `kill -9` during execution leaves either a fully committed operation or no effect. SQLite WAL plus `synchronous=FULL` ensures committed state is durable to disk before returning success.

The `ledger_entries` table records each processed request for audit or debugging. It is not used to drive semantics, but it provides a durable transaction trail.

## Concurrency correctness

`BEGIN IMMEDIATE` locks the database for write access and prevents lost updates on a single wallet balance. Concurrent purchase attempts race on the same player row; only one can commit if the balance allows only one purchase. The second attempt will see the lower balance and reject with `409` or wait until the first transaction completes.

Idempotency ensures duplicate requests from the same client do not create double effects, even if the first request succeeded and the client retries.

## API contract

- `POST /v1/wallets/{playerId}/credit`
  - body: `{ "amount": int>0, "reason": string }`
  - success `200`: `{ "balance": int }`
  - requires `Idempotency-Key`

- `POST /v1/wallets/{playerId}/purchase`
  - body: `{ "itemId": string, "price": int>0 }`
  - success `200`: `{ "balance": int, "itemId": string }`
  - insufficient funds: `409` with `{ "detail": { "error": "insufficient_funds", "balance": int } }`
  - requires `Idempotency-Key`

- `POST /v1/rewards/{rewardId}/claim`
  - body: `{ "playerId": string }`
  - success `200`: `{ "rewardId": string }`
  - already claimed: `409` with `{ "detail": { "error": "already_claimed", "rewardId": string } }`
  - requires `Idempotency-Key`

- `GET /v1/wallets/{playerId}`
  - success `200`: `{ "balance": int, "inventory": [string], "claimedRewards": [string] }`

## Limits and validation

- `amount` and `price` must be positive integers.
- Strings are bounded in length to prevent oversized inputs.
- `Idempotency-Key` must be 8-128 characters.
- Malformed JSON returns a validation error and does not mutate state.
