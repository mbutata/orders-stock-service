# ADR-0005: `order_ref` is the idempotency key, enforced by a unique index

- Status: Accepted
- Date: 2026-09-21
- Requirements: REQ-DUP-01, REQ-DUP-02, REQ-DUP-03, REQ-DUP-04

## Context

Clients submit the same order more than once: retries after timeouts, double clicks, senders with at-least-once semantics.
Only one submission may count.
Submissions of the same `order_ref` can arrive concurrently, on different connections and, with several `api` replicas, in different processes.
Every order already carries a client-chosen `order_ref`.

## Decision drivers

- Exactly one order per `order_ref` under any interleaving.
- A retry must be safe and must look like success to the client.
- A different order sent under a used reference must not be silently merged or dropped.
- No more state and no more concepts than necessary.

## Options considered

### Key: `order_ref` as the primary key with `INSERT ... ON CONFLICT DO NOTHING` (chosen)

- Good, because the unique index decides atomically across transactions and processes.
- Good, because the order row is the idempotency record, so the guarantee is permanent and needs no extra table.
- Good, because the client already has the key; no new header or concept is introduced.
- Bad, because `order_ref` must be unique across every channel, which is the client's responsibility, for example by prefixing a channel name.
- Bad, because a duplicate must load the stored order to compare content.

### Key: an `Idempotency-Key` header with a stored-response table

The approach of the IETF `Idempotency-Key` draft and of several payment APIs.

- Good, because it generalizes to any endpoint.
- Bad, because it introduces a second key next to `order_ref`, and the two can disagree.
- Bad, because it needs an expiry policy and stored responses, which is new state and new failure modes.

### Key: check with `SELECT`, then `INSERT`

- Good, because it is obvious.
- Bad, because it is a check-then-act race: two transactions can both see no order.

### Key: `SERIALIZABLE` transactions with retries

- Good, because it is correct.
- Bad, because conflicts surface as errors that every caller must retry, and the unique index is needed anyway.

### Key: a per-reference advisory lock

- Good, because it serializes submissions of one reference.
- Bad, because it duplicates what the index guarantees, and hash collisions serialize unrelated orders.

### Response to an identical duplicate

- **`200 OK` with the existing order in its current state (chosen).**
  The retry succeeds, and the status code tells the client that this request did not create the order.
- **`409 Conflict` for every duplicate.**
  Rejected: it turns a successful retry into an error each client must special-case, defeating idempotency.
- **Replay the original `201` response verbatim.**
  Rejected: it requires storing responses, hides whether this request created the order, and returns a stale status.

### Response to a duplicate with different content

- **`409 Conflict` with `order_ref_conflict` (chosen).**
  `order_ref` is the resource's identity, so a different order under it conflicts with the current state of that resource.
- **`422 Unprocessable Content`, as the IETF draft suggests for a reused idempotency key.**
  Rejected: that advice targets an opaque key separate from the resource; here the key is the resource.
- **Accept it as a new version of the order.**
  Rejected: amendments are a non-goal (NG-02), and silently changing an accepted order would corrupt stock.

## Decision

`order_ref` is the primary key of `orders`.
Acceptance inserts with `ON CONFLICT (order_ref) DO NOTHING` under `READ COMMITTED`; if no row is inserted, it reads the existing order and compares `customer_id` and the set of `(sku, qty)` pairs.
Identical content gives `200`, different content `409`.
An existing reference is resolved before catalogue validation, so a retry is answered from the stored order alone.
The full algorithm and race argument are in [05-reliability.md](../05-reliability.md#duplicate-submissions).

## Consequences

- Duplicates cannot create orders, events or stock effects, whatever their timing (AC-DUP-01 to AC-DUP-08).
- Clients can retry blindly after any failure.
- A reference can never be reused, which is intended; there is no expiry.
- Revisit when an endpoint without a natural key needs idempotency: that endpoint gets an `Idempotency-Key` header; orders keep `order_ref`.
