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
    claim_expiry_days       integer       NOT NULL DEFAULT 7  CHECK (claim_expiry_days BETWEEN 1 AND 60),
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

-- ---------------------------------------------------------------- item types
-- What an item need can ask for. A type with enabled = false cannot be posted yet (used
-- medical equipment waits for the legal check, see the design note).
CREATE TABLE item_types (
    id       serial PRIMARY KEY,
    category text NOT NULL CHECK (category IN
        ('mobility', 'medical_equipment', 'heating', 'clothing', 'bedding', 'kitchen',
         'hygiene_baby', 'food', 'power_comms', 'other')),
    name     text NOT NULL CHECK (length(name) BETWEEN 2 AND 60),
    enabled  boolean NOT NULL DEFAULT true,
    UNIQUE (category, name)
);
INSERT INTO item_types (category, name, enabled) VALUES
    ('mobility', 'Wheelchair, manual', true),
    ('mobility', 'Wheelchair, electric', false),
    ('mobility', 'Wheelchair, child', true),
    ('mobility', 'Walker', true),
    ('mobility', 'Crutches', true),
    ('mobility', 'Hearing aid', false),
    ('mobility', 'Orthosis', true),
    ('medical_equipment', 'Oxygen concentrator', false),
    ('medical_equipment', 'Hospital bed', false),
    ('medical_equipment', 'Diabetic supplies', false),
    ('heating', 'Electric heater', true),
    ('heating', 'Wood or coal stove', true),
    ('heating', 'Gas cylinder', false),
    ('heating', 'Firewood', true),
    ('clothing', 'Coat', true),
    ('clothing', 'Fleece', true),
    ('clothing', 'Shoes', true),
    ('clothing', 'Underwear', true),
    ('clothing', 'Socks', true),
    ('bedding', 'Sleeping bag', true),
    ('bedding', 'Pillow', true),
    ('bedding', 'Bed and duvet set', true),
    ('kitchen', 'Pan', true),
    ('kitchen', 'Kettle', true),
    ('kitchen', 'Plates', true),
    ('kitchen', 'Cutlery', true),
    ('hygiene_baby', 'Soap', true),
    ('hygiene_baby', 'Menstrual materials', true),
    ('hygiene_baby', 'Diapers', true),
    ('food', 'Canned food', true),
    ('food', 'Staples', true),
    ('power_comms', 'Power bank', true),
    ('power_comms', 'Generator', false),
    ('power_comms', 'Phone', true),
    ('other', 'Other (described to the verifier)', true);

