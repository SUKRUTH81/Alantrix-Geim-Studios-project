# Resilience

## Scenario: external inventory service out of transaction

If item grant moves to a separate inventory service that can time out, fail, or process requests twice, I would use an outbox/saga pattern.

### Approach

1. `purchase` starts a local database transaction and records the intent in a local `ledger_entries` table.
2. It debits the wallet and marks the purchase as pending in an outbox entry.
3. The local transaction commits, making the debit durable.
4. An external worker reads pending outbox entries and calls the inventory service.
5. The outbox entry is updated to success or failure once the external grant is acknowledged.

If the inventory service retries the same request, the local outbox ensures the purchase is idempotent by recording a unique request correlation key.

If the external grant fails permanently, a compensation process can refund the currency or mark the purchase as failed, depending on desired business semantics.

### Partial-failure window

The partial-failure window is the time between local commit and external confirmation. During that window, the debit is durable but the inventory grant is not yet confirmed. The service keeps an audit trail so the purchase can be retried safely.

## Detecting and correcting a double-grant bug

A bug that double-granted currency means the invariant `balance == initial_balance + credits - purchases` was broken.

### Detection

- A ledger audit that compares user balances against the sum of all credit and debit entries would catch inconsistencies.
- The `ledger_entries` table acts as an audit trail; queries can reveal duplicate request keys or repeated credit events for the same key.

### Correction

- Identify affected players by comparing the ledger to actual balances.
- Reconcile by applying corrective debit entries or manual balance updates with an audit record.
- Use a replay-safe fix: reject duplicate idempotency keys and preserve the first accepted event.

An invariant that would have caught it sooner is: "Every successful mutating response must be matched by exactly one ledger entry and one idempotency record." A daily reconciliation job can enforce that invariant.
