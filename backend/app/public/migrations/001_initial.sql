CREATE TABLE users (
    id text PRIMARY KEY,
    email text UNIQUE NOT NULL,
    password_hash text NOT NULL,
    verified boolean NOT NULL DEFAULT false,
    banned boolean NOT NULL DEFAULT false,
    is_admin boolean NOT NULL DEFAULT false,
    daily_minutes integer NOT NULL DEFAULT 120 CHECK (daily_minutes BETWEEN 1 AND 1440),
    created_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz
);
CREATE TABLE invitations (
    id text PRIMARY KEY,
    token_hash text UNIQUE NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL DEFAULT now() + interval '7 days',
    revoked boolean NOT NULL DEFAULT false,
    used_by text REFERENCES users(id) ON DELETE SET NULL,
    used_at timestamptz
);
CREATE TABLE auth_tokens (
    token_hash text PRIMARY KEY,
    user_id text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN ('verify', 'reset')),
    expires_at timestamptz NOT NULL
);
CREATE TABLE sessions (
    token_hash text PRIMARY KEY,
    user_id text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_hash text NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE INDEX sessions_user ON sessions(user_id);
CREATE TABLE site_settings (
    id integer PRIMARY KEY CHECK (id=1),
    registration_open boolean NOT NULL DEFAULT true,
    queue_open boolean NOT NULL DEFAULT true
);
INSERT INTO site_settings(id) VALUES (1);
CREATE TABLE jobs (
    id text PRIMARY KEY,
    user_id text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind text NOT NULL DEFAULT 'inspect' CHECK (kind IN ('inspect', 'convert')),
    status text NOT NULL DEFAULT 'uploading' CHECK (status IN ('uploading','uploaded','queued','running','success','failed','cancelled')),
    filename text NOT NULL DEFAULT '',
    size bigint NOT NULL DEFAULT 0,
    request jsonb,
    wine boolean NOT NULL DEFAULT false,
    cancel_requested boolean NOT NULL DEFAULT false,
    delete_requested boolean NOT NULL DEFAULT false,
    lease_token text,
    lease_until timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    expires_at timestamptz NOT NULL DEFAULT now() + interval '24 hours',
    quota_date date,
    reserved_minutes integer NOT NULL DEFAULT 0,
    billed_minutes integer NOT NULL DEFAULT 0,
    error text
);
CREATE INDEX jobs_queue ON jobs(status, created_at);
CREATE INDEX jobs_owner ON jobs(user_id, created_at DESC);
CREATE TABLE job_metadata (
    id text PRIMARY KEY,
    metadata jsonb NOT NULL
);
CREATE TABLE usage (
    user_id text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day date NOT NULL,
    used integer NOT NULL DEFAULT 0 CHECK (used >= 0),
    reserved integer NOT NULL DEFAULT 0 CHECK (reserved >= 0),
    PRIMARY KEY (user_id, day)
);
CREATE TABLE rate_limits (
    key text PRIMARY KEY,
    hits integer NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE TABLE audit (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor_id text,
    action text NOT NULL,
    target text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE worker_status (
    id text PRIMARY KEY,
    heartbeat_at timestamptz NOT NULL DEFAULT now(),
    active integer NOT NULL DEFAULT 0,
    disk_percent integer NOT NULL DEFAULT 0
);
CREATE VIEW user_limits AS SELECT id, daily_minutes, verified, banned FROM users;