-- ---------------------------------------------------------------- needs
CREATE TABLE needs (
    id               bigserial PRIMARY KEY,
    requester_id     bigint NOT NULL REFERENCES users(id),
    need_kind        text NOT NULL DEFAULT 'money' CHECK (need_kind IN ('money', 'item')),
    item_type_id     integer REFERENCES item_types(id),
    quantity_needed  integer CHECK (quantity_needed BETWEEN 1 AND 50),
    specification    text CHECK (specification IS NULL OR length(specification) <= 300),
    used_ok          boolean,
    handover_method  text CHECK (handover_method IN ('public_place', 'courier_post', 'pickup_from_donor', 'other')),
    purpose          text CHECK (purpose IN
        ('rent_housing', 'utilities', 'food', 'medical', 'education',
         'disaster_recovery', 'debt', 'other')),
    medical_location text CHECK (medical_location IN ('turkey', 'abroad')),
    event            text CHECK (event IN ('earthquake', 'flood', 'fire', 'other_disaster')),
    currency         text CHECK (currency IN ('TRY', 'EUR')),
    province         text NOT NULL CHECK (length(province) BETWEEN 2 AND 40),
    district         text NOT NULL CHECK (length(district) BETWEEN 2 AND 40),
    household_size   smallint NOT NULL CHECK (household_size BETWEEN 1 AND 12),         -- private
    goal             integer CHECK (goal >= 100),                              -- in the need's own currency
    description      text NOT NULL CHECK (length(description) BETWEEN 10 AND 1000),     -- private
    contact_method   text NOT NULL DEFAULT 'other' CHECK (contact_method IN ('phone', 'email', 'whatsapp', 'other')),
    contact          text NOT NULL CHECK (length(contact) BETWEEN 3 AND 200),           -- private
    payee_name       text CHECK (length(payee_name) BETWEEN 2 AND 100),        -- private until a pledge is made
    payee_details    text CHECK (length(payee_details) BETWEEN 5 AND 200),     -- private until a pledge is made
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
    -- A CHECK passes when it evaluates to NULL, so the checks above say nothing about item needs
    -- (purpose is NULL there). These two say it outright: each kind has exactly its own fields.
    CONSTRAINT money_needs_are_complete CHECK (need_kind <> 'money' OR
        (purpose IS NOT NULL AND currency IS NOT NULL AND goal IS NOT NULL
         AND payee_name IS NOT NULL AND payee_details IS NOT NULL
         AND item_type_id IS NULL AND quantity_needed IS NULL AND specification IS NULL
         AND used_ok IS NULL AND handover_method IS NULL)),
    CONSTRAINT item_needs_are_complete CHECK (need_kind <> 'item' OR
        (purpose IS NULL AND medical_location IS NULL AND event IS NULL AND currency IS NULL
         AND goal IS NULL AND payee_name IS NULL AND payee_details IS NULL
         AND item_type_id IS NOT NULL AND quantity_needed IS NOT NULL
         AND used_ok IS NOT NULL AND handover_method IS NOT NULL)),
    CONSTRAINT public_needs_are_complete CHECK
        (status IN ('pending', 'rejected', 'withdrawn')
         OR (public_text IS NOT NULL AND (need_kind = 'item' OR payee_verified))),
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
    IF NEW.need_kind = 'item' THEN
        IF NOT EXISTS (SELECT 1 FROM item_types WHERE id = NEW.item_type_id AND enabled) THEN
            RAISE EXCEPTION 'this item type is not available yet';
        END IF;
        RETURN NEW;
    END IF;
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
    IF FOUND AND n.need_kind <> 'money' THEN
        RAISE EXCEPTION 'money pledges are for money needs only';
    END IF;
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
    UPDATE item_claims SET status = 'cancelled' WHERE need_id = NEW.id AND status = 'pledged';
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

-- ---------------------------------------------------------------- item claims
-- The item side of a pledge: a donor claims some units of an item need.
CREATE TABLE item_claims (
    id             bigserial PRIMARY KEY,
    need_id        bigint NOT NULL REFERENCES needs(id),
    donor_id       bigint NOT NULL REFERENCES users(id),
    units          integer NOT NULL CHECK (units >= 1),
    ack            boolean NOT NULL CHECK (ack),     -- donor accepted that the requester will learn the contact
    status         text NOT NULL DEFAULT 'pledged' CHECK (status IN
        ('pledged', 'sent', 'confirmed', 'disputed', 'expired', 'cancelled')),
    contact_method text NOT NULL DEFAULT 'other' CHECK (contact_method IN ('phone', 'email', 'whatsapp', 'other')),
    contact        text NOT NULL CHECK (length(contact) BETWEEN 3 AND 200),   -- verifiers; requester after release
    created_at     timestamptz NOT NULL DEFAULT now(),
    expires_at     timestamptz,
    released_at    timestamptz,                      -- when a verifier passed the contact on
    released_by    bigint REFERENCES users(id),
    sent_at        timestamptz,
    confirmed_at   timestamptz,
    CONSTRAINT release_has_both CHECK ((released_at IS NULL) = (released_by IS NULL))
);
CREATE INDEX item_claims_need ON item_claims (need_id, status);
CREATE INDEX item_claims_donor ON item_claims (donor_id, status);

-- Same idea as check_pledge: one place, under locks, so concurrent claims cannot oversubscribe
-- a need. A total of units cannot be a CHECK (it spans rows), so it is a trigger.
CREATE FUNCTION check_item_claim() RETURNS trigger AS $$
DECLARE
    n            needs%ROWTYPE;
    s            settings%ROWTYPE;
    active_units bigint;
    open_count   bigint;
BEGIN
    SELECT * INTO s FROM settings;
    PERFORM pg_advisory_xact_lock(7002, NEW.donor_id::int);
    SELECT * INTO n FROM needs WHERE id = NEW.need_id FOR UPDATE;
    IF NOT FOUND OR n.status <> 'published' OR n.need_kind <> 'item' THEN
        RAISE EXCEPTION 'need is not open for item claims';
    END IF;
    IF n.requester_id = NEW.donor_id THEN
        RAISE EXCEPTION 'cannot claim your own need';
    END IF;
    SELECT COALESCE(sum(units), 0) INTO active_units FROM item_claims
        WHERE need_id = NEW.need_id AND status IN ('pledged', 'sent', 'confirmed', 'disputed');
    IF active_units + NEW.units > n.quantity_needed THEN
        RAISE EXCEPTION 'claim would exceed the quantity needed';
    END IF;
    SELECT count(*) INTO open_count FROM item_claims
        WHERE donor_id = NEW.donor_id AND status IN ('pledged', 'sent', 'disputed');
    IF open_count >= s.max_unconfirmed_pledges THEN
        RAISE EXCEPTION 'too many unconfirmed claims';
    END IF;
    NEW.status := 'pledged';
    NEW.expires_at := now() + make_interval(days => s.claim_expiry_days);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER item_claims_insert_check BEFORE INSERT ON item_claims
    FOR EACH ROW EXECUTE FUNCTION check_item_claim();

CREATE FUNCTION check_item_claim_transition() RETURNS trigger AS $$
BEGIN
    -- Releasing the contact restarts the clock, so a slow verifier does not use up the donor's days.
    IF OLD.released_at IS NULL AND NEW.released_at IS NOT NULL THEN
        IF OLD.status <> 'pledged' OR NEW.status <> 'pledged' THEN
            RAISE EXCEPTION 'only a pledged claim can be released';
        END IF;
        NEW.expires_at := now() + make_interval(days => (SELECT claim_expiry_days FROM settings));
    END IF;
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;
    IF (OLD.status, NEW.status) IN (
        ('pledged', 'sent'), ('pledged', 'cancelled'), ('pledged', 'expired'),
        ('sent', 'confirmed'), ('sent', 'disputed'),
        ('disputed', 'confirmed'), ('disputed', 'cancelled')) THEN
        IF NEW.status = 'sent' THEN
            IF NEW.released_at IS NULL THEN
                RAISE EXCEPTION 'the verifier has not released the contact yet';
            END IF;
            NEW.sent_at := now();
        END IF;
        IF NEW.status = 'confirmed' THEN NEW.confirmed_at := now(); END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'invalid claim status change: % to %', OLD.status, NEW.status;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER item_claims_status_flow BEFORE UPDATE ON item_claims
    FOR EACH ROW EXECUTE FUNCTION check_item_claim_transition();

-- An item need is fulfilled when confirmed units reach the quantity needed.
CREATE FUNCTION fulfil_if_items_confirmed() RETURNS trigger AS $$
DECLARE
    n    needs%ROWTYPE;
    conf bigint;
BEGIN
    IF NEW.status = 'confirmed' AND OLD.status <> 'confirmed' THEN
        SELECT * INTO n FROM needs WHERE id = NEW.need_id FOR UPDATE;
        IF n.status = 'published' THEN
            SELECT COALESCE(sum(units), 0) INTO conf FROM item_claims
                WHERE need_id = NEW.need_id AND status = 'confirmed';
            IF conf >= n.quantity_needed THEN
                UPDATE needs SET status = 'fulfilled' WHERE id = n.id;
            END IF;
        END IF;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER item_claims_fulfil AFTER UPDATE ON item_claims
    FOR EACH ROW EXECUTE FUNCTION fulfil_if_items_confirmed();

-- Claims nobody handed over expire and free their units.
CREATE FUNCTION expire_item_claims() RETURNS integer AS $$
DECLARE k integer;
BEGIN
    UPDATE item_claims SET status = 'expired' WHERE status = 'pledged' AND expires_at < now();
    GET DIAGNOSTICS k = ROW_COUNT;
    RETURN k;
END;
$$ LANGUAGE plpgsql;


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
       n.need_kind, n.purpose, n.medical_location, n.event, n.province, n.district,
       t.category AS item_category, t.name AS item_name, n.quantity_needed, n.used_ok, n.handover_method,
       n.currency, n.goal, b.code AS band, n.public_text,
       COALESCE(p.promised, 0)  AS promised,
       COALESCE(p.confirmed, 0) AS confirmed,
       COALESCE(p.donors, 0)    AS donors,
       COALESCE(ic.promised_units, 0)  AS promised_units,
       COALESCE(ic.confirmed_units, 0) AS confirmed_units,
       COALESCE(ic.claimants, 0)       AS claimants
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
LEFT JOIN item_types t ON t.id = n.item_type_id
LEFT JOIN (
    SELECT need_id,
           COALESCE(sum(units), 0) AS promised_units,
           COALESCE(sum(units) FILTER (WHERE status = 'confirmed'), 0) AS confirmed_units,
           count(DISTINCT donor_id) AS claimants
    FROM item_claims
    WHERE status IN ('pledged', 'sent', 'confirmed', 'disputed')
    GROUP BY need_id) ic ON ic.need_id = n.id
WHERE n.status = 'published';

-- Front-page totals: counts and sums over the whole platform, nothing about any one need or person.
CREATE VIEW public_stats AS
SELECT (SELECT count(*) FROM needs WHERE status = 'published')                         AS open_needs,
       (SELECT count(*) FROM needs WHERE status = 'fulfilled')                         AS fulfilled_needs,
       COALESCE((SELECT sum(amount_try) FROM pledges WHERE status = 'confirmed'), 0)   AS confirmed_try,
       (SELECT count(DISTINCT supporter_id) FROM pledges WHERE status = 'confirmed')   AS donors,
       (SELECT count(*) FROM needs WHERE status = 'fulfilled' AND need_kind = 'item')  AS fulfilled_items,
       COALESCE((SELECT sum(units) FROM item_claims WHERE status = 'confirmed'), 0)   AS confirmed_units,
       (SELECT count(DISTINCT donor_id) FROM item_claims WHERE status = 'confirmed')  AS claimants;

-- What analytics may read: verified needs, safe columns only. The requester id, cast to
-- text, is the person key for the per-person cap. Nothing private is listed here.
CREATE VIEW needs_analytics AS
SELECT n.id, n.requester_id::text AS person_id, n.purpose, n.medical_location, n.event,
       n.province, n.household_size, n.currency, n.goal, b.code AS band, n.status, n.created_at,
       n.need_kind::text AS kind, it.category AS item_category
FROM needs n
LEFT JOIN item_types it ON it.id = n.item_type_id
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
GRANT INSERT (requester_id, need_kind, item_type_id, quantity_needed, specification, used_ok,
              handover_method, purpose, medical_location, event, currency, province, district,
              household_size, goal, description, contact_method, contact,
              payee_name, payee_details) ON needs TO dp_app;
GRANT UPDATE (status, public_text, payee_verified, reviewed_by, reviewed_at, review_note,
              withdrawn_reason) ON needs TO dp_app;
GRANT INSERT (need_id, supporter_id, amount_try, ack, contact_method, contact) ON pledges TO dp_app;
GRANT UPDATE (status) ON pledges TO dp_app;
GRANT INSERT ON need_events TO dp_app;
GRANT USAGE ON SEQUENCE needs_id_seq, pledges_id_seq, need_events_id_seq TO dp_app;
GRANT EXECUTE ON FUNCTION expire_pledges() TO dp_app;
GRANT EXECUTE ON FUNCTION expire_item_claims() TO dp_app;
GRANT SELECT ON item_types, item_claims TO dp_app;
GRANT INSERT (need_id, donor_id, units, ack, contact_method, contact) ON item_claims TO dp_app;
GRANT UPDATE (status, released_at, released_by) ON item_claims TO dp_app;
GRANT USAGE ON SEQUENCE item_claims_id_seq TO dp_app;
GRANT EXECUTE ON FUNCTION expire_item_claims() TO dp_app;
REVOKE DELETE, TRUNCATE ON item_claims FROM dp_app;
REVOKE DELETE, TRUNCATE ON needs, pledges FROM dp_app;
REVOKE UPDATE, DELETE, TRUNCATE ON need_events FROM dp_app;

GRANT SELECT ON public_needs, public_stats, needs_analytics, synth_analytics, suggestion_stats TO dp_app;
GRANT INSERT ON needs_synth TO dp_app;
GRANT USAGE ON SEQUENCE needs_synth_id_seq TO dp_app;
REVOKE UPDATE, DELETE, TRUNCATE ON needs_synth FROM dp_app;

-- One privacy budget per analytics dataset
INSERT INTO datasets (name, epsilon_total) VALUES ('needs_synthetic', 10.0), ('needs_live', 10.0);

-- ---------------------------------------------------------------- pledge reference code
-- Every money pledge gets a random code the donor puts in the transfer description, so the
-- requester can match a bank line to a pledge. The database sets it; the app role cannot
-- insert or change it. It is a matching aid, not proof of payment.
-- BEGIN ref code
ALTER TABLE pledges ADD COLUMN ref_code text;

CREATE FUNCTION new_ref_code() RETURNS text AS $$
DECLARE
    alphabet text := 'ABCDEFGHJKMNPQRSTUVWXYZ23456789';
    raw text;
    code text;
    i integer;
BEGIN
    LOOP
        raw := '';
        FOR i IN 1..8 LOOP
            raw := raw || substr(alphabet, 1 + floor(random() * length(alphabet))::integer, 1);
        END LOOP;
        code := 'AB-' || substr(raw, 1, 4) || '-' || substr(raw, 5, 4);
        EXIT WHEN NOT EXISTS (SELECT 1 FROM pledges WHERE ref_code = code);
    END LOOP;
    RETURN code;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION set_pledge_ref() RETURNS trigger AS $$
BEGIN
    NEW.ref_code := new_ref_code();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER pledges_ref_code BEFORE INSERT ON pledges
    FOR EACH ROW EXECUTE FUNCTION set_pledge_ref();

ALTER TABLE pledges DISABLE TRIGGER pledges_status_flow;
ALTER TABLE pledges DISABLE TRIGGER pledges_fulfil;
UPDATE pledges SET ref_code = new_ref_code() WHERE ref_code IS NULL;
ALTER TABLE pledges ENABLE TRIGGER pledges_status_flow;
ALTER TABLE pledges ENABLE TRIGGER pledges_fulfil;

ALTER TABLE pledges ALTER COLUMN ref_code SET NOT NULL;
ALTER TABLE pledges ADD CONSTRAINT pledges_ref_code_key UNIQUE (ref_code);
-- END ref code

-- ---------------------------------------------------------------- notifications
-- In-app notices. Only the database writes them (security definer functions fired by status changes), so
-- the app role cannot forge or rewrite one: it can read rows and set read_at, nothing else. The text is
-- built from fixed templates plus amounts, ids and the reference code. It never carries a name or contact.
-- BEGIN notifications
CREATE TABLE notifications (
    id         bigserial PRIMARY KEY,
    user_id    bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    message    text NOT NULL CHECK (length(message) BETWEEN 1 AND 300),
    link       text CHECK (link IS NULL OR link ~ '^/[A-Za-z0-9/_-]*$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    read_at    timestamptz
);
CREATE INDEX notifications_user ON notifications (user_id, id DESC);
CREATE INDEX notifications_unread ON notifications (user_id) WHERE read_at IS NULL;

CREATE FUNCTION add_notification(uid bigint, msg text, lnk text) RETURNS void AS $$
BEGIN
    INSERT INTO notifications (user_id, message, link) VALUES (uid, msg, lnk);
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public;
REVOKE EXECUTE ON FUNCTION add_notification(bigint, text, text) FROM PUBLIC;

CREATE FUNCTION notify_pledge() RETURNS trigger AS $$
DECLARE
    n   needs%ROWTYPE;
    amt text := NEW.amount_try::text || ' TRY';
    ref text := ' (reference ' || NEW.ref_code || ')';
BEGIN
    IF TG_OP = 'INSERT' THEN
        PERFORM add_notification(NEW.supporter_id, 'Your pledge of ' || amt || ' is now in progress. Send the money to the account shown, put reference code ' || NEW.ref_code || ' in the transfer description, then mark the pledge as sent.', '/support');
        RETURN NULL;
    END IF;
    IF NEW.status = OLD.status THEN
        RETURN NULL;
    END IF;
    SELECT * INTO n FROM needs WHERE id = NEW.need_id;
    IF OLD.status = 'pledged' AND NEW.status = 'sent' THEN
        PERFORM add_notification(n.requester_id, 'A donor says they sent ' || amt || ref || ' to your account. Open your page to answer.', '/needs/mine');
    ELSIF OLD.status = 'sent' AND NEW.status = 'confirmed' THEN
        PERFORM add_notification(NEW.supporter_id, 'Your pledge of ' || amt || ref || ' was confirmed as received. Thank you.', '/support');
    ELSIF OLD.status = 'sent' AND NEW.status = 'disputed' THEN
        PERFORM add_notification(NEW.supporter_id, 'The requester says your pledge of ' || amt || ref || ' did not arrive. A verifier may contact you. Keep your proof of transfer.', '/support');
    ELSIF OLD.status = 'disputed' AND NEW.status = 'confirmed' THEN
        PERFORM add_notification(NEW.supporter_id, 'A verifier settled the dispute: your pledge of ' || amt || ref || ' counts as received. Thank you.', '/support');
        PERFORM add_notification(n.requester_id, 'A verifier settled the dispute on a pledge of ' || amt || ref || ': it counts as received.', '/needs/mine');
    ELSIF OLD.status = 'disputed' AND NEW.status = 'cancelled' THEN
        PERFORM add_notification(NEW.supporter_id, 'A verifier settled the dispute: your pledge of ' || amt || ref || ' counts as not received and is cancelled.', '/support');
        PERFORM add_notification(n.requester_id, 'A verifier settled the dispute on a pledge of ' || amt || ref || ': it counts as not received and the amount is open again.', '/needs/mine');
    ELSIF OLD.status = 'pledged' AND NEW.status = 'expired' THEN
        PERFORM add_notification(NEW.supporter_id, 'Your pledge of ' || amt || ref || ' expired because it was not marked as sent in time. The amount is open again.', '/support');
    ELSIF OLD.status = 'pledged' AND NEW.status = 'cancelled' AND n.status = 'withdrawn' THEN
        PERFORM add_notification(NEW.supporter_id, 'Your pledge of ' || amt || ref || ' was cancelled because the requester withdrew the request.', '/support');
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public;
CREATE TRIGGER pledges_notify AFTER INSERT OR UPDATE ON pledges
    FOR EACH ROW EXECUTE FUNCTION notify_pledge();

CREATE FUNCTION notify_item_claim() RETURNS trigger AS $$
DECLARE
    n     needs%ROWTYPE;
    iname text;
    what  text;
BEGIN
    SELECT * INTO n FROM needs WHERE id = NEW.need_id;
    SELECT name INTO iname FROM item_types WHERE id = n.item_type_id;
    what := NEW.units::text || CASE WHEN NEW.units = 1 THEN ' unit' ELSE ' units' END || ' of ' || COALESCE(iname, 'items');
    IF TG_OP = 'INSERT' THEN
        PERFORM add_notification(NEW.donor_id, 'Your offer of ' || what || ' is recorded. A verifier will pass your contact to the requester, who will then get in touch.', '/support');
        RETURN NULL;
    END IF;
    IF OLD.released_at IS NULL AND NEW.released_at IS NOT NULL THEN
        PERFORM add_notification(n.requester_id, 'A donor offered ' || what || ' for your request #' || n.id || '. Their contact is on your page: agree the handover with them.', '/needs/mine');
        PERFORM add_notification(NEW.donor_id, 'A verifier passed your contact to the requester. Agree the handover with them, then mark it as handed over.', '/support');
    END IF;
    IF NEW.status = OLD.status THEN
        RETURN NULL;
    END IF;
    IF OLD.status = 'pledged' AND NEW.status = 'sent' THEN
        PERFORM add_notification(n.requester_id, 'A donor says they handed over ' || what || '. Say whether you received them.', '/needs/mine');
    ELSIF OLD.status = 'sent' AND NEW.status = 'confirmed' THEN
        PERFORM add_notification(NEW.donor_id, 'The requester confirmed that they received ' || what || '. Thank you.', '/support');
    ELSIF OLD.status = 'sent' AND NEW.status = 'disputed' THEN
        PERFORM add_notification(NEW.donor_id, 'The requester says they did not receive ' || what || '. A verifier may contact you.', '/support');
    ELSIF OLD.status = 'disputed' AND NEW.status = 'confirmed' THEN
        PERFORM add_notification(NEW.donor_id, 'A verifier settled the dispute: your handover of ' || what || ' counts as received. Thank you.', '/support');
        PERFORM add_notification(n.requester_id, 'A verifier settled the dispute on ' || what || ': it counts as received.', '/needs/mine');
    ELSIF OLD.status = 'disputed' AND NEW.status = 'cancelled' THEN
        PERFORM add_notification(NEW.donor_id, 'A verifier settled the dispute: your handover of ' || what || ' counts as not received and is cancelled.', '/support');
        PERFORM add_notification(n.requester_id, 'A verifier settled the dispute on ' || what || ': it counts as not received and the units are open again.', '/needs/mine');
    ELSIF OLD.status = 'pledged' AND NEW.status = 'expired' THEN
        PERFORM add_notification(NEW.donor_id, 'Your offer of ' || what || ' expired because it was not handed over in time. The units are open again.', '/support');
    ELSIF OLD.status = 'pledged' AND NEW.status = 'cancelled' AND n.status = 'withdrawn' THEN
        PERFORM add_notification(NEW.donor_id, 'Your offer of ' || what || ' was cancelled because the requester withdrew the request.', '/support');
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public;
CREATE TRIGGER item_claims_notify AFTER INSERT OR UPDATE ON item_claims
    FOR EACH ROW EXECUTE FUNCTION notify_item_claim();

CREATE FUNCTION notify_need() RETURNS trigger AS $$
BEGIN
    IF OLD.status = 'pending' AND NEW.status = 'published' THEN
        PERFORM add_notification(NEW.requester_id, 'Your request #' || NEW.id || ' was approved and is now on the public board.', '/needs/mine');
    ELSIF OLD.status = 'pending' AND NEW.status = 'rejected' THEN
        PERFORM add_notification(NEW.requester_id, 'Your request #' || NEW.id || ' was not approved. Open your page to see the reason from the verifier.', '/needs/mine');
    ELSIF OLD.status = 'published' AND NEW.status = 'fulfilled' THEN
        PERFORM add_notification(NEW.requester_id, 'Your request #' || NEW.id || ' is fully covered.', '/needs/mine');
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public;
CREATE TRIGGER needs_notify AFTER UPDATE OF status ON needs
    FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM NEW.status)
    EXECUTE FUNCTION notify_need();

GRANT SELECT ON notifications TO dp_app;
GRANT UPDATE (read_at) ON notifications TO dp_app;
REVOKE INSERT, DELETE, TRUNCATE ON notifications FROM dp_app;
-- END notifications

-- ---------------------------------------------------------------- declined item offers
-- The verifier's decline route writes a claim_declined event whose note starts with the claim id. The donor
-- gets a fixed notice; the reason stays on their page.
-- BEGIN decline notice
CREATE FUNCTION notify_claim_declined() RETURNS trigger AS $$
DECLARE
    cid bigint;
    k   item_claims%ROWTYPE;
BEGIN
    cid := substring(NEW.note from '^claim ([0-9]+):')::bigint;
    IF cid IS NOT NULL THEN
        SELECT * INTO k FROM item_claims WHERE id = cid;
        IF FOUND THEN
            PERFORM add_notification(k.donor_id, 'A verifier declined your item offer. Open your page to see the reason.', '/support');
        END IF;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public;
CREATE TRIGGER need_events_notify AFTER INSERT ON need_events
    FOR EACH ROW WHEN (NEW.event = 'claim_declined')
    EXECUTE FUNCTION notify_claim_declined();
-- END decline notice

-- ---------------------------------------------------------------- account deletion
-- Deleting an account anonymizes it in place. The rows other people's records and the statistics depend on
-- stay (needs, pledges, offers, events), but everything private is overwritten and the login stops working.
-- The app role cannot update users; it can only call these two functions. account_blockers lists what must be
-- finished first, and delete_account refuses on the same list, so the page and the rule cannot drift apart.
-- BEGIN account deletion
ALTER TABLE users ADD COLUMN deleted_at timestamptz;

CREATE FUNCTION account_blockers(uid bigint) RETURNS TABLE (blocker text, cnt bigint) AS $$
    SELECT 'pending_needs', count(*) FROM needs WHERE requester_id = uid AND status = 'pending'
    UNION ALL
    SELECT 'published_needs', count(*) FROM needs WHERE requester_id = uid AND status = 'published'
    UNION ALL
    SELECT 'unanswered_pledges_on_mine', count(*) FROM pledges p JOIN needs n ON n.id = p.need_id
        WHERE n.requester_id = uid AND p.status IN ('pledged', 'sent', 'disputed')
    UNION ALL
    SELECT 'unanswered_offers_on_mine', count(*) FROM item_claims k JOIN needs n ON n.id = k.need_id
        WHERE n.requester_id = uid AND k.status IN ('pledged', 'sent', 'disputed')
    UNION ALL
    SELECT 'my_open_pledges', count(*) FROM pledges WHERE supporter_id = uid AND status IN ('pledged', 'sent', 'disputed')
    UNION ALL
    SELECT 'my_open_offers', count(*) FROM item_claims WHERE donor_id = uid AND status IN ('pledged', 'sent', 'disputed');
$$ LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public;

CREATE FUNCTION delete_account(uid bigint) RETURNS boolean AS $$
DECLARE
    u users%ROWTYPE;
BEGIN
    SELECT * INTO u FROM users WHERE id = uid FOR UPDATE;
    IF NOT FOUND OR u.deleted_at IS NOT NULL OR u.role NOT IN ('requester', 'supporter') THEN
        RETURN false;
    END IF;
    IF EXISTS (SELECT 1 FROM account_blockers(uid) WHERE cnt > 0) THEN
        RETURN false;
    END IF;
    UPDATE needs SET
        description = 'Removed with the account.',
        contact = 'removed',
        payee_name = CASE WHEN payee_name IS NULL THEN NULL ELSE 'Removed' END,
        payee_details = CASE WHEN payee_details IS NULL THEN NULL ELSE 'removed' END,
        public_text = CASE WHEN public_text IS NULL THEN NULL ELSE 'This request is closed.' END,
        withdrawn_reason = CASE WHEN withdrawn_reason IS NULL THEN NULL ELSE 'Removed with the account.' END,
        review_note = NULL,
        specification = NULL
    WHERE requester_id = uid;
    UPDATE pledges SET contact = NULL WHERE supporter_id = uid;
    UPDATE item_claims SET contact = 'removed' WHERE donor_id = uid;
    UPDATE need_events SET note = CASE
        WHEN note ~ '^(pledge|claim) [0-9]+: ' THEN regexp_replace(note, '^((pledge|claim) [0-9]+): .*$', '\1: removed')
        WHEN note ~ '^(pledge|claim) [0-9]+$' THEN note
        ELSE NULL END
        WHERE actor_id = uid AND note IS NOT NULL;
    DELETE FROM notifications WHERE user_id = uid;
    UPDATE users SET username = 'deleted user #' || uid, password_hash = '!', deleted_at = now() WHERE id = uid;
    RETURN true;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public;

REVOKE EXECUTE ON FUNCTION account_blockers(bigint), delete_account(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION account_blockers(bigint), delete_account(bigint) TO dp_app;
-- END account deletion
