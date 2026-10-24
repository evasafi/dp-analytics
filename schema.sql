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
INSERT INTO datasets (name, epsilon_total) VALUES ('assistance_program', 5.0);

-- Privileges for the app role: only what it needs
GRANT USAGE ON SCHEMA public TO dp_app;
GRANT SELECT, INSERT ON users, beneficiaries, audit_log TO dp_app;
GRANT SELECT ON datasets TO dp_app;
GRANT UPDATE (epsilon_spent) ON datasets TO dp_app;
GRANT USAGE ON SEQUENCE users_id_seq, audit_log_id_seq TO dp_app;
REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM dp_app;