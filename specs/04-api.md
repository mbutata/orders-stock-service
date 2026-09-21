# 04 - API contract

[openapi.yaml](openapi.yaml) is the normative HTTP contract: paths, parameters, schemas, status codes and examples.
This document states the semantics a schema cannot express: processing order, idempotency, consistency, the feed consumer contract, and the error model.
Where the two disagree, that is a defect in the specification, to be fixed before implementation proceeds.

## Conventions

- Requests and successful responses are `application/json` in UTF-8.
  Error responses are `application/problem+json`.
- Request bodies are strict: unknown members are rejected with `422 validation_error`, so a misspelt field such as `quantity` fails loudly instead of being ignored.
- Response bodies are open for extension: clients must ignore members they do not recognise.
  New optional members may be added without a version change.
- Identifiers, money and timestamps follow [02-domain-model.md](02-domain-model.md#conventions): identifiers match `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`, amounts are integer minor units in `*_cents` fields, and timestamps are RFC 3339 UTC strings.
- There is no authentication (NG-01).
- The base URL in local development is `http://127.0.0.1:8000`.

## Endpoints

| Method | Path | Purpose | Success | Errors |
| --- | --- | --- | --- | --- |
| `POST` | `/orders` | Accept an order, idempotently by `order_ref` | `201`, or `200` for an identical duplicate | `409`, `422`, `500`, `503` |
| `GET` | `/orders/{order_ref}` | Order details and status | `200` | `404`, `500`, `503` |
| `GET` | `/stock/{sku}` | Current stock level of a SKU | `200` | `404`, `500`, `503` |
| `GET` | `/order-events` | Page of the order-accepted event log | `200` | `422`, `500`, `503` |

Only the feed is paginated.
There is no order listing (NG-07), so no other endpoint returns a collection.

## `POST /orders`

### Request

| Field | Type | Required | Constraints |
| --- | --- | --- | --- |
| `order_ref` | string | yes | Identifier pattern, 1 to 64 characters. The order's identity and idempotency key. |
| `customer_id` | string | yes | Identifier pattern, 1 to 64 characters. |
| `items` | array | yes | 1 to 100 entries, at most one entry per `sku`. |
| `items[].sku` | string | yes | Identifier pattern, 1 to 64 characters. |
| `items[].qty` | integer | yes | 1 to 10000. |

With at most 100 items of at most 10000 units, an order total stays below 2^53, the largest integer JSON numbers represent exactly, for any unit price under 9 billion cents.

### Processing order

The service evaluates a request in this order and stops at the first step that produces a response.

1. **Schema.**
   The body must match `OrderCreate`, and no `sku` may repeat.
   Otherwise `422 validation_error`.
2. **Existing order.**
   If an order with this `order_ref` exists, compare content: identical gives `200` with the existing order, different gives `409 order_ref_conflict`.
3. **Catalogue.**
   Every `sku` must exist in the catalogue.
   Otherwise `422 unknown_sku`.
4. **Create.**
   Insert the order, its items and its `order.accepted` event in one transaction, then respond `201`.
   If a concurrent request with the same `order_ref` committed first, the insert does nothing and the outcome is decided as in step 2.

Step 2 precedes step 3 so that a retry is answered from the stored order alone (REQ-DUP-04).
The algorithm and its concurrency argument are in [05-reliability.md](05-reliability.md#duplicate-submissions).

### Responses

- `201 Created`: the order was accepted.
  The body is the `Order`, with `status: accepted` and `stock_committed_at: null`.
  `Location` is `/orders/{order_ref}`.
- `200 OK`: identical duplicate.
  The body is the existing `Order` in its current state, which may already be `stock_committed`.
  Nothing was created or changed.
- `409 Conflict`: `order_ref_conflict`.
  The stored order is unchanged.
- `422 Unprocessable Content`: `validation_error` or `unknown_sku`, with an `errors` entry per offending location.
  Nothing was stored.
- `503 Service Unavailable`: `service_unavailable` with `Retry-After`.
  The request may or may not have been committed; retry it.

### Idempotency: what a client can rely on

- **Retrying is always safe.**
  After a timeout, a dropped connection, a `500` or a `503`, resend the identical request.
  If the first attempt committed, the retry gets `200` with that order; if it did not, the retry creates it and gets `201`.
  No outcome counts the order twice.
- **Same content means** equal `customer_id` and the same set of `(sku, qty)` pairs.
  Item order is irrelevant.
- **201 versus 200** tells the client whether this request created the order.
  Both carry the full order.
- **A resubmission is never repriced.**
  It returns the prices and total fixed at first acceptance, even if catalogue prices have changed since.
- **Concurrent submissions are safe.**
  Exactly one receives `201`; each other identical submission receives `200` once the winner has committed, and never an error caused by the race.
- **An `order_ref` is permanent.**
  It is never released or reused.
  A client that sends a different order under an existing `order_ref` receives `409`; generating unique references, for example a channel prefix plus a sequence number as in `web-100045`, is the client's responsibility.
- **There is no expiry.**
  Unlike header-based idempotency keys, the guarantee does not lapse after a time window, because the key is the order's own identity.

### Example

```http
POST /orders HTTP/1.1
Content-Type: application/json

{"order_ref": "web-100045", "customer_id": "cust-42",
 "items": [{"sku": "BAN-001", "qty": 2}, {"sku": "APL-003", "qty": 1}]}
```

```http
HTTP/1.1 201 Created
Location: /orders/web-100045
Content-Type: application/json

{"order_ref": "web-100045", "customer_id": "cust-42", "status": "accepted",
 "items": [{"sku": "APL-003", "qty": 1, "unit_price_cents": 349, "line_total_cents": 349},
           {"sku": "BAN-001", "qty": 2, "unit_price_cents": 199, "line_total_cents": 398}],
 "total_cents": 747, "accepted_at": "2026-09-21T10:15:03.412907Z", "stock_committed_at": null}
```

Sending the same request again returns `200 OK` with the same body, except that `status` and `stock_committed_at` show the order's current state.

## `GET /orders/{order_ref}`

Returns the `Order`: its items in ascending SKU order, prices as fixed at acceptance, the total, and the current status.
`status` is `accepted` until the stock applier has applied the order, then `stock_committed`.
Reads go to the same PostgreSQL primary that accepted the order, so a client that received `201` or `200` can read the order immediately.
An unknown `order_ref` gives `404 order_not_found`.

## `GET /stock/{sku}`

Returns `sku`, `on_hand` and `as_of_event_id`.
`on_hand` is eventually consistent with intake: it includes the effect of exactly those `order.accepted` events whose `event_id` is at most `as_of_event_id`, the stock applier's offset.
The two values are read in one SQL statement, so they always describe the same moment.
While the stock applier is down, `on_hand` does not move and `as_of_event_id` stays behind the head of the log; this is how a client sees that stock is stale rather than wrong.
`on_hand` may be negative, meaning units sold but not available (backorder).
An unknown `sku` gives `404 sku_not_found`.

## `GET /order-events`

The integration surface for other systems: a pull feed over the append-only event log.
The design choice is recorded in [ADR-0007](adr/0007-integration-surface-pull-feed.md).

### Parameters and page

- `after` (default `0`): return events with `event_id > after`.
- `limit` (default `100`, 1 to 500): maximum events per page.
- `events`: ascending by `event_id`.
- `next_after`: the `event_id` of the last event in the page, or the request's `after` when the page is empty.
  Pass it as `after` in the next request.
- `has_more`: `true` if more events existed beyond this page at the time of the read.
  The server reads `limit + 1` rows to decide it.

The cursor is the integer `event_id` itself, not an opaque token, because consumers also use it as their deduplication key and as a lag measure.

### Event envelope

| Member | Meaning |
| --- | --- |
| `event_id` | Position in the log. Increasing in commit order, not contiguous. Unique; the deduplication key. |
| `event_type` | `order.accepted`, the only type. |
| `event_version` | Version of `data`'s schema for this type, currently `1`. |
| `occurred_at` | Equal to `data.accepted_at`. Informational only. |
| `data` | `OrderAcceptedData`: `order_ref`, `customer_id`, `items` with unit prices and line totals, `total_cents`, `accepted_at`. |

`data` is a snapshot of the order at acceptance and never changes.
It carries everything a consumer needs, so a consumer never has to call `GET /orders/{order_ref}` to process an event.
It deliberately has no status: the event records that the order was accepted, which stays true forever.

### Delivery and ordering guarantees

- **Ordering.**
  Events form one total order by `event_id`, which equals the commit order of the accepting transactions.
  Once an event is visible, no event with a lower `event_id` can appear later.
  The order is unrelated to when clients sent their requests.
- **Completeness.**
  Each accepted order appears exactly once in the log; duplicates and rejected requests never appear.
- **Delivery.**
  The feed is a replayable log; the delivery guarantee a consumer obtains depends on when it saves its cursor.
  Saving after processing gives **at-least-once** delivery, which is the supported mode.
  Saving before processing gives at-most-once and is not recommended.
  Exactly-once delivery is not offered (NG-10).
- **Retention.**
  Events are kept indefinitely (NG-09), so a consumer can always replay from `after=0`.

### Consumer obligations

1. Process a page, then persist the last processed `event_id` as the cursor.
   Never persist a cursor for an event that has not been processed.
2. Make processing idempotent by `event_id`: a consumer that crashes between processing an event and saving its cursor will receive that event again.
3. Do not assume `event_id` values are contiguous; gaps are normal.
4. Ignore unknown members in the envelope and in `data`.
5. Skip events whose `event_type` is unknown, and still advance the cursor past them; new event types are additive.
6. Stop and alert on an `event_version` higher than the one the consumer understands; do not skip it, because skipping would silently lose an order.
7. When `has_more` is `true`, request the next page immediately; otherwise wait before polling again.
   One second is a sensible interval.

`consume-feed` ([03-architecture.md](03-architecture.md#consume-feed)) is the reference implementation of these obligations.

## Errors

### Shape

Every error response, including framework-level `404` and `405` responses for unknown routes and methods, is an RFC 9457 problem document with media type `application/problem+json`:

| Member | Presence | Meaning |
| --- | --- | --- |
| `type` | always | URI of the problem type: this document's anchor for `code`, for example `https://github.com/mbutata/orders-stock-service/blob/main/specs/04-api.md#order_ref_conflict`. |
| `title` | always | Constant summary per `code`. |
| `status` | always | The HTTP status code, repeated. |
| `detail` | always | Explanation specific to this occurrence. |
| `code` | always | Stable machine-readable identifier; clients branch on this, not on `title` or `detail`. |
| `errors` | `validation_error` and `unknown_sku` only | One entry per offending location: `detail`, plus `pointer` (RFC 6901 JSON Pointer into the body, in URI fragment form such as `#/items/0/qty`) or `parameter` (a query or path parameter name). |

Validation errors from the web framework are translated into this shape; a body that is not valid JSON yields one entry with `pointer: "#"`.

### Codes

| Code | Status | Title | When |
| --- | --- | --- | --- |
| `validation_error` | 422 | Request validation failed | The body or a query parameter violates the schema, or an order repeats a SKU. |
| `unknown_sku` | 422 | Unknown SKU | A new order references a SKU absent from the catalogue. |
| `order_ref_conflict` | 409 | order_ref already used with different content | A resubmission's content differs from the stored order. |
| `order_not_found` | 404 | Order not found | `GET /orders/{order_ref}` for an unknown reference. |
| `sku_not_found` | 404 | SKU not found | `GET /stock/{sku}` for an unknown SKU. |
| `not_found` | 404 | Resource not found | No route matches the path. |
| `method_not_allowed` | 405 | Method not allowed | The path exists but not with this method. |
| `internal_error` | 500 | Internal server error | An unexpected failure. The detail never includes stack traces or SQL. |
| `service_unavailable` | 503 | Service unavailable | The database is unreachable, no connection was available within 5 seconds, or a statement exceeded its 5-second timeout. `Retry-After: 1`. |

### Problem types

Each heading below is the target of the corresponding problem `type` URI.

#### validation_error

The request does not conform to [openapi.yaml](openapi.yaml), or repeats a SKU within one order.
Nothing was stored.
Fix the request; retrying it unchanged fails the same way.

#### unknown_sku

A new order references at least one SKU that the catalogue does not contain.
`errors` has one entry per unknown item, with `pointer` set to `#/items/<index>/sku`.
Nothing was stored.

#### order_ref_conflict

An order with this `order_ref` exists, and the request's `customer_id` or items differ from it.
The stored order is unchanged.
Either the client reused a reference for a different order, or the client changed a request between retries; in both cases the reference cannot be reassigned.

#### order_not_found

No order has the requested `order_ref`.

#### sku_not_found

No stock level exists for the requested SKU.

#### not_found

No route matches the request path.

#### method_not_allowed

The route exists but does not support the request method.

#### internal_error

An unexpected server failure.
The request may be retried; `POST /orders` is idempotent, so a retry cannot double-count an order.

#### service_unavailable

PostgreSQL is unreachable, the connection pool had no connection within 5 seconds, or a statement exceeded its timeout.
Retry after `Retry-After` seconds.

## Versioning and compatibility

The contract is version 1 and the paths are unversioned.
Compatible changes, such as new optional response members, new endpoints, new problem codes and new event types, are made in place.
An incompatible change to a request or response would be published under new paths alongside the old ones; an incompatible change to event data increments `event_version`.

## Conformance

REQ-API-01 makes this contract executable: the test suite validates every response body it receives against the schema that [openapi.yaml](openapi.yaml) declares for that operation and status code (AC-API-01).
The running service serves the committed document at `/openapi.json`, and Swagger UI at `/docs` renders it; the framework's generated schema is not used, so there is exactly one contract.
