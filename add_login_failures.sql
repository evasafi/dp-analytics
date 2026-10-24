
-- Failed-login log for rate limiting (append-only for the app role)
CREATE TABLE login_failures (
    id       bigserial PRIMARY KEY,
    ts       timestamptz NOT NULL DEFAULT now(),
    username text NOT NULL,
    ip       text NOT NULL
);
CREATE INDEX login_failures_user_ts ON login_failures (username, ts);
CREATE INDEX login_failures_ip_ts ON login_failures (ip, ts);
GRANT SELECT, INSERT ON login_failures TO dp_app;
GRANT USAGE ON SEQUENCE login_failures_id_seq TO dp_app;
