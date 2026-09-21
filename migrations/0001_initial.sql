-- Catalogue ------------------------------------------------------------------

CREATE TABLE products (
    sku          text   PRIMARY KEY,
    name         text   NOT NULL,
    price_cents  bigint NOT NULL,
    CONSTRAINT products_sku_length         CHECK (char_length(sku) BETWEEN 1 AND 64),
    CONSTRAINT products_name_length        CHECK (char_length(name) BETWEEN 1 AND 200),
    CONSTRAINT products_price_non_negative CHECK (price_cents >= 0)
);

-- Orders ---------------------------------------------------------------------

CREATE TABLE orders (
    order_ref           text        PRIMARY KEY,
    customer_id         text        NOT NULL,
    status              text        NOT NULL DEFAULT 'accepted',
    total_cents         bigint      NOT NULL,
    accepted_at         timestamptz NOT NULL DEFAULT now(),
    stock_committed_at  timestamptz,
    CONSTRAINT orders_order_ref_length   CHECK (char_length(order_ref) BETWEEN 1 AND 64),
    CONSTRAINT orders_customer_id_length CHECK (char_length(customer_id) BETWEEN 1 AND 64),
    CONSTRAINT orders_status_known       CHECK (status IN ('accepted', 'stock_committed')),
    CONSTRAINT orders_total_non_negative CHECK (total_cents >= 0),
    CONSTRAINT orders_stock_committed_at_matches_status
        CHECK ((status = 'stock_committed') = (stock_committed_at IS NOT NULL))
);

CREATE TABLE order_items (
    order_ref         text    NOT NULL REFERENCES orders (order_ref),
    sku               text    NOT NULL REFERENCES products (sku),
    qty               integer NOT NULL,
    unit_price_cents  bigint  NOT NULL,
    line_total_cents  bigint  NOT NULL GENERATED ALWAYS AS (qty * unit_price_cents) STORED,
    PRIMARY KEY (order_ref, sku),
    CONSTRAINT order_items_qty_positive            CHECK (qty > 0),
    CONSTRAINT order_items_unit_price_non_negative CHECK (unit_price_cents >= 0)
);

CREATE TABLE order_events (
    event_id       bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_type     text        NOT NULL,
    event_version  smallint    NOT NULL,
    order_ref      text        NOT NULL REFERENCES orders (order_ref),
    occurred_at    timestamptz NOT NULL,
    payload        jsonb       NOT NULL,
    CONSTRAINT order_events_type_known       CHECK (event_type IN ('order.accepted')),
    CONSTRAINT order_events_version_positive CHECK (event_version >= 1),
    CONSTRAINT order_events_one_per_order_and_type UNIQUE (order_ref, event_type)
);

CREATE FUNCTION order_events_reject_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'order_events is append-only: % is not allowed', TG_OP;
END;
$$;

CREATE TRIGGER order_events_append_only
    BEFORE UPDATE OR DELETE ON order_events
    FOR EACH ROW EXECUTE FUNCTION order_events_reject_mutation();

-- Inventory ------------------------------------------------------------------

CREATE TABLE stock_levels (
    sku         text        PRIMARY KEY REFERENCES products (sku),
    on_hand     integer     NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE consumer_offsets (
    consumer       text        PRIMARY KEY,
    last_event_id  bigint      NOT NULL DEFAULT 0,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT consumer_offsets_last_event_id_non_negative CHECK (last_event_id >= 0)
);

INSERT INTO consumer_offsets (consumer) VALUES ('stock-applier');
