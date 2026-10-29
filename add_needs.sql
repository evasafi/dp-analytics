
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