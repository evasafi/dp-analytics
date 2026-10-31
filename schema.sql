-- Users and roles (role enforced again in the API layer)
CREATE TABLE users (
    id            bigserial PRIMARY KEY,
    username      text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    role          text NOT NULL CHECK (role IN ('field_agent', 'analyst', 'admin')),
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- One row per dataset: the global privacy budget
CREATE TABLE datasets (
    id            serial PRIMARY KEY,
    name          text NOT NULL UNIQUE,
    epsilon_total numeric(10,4) NOT NULL CHECK (epsilon_total > 0),
    epsilon_spent numeric(10,4) NOT NULL DEFAULT 0 CHECK (epsilon_spent >= 0),
    CONSTRAINT budget_not_exceeded CHECK (epsilon_spent <= epsilon_total)
);

-- Budget is spend-only: it can never decrease, and the total can't be raised
CREATE FUNCTION forbid_budget_reset() RETURNS trigger AS $$
BEGIN
    IF NEW.epsilon_spent < OLD.epsilon_spent
       OR NEW.epsilon_total <> OLD.epsilon_total THEN
        RAISE EXCEPTION 'privacy budget is spend-only';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER datasets_spend_only
    BEFORE UPDATE ON datasets
    FOR EACH ROW EXECUTE FUNCTION forbid_budget_reset();

-- One record per person per dataset (the person is the privacy unit)
CREATE TABLE beneficiaries (
    dataset_id       integer NOT NULL REFERENCES datasets(id),
    beneficiary_id   text NOT NULL,   -- random pseudonym generated at intake
    region           text NOT NULL CHECK (region IN
        ('region_1','region_2','region_3','region_4',
         'region_5','region_6','region_7','region_8')),
    household_size   integer NOT NULL CHECK (household_size BETWEEN 1 AND 12),
    monthly_transfer numeric(6,2) NOT NULL CHECK (monthly_transfer BETWEEN 0 AND 500),
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (dataset_id, beneficiary_id)
);

-- Append-only audit log
CREATE TABLE audit_log (
    id              bigserial PRIMARY KEY,
    ts              timestamptz NOT NULL DEFAULT now(),
    user_id         bigint NOT NULL REFERENCES users(id),
    dataset_id      integer NOT NULL REFERENCES datasets(id),
    query_type      text NOT NULL,
    params          jsonb NOT NULL,
    epsilon_charged numeric(10,4) NOT NULL,
    result_hash     text NOT NULL
);

-- Seed the one dataset, with the placeholder budget from config
INSERT INTO datasets (name, epsilon_total) VALUES ('assistance_program', 10.0);

-- Privileges for the app role: only what it needs
GRANT USAGE ON SCHEMA public TO dp_app;
GRANT SELECT, INSERT ON users, beneficiaries, audit_log TO dp_app;
GRANT SELECT ON datasets TO dp_app;
GRANT UPDATE (epsilon_spent) ON datasets TO dp_app;
GRANT USAGE ON SEQUENCE users_id_seq, audit_log_id_seq TO dp_app;
REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM dp_app;

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

-- Needs-matching platform, step 1: roles, needs, pledges, events
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN
    ('field_agent', 'analyst', 'admin', 'requester', 'supporter', 'verifier'));

CREATE TABLE needs (
    id           bigserial PRIMARY KEY,
    requester_id bigint NOT NULL REFERENCES users(id),
    category     text NOT NULL CHECK (category IN
        ('baby_child', 'education', 'household', 'electronics', 'clothing',
         'culture_art', 'health', 'street_animals', 'sports', 'hygiene')),
    province     text NOT NULL CHECK (length(province) BETWEEN 2 AND 40),
    district     text NOT NULL CHECK (length(district) BETWEEN 2 AND 40),
    description  text NOT NULL CHECK (length(description) BETWEEN 10 AND 1000),
    contact      text NOT NULL CHECK (length(contact) BETWEEN 3 AND 200),
    public_text  text CHECK (public_text IS NULL OR length(public_text) <= 300),
    status       text NOT NULL DEFAULT 'pending' CHECK (status IN
        ('pending', 'published', 'rejected', 'pledged', 'fulfilled')),
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    reviewed_by  bigint REFERENCES users(id),
    reviewed_at  timestamptz,
    review_note  text CHECK (review_note IS NULL OR length(review_note) <= 500),
    CONSTRAINT public_needs_have_text CHECK
        (status IN ('pending', 'rejected') OR public_text IS NOT NULL)
);
CREATE INDEX needs_board ON needs (status, category, province);
CREATE INDEX needs_requester ON needs (requester_id);

