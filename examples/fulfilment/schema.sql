CREATE TABLE wave (name text PRIMARY KEY);
CREATE TABLE stock (
    sku text PRIMARY KEY, initial integer NOT NULL,
    available integer NOT NULL CHECK (available >= 0),
    reserved integer NOT NULL CHECK (reserved >= 0),
    dispatched integer NOT NULL CHECK (dispatched >= 0),
    CHECK (initial = available + reserved + dispatched)
);
CREATE TABLE orders (
    id text PRIMARY KEY, revision integer NOT NULL, state text NOT NULL
);
CREATE TABLE parcels (
    id text PRIMARY KEY, order_id text NOT NULL REFERENCES orders(id),
    sku text NOT NULL REFERENCES stock(sku), quantity integer NOT NULL CHECK (quantity > 0),
    state text NOT NULL
);
CREATE TABLE operations (key text PRIMARY KEY, payload jsonb NOT NULL, result jsonb NOT NULL);
CREATE TABLE movements (
    key text PRIMARY KEY REFERENCES operations(key),
    available integer NOT NULL, reserved integer NOT NULL, dispatched integer NOT NULL,
    CHECK (available + reserved + dispatched = 0)
);
CREATE TABLE carrier_receipts (key text PRIMARY KEY, payload jsonb NOT NULL, receipt jsonb NOT NULL);
CREATE TABLE attempts (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, job text NOT NULL);
INSERT INTO stock VALUES ('WIDGET', 3, 3, 0, 0);
INSERT INTO orders VALUES ('SPLIT', 1, 'accepted'), ('CANCEL', 1, 'accepted');
INSERT INTO parcels VALUES
    ('P1', 'SPLIT', 'WIDGET', 1, 'accepted'),
    ('P2', 'SPLIT', 'WIDGET', 1, 'accepted'),
    ('P3', 'CANCEL', 'WIDGET', 1, 'accepted');
