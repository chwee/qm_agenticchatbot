-- Migration: add whatsapp_id and phone_confirmed columns to leads.
-- Safe to run more than once (uses IF NOT EXISTS / DO block).

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'leads' AND column_name = 'whatsapp_id'
    ) THEN
        ALTER TABLE leads ADD COLUMN whatsapp_id TEXT UNIQUE;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'leads' AND column_name = 'phone_confirmed'
    ) THEN
        ALTER TABLE leads ADD COLUMN phone_confirmed BOOLEAN NOT NULL DEFAULT FALSE;
    END IF;
END$$;

-- Mark existing leads (captured before this migration) as confirmed —
-- they came from @c.us accounts where the phone IS the real number.
UPDATE leads SET phone_confirmed = TRUE WHERE phone_confirmed = FALSE;