CREATE FUNCTION check_need_transition() RETURNS trigger AS $$
BEGIN
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;
    IF (OLD.status, NEW.status) IN (
        ('pending', 'published'), ('pending', 'rejected'),
        ('published', 'pledged'), ('pledged', 'published'),
        ('pledged', 'fulfilled')) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'invalid need status change: % to %', OLD.status, NEW.status;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER needs_status_flow
    BEFORE UPDATE ON needs
    FOR EACH ROW EXECUTE FUNCTION check_need_transition();

CREATE TABLE pledges (
    id           bigserial PRIMARY KEY,
    need_id      bigint NOT NULL REFERENCES needs(id),
    supporter_id bigint NOT NULL REFERENCES users(id),
    status       text NOT NULL DEFAULT 'pledged' CHECK (status IN
        ('pledged', 'handed_over', 'cancelled')),
    note         text CHECK (note IS NULL OR length(note) <= 500),
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX one_active_pledge ON pledges (need_id)
    WHERE status IN ('pledged', 'handed_over');

CREATE TABLE need_events (
    id       bigserial PRIMARY KEY,
    ts       timestamptz NOT NULL DEFAULT now(),
    need_id  bigint NOT NULL REFERENCES needs(id),
    actor_id bigint NOT NULL REFERENCES users(id),
    event    text NOT NULL,
    note     text CHECK (note IS NULL OR length(note) <= 500)
);

GRANT SELECT, INSERT ON needs, pledges, need_events TO dp_app;
GRANT UPDATE (status, public_text, reviewed_by, reviewed_at, review_note, updated_at) ON needs TO dp_app;
GRANT UPDATE (status) ON pledges TO dp_app;
GRANT USAGE ON SEQUENCE needs_id_seq, pledges_id_seq, need_events_id_seq TO dp_app;
REVOKE DELETE, TRUNCATE ON needs, pledges FROM dp_app;
REVOKE UPDATE, DELETE, TRUNCATE ON need_events FROM dp_app;

-- How a verifier can reach the requester (private; shown to verifiers only)
ALTER TABLE needs ADD COLUMN contact_method text NOT NULL DEFAULT 'other'
    CHECK (contact_method IN ('phone', 'email', 'whatsapp', 'other'));

-- How a verifier can reach the supporter who pledged (private; shown to verifiers only)
ALTER TABLE pledges ADD COLUMN contact_method text NOT NULL DEFAULT 'other'
    CHECK (contact_method IN ('phone', 'email', 'whatsapp', 'other'));
ALTER TABLE pledges ADD COLUMN contact text
    CHECK (contact IS NULL OR length(contact) BETWEEN 3 AND 200);

-- Synthetic needs for analytics demos and experiments (assumed distributions, not real data).
-- Never shown on the board or in the verifier's queue.
CREATE TABLE needs_synth (
    id         bigserial PRIMARY KEY,
    person_id  text NOT NULL,
    category   text NOT NULL CHECK (category IN
        ('baby_child', 'education', 'household', 'electronics', 'clothing',
         'culture_art', 'health', 'street_animals', 'sports', 'hygiene')),
    province   text NOT NULL CHECK (length(province) BETWEEN 2 AND 40),
    status     text NOT NULL CHECK (status IN ('pending', 'published', 'rejected', 'pledged', 'fulfilled')),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX needs_synth_person ON needs_synth (person_id, id);
CREATE INDEX needs_synth_dims ON needs_synth (category, province, status);
GRANT SELECT, INSERT ON needs_synth TO dp_app;
GRANT USAGE ON SEQUENCE needs_synth_id_seq TO dp_app;
REVOKE UPDATE, DELETE, TRUNCATE ON needs_synth FROM dp_app;

-- One privacy budget per analytics dataset
INSERT INTO datasets (name, epsilon_total) VALUES ('needs_synthetic', 10.0), ('needs_live', 10.0)
    ON CONFLICT (name) DO NOTHING;