CREATE TABLE IF NOT EXISTS reservation (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES product(id),
    buyer_id INTEGER NOT NULL REFERENCES account(id),
    product_title TEXT NOT NULL,
    price_cents INTEGER NOT NULL CHECK (price_cents BETWEEN 100 AND 1000000),
    location TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'confirmed', 'canceled', 'rejected', 'completed')),
    created_at INTEGER NOT NULL,
    confirmed_at INTEGER,
    ended_at INTEGER,
    ended_by INTEGER REFERENCES account(id),
    reason TEXT NOT NULL DEFAULT '',
    CHECK (status <> 'pending' OR confirmed_at IS NULL),
    CHECK (status NOT IN ('confirmed', 'completed') OR confirmed_at IS NOT NULL),
    CHECK ((status IN ('pending', 'confirmed') AND ended_at IS NULL AND ended_by IS NULL)
        OR (status IN ('canceled', 'rejected', 'completed') AND ended_at IS NOT NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS reservation_one_active_per_product
    ON reservation(product_id) WHERE status IN ('pending', 'confirmed');
CREATE INDEX IF NOT EXISTS reservation_buyer_history
    ON reservation(buyer_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS reservation_product_history
    ON reservation(product_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS reservation_pending_deadline
    ON reservation(created_at, id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS reservation_confirmed_deadline
    ON reservation(confirmed_at, id) WHERE status = 'confirmed';

CREATE TABLE IF NOT EXISTS request_result (
    account_id INTEGER NOT NULL REFERENCES account(id),
    action TEXT NOT NULL,
    request_key TEXT NOT NULL,
    digest TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (account_id, action, request_key)
);
