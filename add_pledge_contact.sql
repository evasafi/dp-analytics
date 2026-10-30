
-- How a verifier can reach the supporter who pledged (private; shown to verifiers only)
ALTER TABLE pledges ADD COLUMN contact_method text NOT NULL DEFAULT 'other'
    CHECK (contact_method IN ('phone', 'email', 'whatsapp', 'other'));
ALTER TABLE pledges ADD COLUMN contact text
    CHECK (contact IS NULL OR length(contact) BETWEEN 3 AND 200);