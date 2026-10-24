
-- Stored noisy results (post-processing of charged answers; append-only for the app role)
CREATE TABLE results (
    id         bigserial PRIMARY KEY,
    ts         timestamptz NOT NULL DEFAULT now(),
    user_id    bigint NOT NULL REFERENCES users(id),
    dataset_id integer NOT NULL REFERENCES datasets(id),
    audit_id   bigint NOT NULL REFERENCES audit_log(id),
    spec       jsonb NOT NULL,
    cells      jsonb NOT NULL
);
CREATE INDEX results_user_ts ON results (user_id, ts);
GRANT SELECT, INSERT ON results TO dp_app;
GRANT USAGE ON SEQUENCE results_id_seq TO dp_app;
REVOKE UPDATE, DELETE, TRUNCATE ON results FROM dp_app;