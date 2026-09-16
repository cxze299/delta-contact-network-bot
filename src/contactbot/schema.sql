PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_key TEXT NOT NULL UNIQUE,
    contact_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    is_system_admin INTEGER NOT NULL DEFAULT 0 CHECK (is_system_admin IN (0, 1)),
    current_network_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS networks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    join_code_hash TEXT NOT NULL UNIQUE,
    join_code_ciphertext TEXT,
    join_code_hint TEXT NOT NULL,
    join_enabled INTEGER NOT NULL DEFAULT 1 CHECK (join_enabled IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memberships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    network_id INTEGER NOT NULL REFERENCES networks(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    member_code TEXT NOT NULL,
    nickname TEXT NOT NULL,
    bio TEXT NOT NULL DEFAULT '',
    discoverable INTEGER NOT NULL DEFAULT 0 CHECK (discoverable IN (0, 1)),
    accepts_requests INTEGER NOT NULL DEFAULT 0 CHECK (accepts_requests IN (0, 1)),
    share_contact TEXT,
    share_link_ciphertext TEXT,
    share_version INTEGER NOT NULL DEFAULT 0,
    role TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('member', 'network_admin')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'removed', 'left')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(network_id, user_id),
    UNIQUE(network_id, member_code)
);

CREATE TABLE IF NOT EXISTS bans (
    network_id INTEGER NOT NULL REFERENCES networks(id) ON DELETE CASCADE,
    actor_key TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(network_id, actor_key)
);

CREATE TABLE IF NOT EXISTS contact_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id TEXT NOT NULL UNIQUE,
    network_id INTEGER NOT NULL REFERENCES networks(id) ON DELETE CASCADE,
    requester_membership_id INTEGER NOT NULL REFERENCES memberships(id),
    target_membership_id INTEGER NOT NULL REFERENCES memberships(id),
    reason TEXT NOT NULL,
    requester_contact TEXT NOT NULL,
    requester_share_version INTEGER NOT NULL,
    target_contact TEXT,
    target_share_version INTEGER,
    request_kind TEXT NOT NULL DEFAULT 'legacy' CHECK (request_kind IN ('legacy', 'vcard')),
    status TEXT NOT NULL CHECK (status IN (
        'pending_target', 'processing', 'completed', 'rejected', 'withdrawn', 'expired', 'invalid'
    )),
    expires_at TEXT NOT NULL,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS one_open_request_per_pair
ON contact_requests(
    network_id,
    min(requester_membership_id, target_membership_id),
    max(requester_membership_id, target_membership_id)
)
WHERE status IN ('pending_target', 'processing');

CREATE TABLE IF NOT EXISTS blocks (
    network_id INTEGER NOT NULL REFERENCES networks(id) ON DELETE CASCADE,
    blocker_membership_id INTEGER NOT NULL REFERENCES memberships(id) ON DELETE CASCADE,
    blocked_membership_id INTEGER NOT NULL REFERENCES memberships(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY(network_id, blocker_membership_id, blocked_membership_id)
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id TEXT NOT NULL UNIQUE,
    network_id INTEGER NOT NULL REFERENCES networks(id) ON DELETE CASCADE,
    reporter_membership_id INTEGER NOT NULL REFERENCES memberships(id),
    target_membership_id INTEGER NOT NULL REFERENCES memberships(id),
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    resolution TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    network_id INTEGER REFERENCES networks(id) ON DELETE SET NULL,
    actor_user_id INTEGER NOT NULL REFERENCES users(id),
    action TEXT NOT NULL,
    target_ref TEXT NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incoming_messages (
    account_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    processed_at TEXT NOT NULL,
    PRIMARY KEY(account_id, message_id)
);

CREATE TABLE IF NOT EXISTS pending_actions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    payload TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    requires_encryption INTEGER NOT NULL DEFAULT 0 CHECK (requires_encryption IN (0, 1)),
    vcard_contact_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sending', 'sent')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT
);

CREATE TABLE IF NOT EXISTS rate_events (
    actor_key TEXT NOT NULL,
    category TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS link_shares (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    network_id INTEGER NOT NULL REFERENCES networks(id) ON DELETE CASCADE,
    sender_membership_id INTEGER NOT NULL REFERENCES memberships(id) ON DELETE CASCADE,
    target_membership_id INTEGER NOT NULL REFERENCES memberships(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_recoveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id TEXT NOT NULL UNIQUE,
    membership_id INTEGER NOT NULL REFERENCES memberships(id) ON DELETE CASCADE,
    requester_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'approved', 'rejected', 'expired')
    ),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_by INTEGER REFERENCES users(id),
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS rate_events_lookup ON rate_events(actor_key, category, created_at);
CREATE INDEX IF NOT EXISTS link_shares_sender ON link_shares(sender_membership_id, created_at);
CREATE INDEX IF NOT EXISTS link_shares_pair ON link_shares(sender_membership_id, target_membership_id, created_at);
CREATE INDEX IF NOT EXISTS memberships_lookup ON memberships(network_id, status, member_code);
CREATE INDEX IF NOT EXISTS requests_target ON contact_requests(target_membership_id, status);
CREATE INDEX IF NOT EXISTS reports_network ON reports(network_id, status);
CREATE INDEX IF NOT EXISTS pending_actions_user ON pending_actions(user_id, expires_at);
CREATE INDEX IF NOT EXISTS recoveries_status ON account_recoveries(status, expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_recovery_per_membership
ON account_recoveries(membership_id) WHERE status='pending';
PRAGMA user_version = 6;
