-- ============================================================
-- GetAutomatik — Supabase migrations
-- Esegui nel SQL Editor di Supabase (una volta sola)
-- ============================================================

-- 1. CHAT_SESSIONS: colonne per qualifica immobiliare completa
ALTER TABLE chat_sessions
  ADD COLUMN IF NOT EXISTS lead_budget TEXT,
  ADD COLUMN IF NOT EXISTS lead_zone TEXT,
  ADD COLUMN IF NOT EXISTS lead_appointment TEXT,
  ADD COLUMN IF NOT EXISTS reminder_sent BOOLEAN DEFAULT FALSE;

-- 2. CLIENT_CONFIGS: colonne per configurazione immobiliare
ALTER TABLE client_configs
  ADD COLUMN IF NOT EXISTS slot_visita TEXT,
  ADD COLUMN IF NOT EXISTS citta TEXT,
  ADD COLUMN IF NOT EXISTS tipo_attivita TEXT,
  ADD COLUMN IF NOT EXISTS active BOOLEAN DEFAULT TRUE;

-- 3. Crea la tabella chat_sessions se non esiste
CREATE TABLE IF NOT EXISTS chat_sessions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_config_id UUID REFERENCES client_configs(id),
  lead_name TEXT,
  lead_phone TEXT,
  lead_type TEXT,
  lead_budget TEXT,
  lead_zone TEXT,
  lead_appointment TEXT,
  messages JSONB,
  qualified BOOLEAN DEFAULT FALSE,
  reminder_sent BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 4. Crea la tabella client_configs se non esiste
CREATE TABLE IF NOT EXISTS client_configs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  nome_azienda TEXT NOT NULL,
  email_titolare TEXT NOT NULL,
  settore TEXT,
  tipo_attivita TEXT,
  citta TEXT,
  slot_visita TEXT,
  forwarding_address TEXT,
  chat_token TEXT UNIQUE,
  active BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 5. Indice per lookup veloce per chat_token
CREATE INDEX IF NOT EXISTS idx_client_configs_chat_token ON client_configs(chat_token);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_created ON chat_sessions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_appointment ON chat_sessions(lead_appointment) WHERE lead_appointment IS NOT NULL;
