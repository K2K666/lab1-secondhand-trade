CREATE TABLE IF NOT EXISTS product (
    id INTEGER PRIMARY KEY,
    seller_id INTEGER NOT NULL REFERENCES account(id),
    category_id INTEGER REFERENCES category(id),
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 50),
    description TEXT NOT NULL DEFAULT '',
    price_cents INTEGER CHECK (price_cents BETWEEN 100 AND 1000000),
    location TEXT NOT NULL DEFAULT '',
    condition TEXT NOT NULL DEFAULT '',
    contact TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN (
        'draft', 'pending_review', 'published', 'rejected', 'downlisted',
        'reserved', 'sold', 'violation'
    )),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    published_at INTEGER,
    first_published_at INTEGER,
    sold_at INTEGER,
    hidden INTEGER NOT NULL DEFAULT 0 CHECK (hidden IN (0, 1))
);

CREATE INDEX IF NOT EXISTS ix_product_status_published
    ON product(status, published_at, id);
CREATE INDEX IF NOT EXISTS ix_product_seller ON product(seller_id, id);
CREATE INDEX IF NOT EXISTS ix_product_status_category_price
    ON product(status, category_id, price_cents);

-- Photo bytes share the product transaction and are never exposed as raw JSON.
CREATE TABLE IF NOT EXISTS product_photo (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES product(id),
    position INTEGER NOT NULL CHECK (position BETWEEN 0 AND 8),
    mime TEXT NOT NULL CHECK (mime IN ('image/png', 'image/jpeg')),
    body BLOB NOT NULL CHECK (length(body) BETWEEN 1 AND 5242880),
    UNIQUE (product_id, position)
);

CREATE TABLE IF NOT EXISTS product_review (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES product(id),
    kind TEXT NOT NULL CHECK (kind IN ('submission', 'appeal')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'closed')),
    request_reason TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    actor_id INTEGER REFERENCES account(id),
    created_at INTEGER NOT NULL,
    decided_at INTEGER
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_open_product_review
    ON product_review(product_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS ix_review_status ON product_review(status, id);
CREATE INDEX IF NOT EXISTS ix_review_history ON product_review(product_id, id);

CREATE TABLE IF NOT EXISTS violation (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES product(id),
    actor_id INTEGER NOT NULL REFERENCES account(id),
    reason TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_violation_product ON violation(product_id, id);

CREATE TABLE IF NOT EXISTS product_report (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES product(id),
    reporter_id INTEGER NOT NULL REFERENCES account(id),
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'dismissed', 'violation')),
    actor_id INTEGER REFERENCES account(id),
    decision_reason TEXT NOT NULL DEFAULT '',
    violation_id INTEGER REFERENCES violation(id),
    created_at INTEGER NOT NULL,
    decided_at INTEGER
);
CREATE INDEX IF NOT EXISTS ix_report_status ON product_report(status, id);
