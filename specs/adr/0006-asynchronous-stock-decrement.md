# ADR-0006: Stock is decremented asynchronously and may go negative

- Status: Accepted
- Date: 2026-09-21
- Requirements: REQ-STK-02, REQ-STK-03, REQ-STK-04, REQ-OUT-01, REQ-OUT-02

## Context

Each accepted order must reduce stock.
Orders must still be accepted while stock processing is unavailable, and stock must catch up afterwards.
Nothing downstream of stock exists: no picking, shipping or fulfilment.
The question is when stock changes, whether it is reserved or decremented, and what happens when there is not enough.

## Decision drivers

- Intake must not depend on stock processing.
- An order acknowledged with `201` must stay a valid order.
- After an outage, stock must converge to the same value it would have reached without one.
- The model must be as simple as the requirements allow.

## Options considered

### Option 1: Check and decrement at acceptance; reject with `409` when insufficient

- Good, because stock never goes negative and customers learn about shortages immediately.
- Bad, because intake depends on stock processing, violating REQ-OUT-01.
- Bad, because hot SKUs become row-lock contention in the request path.

### Option 2: Reserve at acceptance, commit the reservation later

- Good, because it separates available stock from physical stock, as warehouse systems do.
- Bad, because creating the reservation is still a synchronous stock operation at intake.
- Bad, because reservations need expiry and release rules, and nothing in this system would ever consume them (NG-05).

### Option 3: Decrement asynchronously; cancel the order if stock is insufficient

- Good, because stock never goes negative.
- Bad, because an order already acknowledged with `201` is cancelled afterwards, breaking the promise to the customer.
- Bad, because it needs a new order state, a compensating event on the feed, and a third failure path.
- Bad, because which orders are cancelled depends on processing order, so catch-up after an outage could cancel different orders than real-time processing would have.

### Option 4: Decrement asynchronously; clamp at zero

- Good, because the number never looks negative.
- Bad, because it discards information: sold units vanish, and conservation (INV-STK-2) no longer holds.

### Option 5: Decrement asynchronously; allow negative `on_hand` as a backorder (chosen)

- Good, because intake is never blocked by stock.
- Good, because decrements commute, so the result after catch-up is independent of timing and batching.
- Good, because conservation stays exact and auditable.
- Bad, because the system can oversell, and handling a backorder, by restocking or contacting the customer, is left to the business.
- Bad, because stock reads are eventually consistent.

## Decision

Option 5.
Stock is decremented, not reserved, by the stock applier in the same transaction that moves the order to `stock_committed` and advances the applier's offset.
`on_hand` has no lower bound; negative values are backorders, logged at warning level and visible through `GET /stock/{sku}`.
`as_of_event_id` makes the eventual consistency explicit to readers.

## Consequences

- AC-STK-03 shows an oversell producing negative stock with the order still `stock_committed`.
- AC-OUT-03 shows that an outage in the middle does not change the final stock.
- Downstream teams can read backorders directly from stock levels.
- Revisit when the business must refuse orders it cannot fulfil: add a best-effort availability pre-check at intake that reads `on_hand` without locking, and keep the asynchronous decrement as the authority.
