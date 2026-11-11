CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY,
    phone TEXT NOT NULL UNIQUE,
    nickname TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('member', 'admin', 'super')),
    must_change INTEGER NOT NULL DEFAULT 0 CHECK (must_change IN (0, 1)),
    avatar TEXT NOT NULL DEFAULT '',
    bio TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS only_one_super
    ON account(role) WHERE role = 'super';

CREATE TABLE IF NOT EXISTS verification_code (
    phone TEXT PRIMARY KEY,
    code_hash TEXT NOT NULL,
    issued_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS session (
    token_hash TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES account(id),
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS session_account ON session(account_id);

CREATE TABLE IF NOT EXISTS category (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
INSERT OR IGNORE INTO category (id, name) VALUES
    (1, '教材与书籍'), (2, '数码配件'), (3, '宿舍用品'),
    (4, '运动户外'), (5, '其他');

CREATE TABLE IF NOT EXISTS notification (
    id INTEGER PRIMARY KEY,
    recipient_id INTEGER NOT NULL REFERENCES account(id),
    event_key TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    read_at INTEGER,
    UNIQUE (recipient_id, event_key)
);
CREATE INDEX IF NOT EXISTS notification_recipient
    ON notification(recipient_id, id DESC);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    actor_id INTEGER REFERENCES account(id),
    action TEXT NOT NULL,
    object_type TEXT,
    object_id INTEGER,
    product_id INTEGER,
    before TEXT,
    after TEXT,
    success INTEGER NOT NULL CHECK (success IN (0, 1)),
    detail TEXT,
    ip TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_by_time ON audit_log(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS audit_by_actor ON audit_log(actor_id, id DESC);
CREATE INDEX IF NOT EXISTS audit_by_product ON audit_log(product_id, id DESC);
