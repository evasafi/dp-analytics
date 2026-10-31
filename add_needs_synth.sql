
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