
-- How a verifier can reach the requester (private; shown to verifiers only)
ALTER TABLE needs ADD COLUMN contact_method text NOT NULL DEFAULT 'other'
    CHECK (contact_method IN ('phone', 'email', 'whatsapp', 'other'));