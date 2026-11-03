-- AidBridge schema, financial-needs version.
-- Apply to a NEW, empty database as the owner (postgres). It replaces the earlier schema
-- and the add_*.sql migration files. The role dp_app must already exist.
--
-- Principles: the database enforces every rule that protects money and privacy, so a bug
-- in a route cannot break them. The app role gets only the columns it needs.

-- ---------------------------------------------------------------- users, budgets, audit
CREATE TABLE users (
    id            bigserial PRIMARY KEY,
    username      text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    role          text NOT NULL CHECK (role IN ('analyst', 'admin', 'requester', 'supporter', 'verifier')),
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- One row per analytics dataset: the global privacy budget
CREATE TABLE datasets (
    id            serial PRIMARY KEY,
    name          text NOT NULL UNIQUE,
    epsilon_total numeric(10,4) NOT NULL CHECK (epsilon_total > 0),
    epsilon_spent numeric(10,4) NOT NULL DEFAULT 0 CHECK (epsilon_spent >= 0),
    CONSTRAINT budget_not_exceeded CHECK (epsilon_spent <= epsilon_total)
);

-- The budget is spend-only: it can never decrease, and the total can't be raised
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

-- Failed-login log for rate limiting (append-only for the app role)
CREATE TABLE login_failures (
    id       bigserial PRIMARY KEY,
    ts       timestamptz NOT NULL DEFAULT now(),
    username text NOT NULL,
    ip       text NOT NULL
);
CREATE INDEX login_failures_user_ts ON login_failures (username, ts);
CREATE INDEX login_failures_ip_ts ON login_failures (ip, ts);

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

-- ---------------------------------------------------------------- tunable values
-- One row, edited by the owner only. The app role can read it but not change it.
CREATE TABLE settings (
    id                      boolean PRIMARY KEY DEFAULT true CHECK (id),
    pledge_floor            integer       NOT NULL DEFAULT 10 CHECK (pledge_floor >= 1),           -- lira
    pledge_expiry_days      integer       NOT NULL DEFAULT 7  CHECK (pledge_expiry_days BETWEEN 1 AND 60),
    max_unconfirmed_pledges integer       NOT NULL DEFAULT 10 CHECK (max_unconfirmed_pledges >= 1),
    suggestion_min_needs    integer       NOT NULL DEFAULT 20 CHECK (suggestion_min_needs >= 1),
    eur_try_rate            numeric(10,4) NOT NULL DEFAULT 50 CHECK (eur_try_rate > 0)              -- PLACEHOLDER rate
);
INSERT INTO settings DEFAULT VALUES;

-- Goal bands in lira for lira needs. A need's band is the highest lo that is <= its goal.
CREATE TABLE amount_bands (
    code  integer PRIMARY KEY,
    lo    integer NOT NULL UNIQUE,
    label text NOT NULL
);
INSERT INTO amount_bands VALUES
    (1, 100,    '100 to 1,000 TRY'),
    (2, 1000,   '1,000 to 5,000 TRY'),
    (3, 5000,   '5,000 to 25,000 TRY'),
    (4, 25000,  '25,000 to 100,000 TRY'),
    (5, 100000, '100,000 to 500,000 TRY'),
    (6, 500000, '500,000 to 1,000,000 TRY');

-- Per-purpose goal ceilings (placeholders chosen by the author, not figures from data)
CREATE TABLE purpose_limits (
    key      text PRIMARY KEY,
    currency text NOT NULL CHECK (currency IN ('TRY', 'EUR')),
    ceiling  integer NOT NULL CHECK (ceiling >= 100)
);
INSERT INTO purpose_limits VALUES
    ('rent_housing',      'TRY',  300000),
    ('utilities',         'TRY',  300000),
    ('food',              'TRY',  300000),
    ('medical_turkey',    'TRY', 1000000),
    ('medical_abroad',    'EUR', 2000000),
    ('education',         'TRY',  300000),
    ('disaster_recovery', 'TRY', 1000000),
    ('debt',              'TRY',  300000),
    ('other',             'TRY',  300000);

-- ---------------------------------------------------------------- needs
CREATE TABLE needs (
    id               bigserial PRIMARY KEY,
    requester_id     bigint NOT NULL REFERENCES users(id),
    purpose          text NOT NULL CHECK (purpose IN
        ('rent_housing', 'utilities', 'food', 'medical', 'education',
         'disaster_recovery', 'debt', 'other')),
    medical_location text CHECK (medical_location IN ('turkey', 'abroad')),
    event            text CHECK (event IN ('earthquake', 'flood', 'fire', 'other_disaster')),
    currency         text NOT NULL CHECK (currency IN ('TRY', 'EUR')),
    province         text NOT NULL CHECK (length(province) BETWEEN 2 AND 40),
    district         text NOT NULL CHECK (length(district) BETWEEN 2 AND 40),
    household_size   smallint NOT NULL CHECK (household_size BETWEEN 1 AND 12),         -- private
    goal             integer NOT NULL CHECK (goal >= 100),                              -- in the need's own currency
    description      text NOT NULL CHECK (length(description) BETWEEN 10 AND 1000),     -- private
    contact_method   text NOT NULL DEFAULT 'other' CHECK (contact_method IN ('phone', 'email', 'whatsapp', 'other')),
    contact          text NOT NULL CHECK (length(contact) BETWEEN 3 AND 200),           -- private
    payee_name       text NOT NULL CHECK (length(payee_name) BETWEEN 2 AND 100),        -- private until a pledge is made
    payee_details    text NOT NULL CHECK (length(payee_details) BETWEEN 5 AND 200),     -- private until a pledge is made
    payee_verified   boolean NOT NULL DEFAULT false,
    public_text      text CHECK (public_text IS NULL OR length(public_text) <= 300),
    status           text NOT NULL DEFAULT 'pending' CHECK (status IN
        ('pending', 'published', 'rejected', 'fulfilled', 'withdrawn')),
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    reviewed_by      bigint REFERENCES users(id),
    reviewed_at      timestamptz,
    review_note      text CHECK (review_note IS NULL OR length(review_note) <= 500),
    withdrawn_reason text,
    withdrawn_at     timestamptz,
    CONSTRAINT medical_has_location CHECK ((purpose = 'medical') = (medical_location IS NOT NULL)),
    CONSTRAINT euro_only_for_medical_abroad CHECK
        ((currency = 'EUR') = (purpose = 'medical' AND medical_location = 'abroad')),
    CONSTRAINT event_only_for_disaster CHECK ((purpose = 'disaster_recovery') = (event IS NOT NULL)),
    CONSTRAINT public_needs_are_complete CHECK
        (status IN ('pending', 'rejected', 'withdrawn') OR (public_text IS NOT NULL AND payee_verified)),
    CONSTRAINT withdrawal_has_reason CHECK ((status = 'withdrawn') = (withdrawn_reason IS NOT NULL)),
    CONSTRAINT withdrawal_reason_length CHECK
        (withdrawn_reason IS NULL OR length(withdrawn_reason) BETWEEN 10 AND 500)
);
CREATE INDEX needs_board ON needs (status, purpose, province);
CREATE INDEX needs_requester ON needs (requester_id);

-- New needs must respect the ceiling for their purpose and the right currency.
CREATE FUNCTION check_need_insert() RETURNS trigger AS $$
DECLARE
    lim purpose_limits%ROWTYPE;
BEGIN
    SELECT * INTO lim FROM purpose_limits
        WHERE key = CASE WHEN NEW.purpose = 'medical' THEN 'medical_' || NEW.medical_location ELSE NEW.purpose END;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no limit defined for this purpose';
    END IF;
    IF lim.currency <> NEW.currency THEN
        RAISE EXCEPTION 'wrong currency for this purpose';
    END IF;
    IF NEW.goal > lim.ceiling THEN
        RAISE EXCEPTION 'goal above the ceiling of % % for this purpose', lim.ceiling, lim.currency;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER needs_insert_check BEFORE INSERT ON needs
    FOR EACH ROW EXECUTE FUNCTION check_need_insert();

-- Status order. Fulfilled, rejected and withdrawn are final.
CREATE FUNCTION check_need_transition() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;
    IF (OLD.status, NEW.status) IN (
        ('pending', 'published'), ('pending', 'rejected'), ('pending', 'withdrawn'),
        ('published', 'fulfilled'), ('published', 'withdrawn')) THEN
        IF NEW.status = 'withdrawn' THEN
            NEW.withdrawn_at := now();
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'invalid need status change: % to %', OLD.status, NEW.status;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER needs_status_flow BEFORE UPDATE ON needs
    FOR EACH ROW EXECUTE FUNCTION check_need_transition();

-- ---------------------------------------------------------------- pledges
CREATE TABLE pledges (
    id             bigserial PRIMARY KEY,
    need_id        bigint NOT NULL REFERENCES needs(id),
    supporter_id   bigint NOT NULL REFERENCES users(id),
    amount_try     integer NOT NULL CHECK (amount_try >= 1),
    amount_need    numeric(14,2) NOT NULL,          -- same amount in the need's currency (set by trigger)
    rate_used      numeric(10,4),                   -- euro needs only: lira per euro when pledged (set by trigger)
    ack            boolean NOT NULL CHECK (ack),    -- supporter accepted the withdrawal and refund notice
    status         text NOT NULL DEFAULT 'pledged' CHECK (status IN
        ('pledged', 'sent', 'confirmed', 'disputed', 'expired', 'cancelled')),
    contact_method text NOT NULL DEFAULT 'other' CHECK (contact_method IN ('phone', 'email', 'whatsapp', 'other')),
    contact        text CHECK (contact IS NULL OR length(contact) BETWEEN 3 AND 200),   -- verifiers only
    created_at     timestamptz NOT NULL DEFAULT now(),
    expires_at     timestamptz,
    sent_at        timestamptz,
    confirmed_at   timestamptz
);
CREATE INDEX pledges_need ON pledges (need_id, status);
CREATE INDEX pledges_supporter ON pledges (supporter_id, status);

-- One place enforces every rule for a new pledge, under locks, so concurrent pledges
-- cannot oversubscribe a need or exceed a supporter's limit.
CREATE FUNCTION check_pledge() RETURNS trigger AS $$
DECLARE
    n            needs%ROWTYPE;
    s            settings%ROWTYPE;
    active_total numeric;
    open_count   bigint;
BEGIN
    SELECT * INTO s FROM settings;
    PERFORM pg_advisory_xact_lock(7001, NEW.supporter_id::int);
    SELECT * INTO n FROM needs WHERE id = NEW.need_id FOR UPDATE;
    IF NOT FOUND OR n.status <> 'published' THEN
        RAISE EXCEPTION 'need is not open for pledges';
    END IF;
    IF n.requester_id = NEW.supporter_id THEN
        RAISE EXCEPTION 'cannot pledge to your own need';
    END IF;
    IF NEW.amount_try < s.pledge_floor THEN
        RAISE EXCEPTION 'pledge below the floor of % lira', s.pledge_floor;
    END IF;
    IF n.currency = 'EUR' THEN
        NEW.rate_used := s.eur_try_rate;
        NEW.amount_need := round(NEW.amount_try / s.eur_try_rate, 2);
    ELSE
        NEW.rate_used := NULL;
        NEW.amount_need := NEW.amount_try;
    END IF;
    SELECT COALESCE(sum(amount_need), 0) INTO active_total FROM pledges
        WHERE need_id = NEW.need_id AND status IN ('pledged', 'sent', 'confirmed', 'disputed');
    IF active_total + NEW.amount_need > n.goal THEN
        RAISE EXCEPTION 'pledge would exceed the goal';
    END IF;
    SELECT count(*) INTO open_count FROM pledges
        WHERE supporter_id = NEW.supporter_id AND status IN ('pledged', 'sent', 'disputed');
    IF open_count >= s.max_unconfirmed_pledges THEN
        RAISE EXCEPTION 'too many unconfirmed pledges';
    END IF;
    NEW.status := 'pledged';
    NEW.expires_at := now() + make_interval(days => s.pledge_expiry_days);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER pledges_insert_check BEFORE INSERT ON pledges
    FOR EACH ROW EXECUTE FUNCTION check_pledge();

CREATE FUNCTION check_pledge_transition() RETURNS trigger AS $$
BEGIN
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;
    IF (OLD.status, NEW.status) IN (
        ('pledged', 'sent'), ('pledged', 'cancelled'), ('pledged', 'expired'),
        ('sent', 'confirmed'), ('sent', 'disputed'),
        ('disputed', 'confirmed'), ('disputed', 'cancelled')) THEN
        IF NEW.status = 'sent' THEN NEW.sent_at := now(); END IF;
        IF NEW.status = 'confirmed' THEN NEW.confirmed_at := now(); END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'invalid pledge status change: % to %', OLD.status, NEW.status;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER pledges_status_flow BEFORE UPDATE ON pledges
    FOR EACH ROW EXECUTE FUNCTION check_pledge_transition();

-- A need is fulfilled when confirmed receipts leave less than one minimum pledge to raise
-- (nobody could pledge a smaller remainder, so waiting for it would strand the need).
CREATE FUNCTION fulfil_if_funded() RETURNS trigger AS $$
DECLARE
    n          needs%ROWTYPE;
    s          settings%ROWTYPE;
    conf       numeric;
    floor_need numeric;
BEGIN
    IF NEW.status = 'confirmed' AND OLD.status <> 'confirmed' THEN
        SELECT * INTO n FROM needs WHERE id = NEW.need_id FOR UPDATE;
        IF n.status = 'published' THEN
            SELECT * INTO s FROM settings;
            SELECT COALESCE(sum(amount_need), 0) INTO conf FROM pledges
                WHERE need_id = NEW.need_id AND status = 'confirmed';
            floor_need := CASE WHEN n.currency = 'EUR'
                               THEN s.pledge_floor::numeric / s.eur_try_rate
                               ELSE s.pledge_floor END;
            IF n.goal - conf < floor_need THEN
                UPDATE needs SET status = 'fulfilled' WHERE id = n.id;
            END IF;
        END IF;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER pledges_fulfil AFTER UPDATE ON pledges
    FOR EACH ROW EXECUTE FUNCTION fulfil_if_funded();

-- Withdrawing a need cancels pledges that have not been sent yet. Pledges already sent or
-- confirmed stay as they are: refunds are between the donor and the requester.
CREATE FUNCTION cancel_open_pledges() RETURNS trigger AS $$
BEGIN
    UPDATE pledges SET status = 'cancelled' WHERE need_id = NEW.id AND status = 'pledged';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER needs_withdraw_cascade AFTER UPDATE OF status ON needs
    FOR EACH ROW WHEN (NEW.status = 'withdrawn' AND OLD.status <> 'withdrawn')
    EXECUTE FUNCTION cancel_open_pledges();

-- Pledges nobody marked as sent expire and free their share of the goal. Call it before
-- reading totals (or on a schedule).
CREATE FUNCTION expire_pledges() RETURNS integer AS $$
DECLARE k integer;
BEGIN
    UPDATE pledges SET status = 'expired' WHERE status = 'pledged' AND expires_at < now();
    GET DIAGNOSTICS k = ROW_COUNT;
    RETURN k;
END;
$$ LANGUAGE plpgsql;

CREATE TABLE need_events (
    id       bigserial PRIMARY KEY,
    ts       timestamptz NOT NULL DEFAULT now(),
    need_id  bigint NOT NULL REFERENCES needs(id),
    actor_id bigint NOT NULL REFERENCES users(id),
    event    text NOT NULL,
    note     text CHECK (note IS NULL OR length(note) <= 500)
);

-- ---------------------------------------------------------------- synthetic twin
-- Same columns as the analytics view, so one engine can read either. Assumed data, not real.
CREATE TABLE needs_synth (
    id               bigserial PRIMARY KEY,
    person_id        text NOT NULL,
    purpose          text NOT NULL CHECK (purpose IN
        ('rent_housing', 'utilities', 'food', 'medical', 'education',
         'disaster_recovery', 'debt', 'other')),
    medical_location text CHECK (medical_location IN ('turkey', 'abroad')),
    event            text CHECK (event IN ('earthquake', 'flood', 'fire', 'other_disaster')),
    province         text NOT NULL CHECK (length(province) BETWEEN 2 AND 40),
    household_size   smallint NOT NULL CHECK (household_size BETWEEN 1 AND 12),
    currency         text NOT NULL CHECK (currency IN ('TRY', 'EUR')),
    goal             integer NOT NULL CHECK (goal >= 100),
    status           text NOT NULL CHECK (status IN ('published', 'fulfilled')),
    created_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT synth_medical_has_location CHECK ((purpose = 'medical') = (medical_location IS NOT NULL)),
    CONSTRAINT synth_euro_only_for_medical_abroad CHECK
        ((currency = 'EUR') = (purpose = 'medical' AND medical_location = 'abroad')),
    CONSTRAINT synth_event_only_for_disaster CHECK ((purpose = 'disaster_recovery') = (event IS NOT NULL))
);
CREATE INDEX needs_synth_person ON needs_synth (person_id, id);

-- ---------------------------------------------------------------- views
-- The public board: published needs only, no private column. Totals are derived from
-- pledges and never stored, so they cannot drift. band is set for lira needs only.
CREATE VIEW public_needs AS
SELECT n.id,
       date_trunc('month', n.created_at) AS month,
       n.purpose, n.medical_location, n.event, n.province, n.district,
       n.currency, n.goal, b.code AS band, n.public_text,
       COALESCE(p.promised, 0)  AS promised,
       COALESCE(p.confirmed, 0) AS confirmed,
       COALESCE(p.donors, 0)    AS donors
FROM needs n
LEFT JOIN LATERAL (SELECT code FROM amount_bands WHERE lo <= n.goal ORDER BY lo DESC LIMIT 1) b
       ON n.currency = 'TRY'
LEFT JOIN (
    SELECT need_id,
           sum(amount_need) AS promised,
           COALESCE(sum(amount_need) FILTER (WHERE status = 'confirmed'), 0) AS confirmed,
           count(DISTINCT supporter_id) AS donors
    FROM pledges
    WHERE status IN ('pledged', 'sent', 'confirmed', 'disputed')
    GROUP BY need_id) p ON p.need_id = n.id
WHERE n.status = 'published';

-- What analytics may read: verified needs, safe columns only. The requester id, cast to
-- text, is the person key for the per-person cap. Nothing private is listed here.
CREATE VIEW needs_analytics AS
SELECT n.id, n.requester_id::text AS person_id, n.purpose, n.medical_location, n.event,
       n.province, n.household_size, n.currency, n.goal, b.code AS band, n.status, n.created_at
FROM needs n
LEFT JOIN LATERAL (SELECT code FROM amount_bands WHERE lo <= n.goal ORDER BY lo DESC LIMIT 1) b
       ON n.currency = 'TRY'
WHERE n.status IN ('published', 'fulfilled');

CREATE VIEW synth_analytics AS
SELECT s.id, s.person_id, s.purpose, s.medical_location, s.event,
       s.province, s.household_size, s.currency, s.goal, b.code AS band, s.status, s.created_at
FROM needs_synth s
LEFT JOIN LATERAL (SELECT code FROM amount_bands WHERE lo <= s.goal ORDER BY lo DESC LIMIT 1) b
       ON s.currency = 'TRY';

-- Public statistics behind the "suggested amount" button: finished lira needs per band
CREATE VIEW suggestion_stats AS
SELECT b.code AS band, count(*) AS finished_needs, round(avg(d.donors), 1) AS avg_donors
FROM needs n
JOIN LATERAL (SELECT code FROM amount_bands WHERE lo <= n.goal ORDER BY lo DESC LIMIT 1) b ON true
JOIN (SELECT need_id, count(DISTINCT supporter_id) AS donors FROM pledges
      WHERE status = 'confirmed' GROUP BY need_id) d ON d.need_id = n.id
WHERE n.status = 'fulfilled' AND n.currency = 'TRY'
GROUP BY b.code;

-- ---------------------------------------------------------------- privileges
GRANT USAGE ON SCHEMA public TO dp_app;

GRANT SELECT, INSERT ON users, audit_log, results, login_failures TO dp_app;
GRANT SELECT ON datasets TO dp_app;
GRANT UPDATE (epsilon_spent) ON datasets TO dp_app;
GRANT USAGE ON SEQUENCE users_id_seq, audit_log_id_seq, results_id_seq, login_failures_id_seq TO dp_app;
REVOKE UPDATE, DELETE, TRUNCATE ON audit_log, results, login_failures FROM dp_app;

GRANT SELECT ON settings, amount_bands, purpose_limits TO dp_app;

-- A requester fills in only these columns. Status, verification and text are not insertable,
-- and payee details cannot be updated after posting (no UPDATE grant on them).
GRANT SELECT ON needs, pledges, need_events TO dp_app;
GRANT INSERT (requester_id, purpose, medical_location, event, currency, province, district,
              household_size, goal, description, contact_method, contact,
              payee_name, payee_details) ON needs TO dp_app;
GRANT UPDATE (status, public_text, payee_verified, reviewed_by, reviewed_at, review_note,
              withdrawn_reason) ON needs TO dp_app;
GRANT INSERT (need_id, supporter_id, amount_try, ack, contact_method, contact) ON pledges TO dp_app;
GRANT UPDATE (status) ON pledges TO dp_app;
GRANT INSERT ON need_events TO dp_app;
GRANT USAGE ON SEQUENCE needs_id_seq, pledges_id_seq, need_events_id_seq TO dp_app;
GRANT EXECUTE ON FUNCTION expire_pledges() TO dp_app;
REVOKE DELETE, TRUNCATE ON needs, pledges FROM dp_app;
REVOKE UPDATE, DELETE, TRUNCATE ON need_events FROM dp_app;

GRANT SELECT ON public_needs, needs_analytics, synth_analytics, suggestion_stats TO dp_app;
GRANT INSERT ON needs_synth TO dp_app;
GRANT USAGE ON SEQUENCE needs_synth_id_seq TO dp_app;
REVOKE UPDATE, DELETE, TRUNCATE ON needs_synth FROM dp_app;

-- One privacy budget per analytics dataset
INSERT INTO datasets (name, epsilon_total) VALUES ('needs_synthetic', 10.0), ('needs_live', 10.0);
