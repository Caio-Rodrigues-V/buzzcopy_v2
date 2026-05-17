-- ============================================================
-- Pulse — Schema v3 (multi-handle unificado)
-- BREAKING CHANGE: dropa tudo do v1/v2. Sem migração de dados.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ── DROP DO LEGADO ───────────────────────────────────────────
DROP VIEW  IF EXISTS latest_reports          CASCADE;
DROP TABLE IF EXISTS war_room_responses      CASCADE;
DROP TABLE IF EXISTS simulacoes              CASCADE;
DROP TABLE IF EXISTS analysis_reports        CASCADE;
DROP TABLE IF EXISTS social_comments         CASCADE;
DROP TABLE IF EXISTS social_posts            CASCADE;
DROP TABLE IF EXISTS instagram_comments      CASCADE;
DROP TABLE IF EXISTS instagram_posts         CASCADE;
DROP TABLE IF EXISTS videos                  CASCADE;
DROP TABLE IF EXISTS channel_snapshots       CASCADE;
DROP TABLE IF EXISTS profiles                CASCADE;
DROP TABLE IF EXISTS users                   CASCADE;


-- ── USERS ────────────────────────────────────────────────────
CREATE TABLE users (
  id            UUID         DEFAULT gen_random_uuid() PRIMARY KEY,
  email         TEXT         UNIQUE NOT NULL,
  password_hash TEXT         NOT NULL,
  name          TEXT,
  role          TEXT         NOT NULL DEFAULT 'client' CHECK (role IN ('admin','client')),
  active        BOOLEAN      DEFAULT TRUE,
  last_login    TIMESTAMPTZ,
  created_at    TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX idx_users_email ON users (email) WHERE active = TRUE;


-- ── PROFILES (1 alvo = N handles) ────────────────────────────
CREATE TABLE profiles (
  id          UUID         DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id     UUID         REFERENCES users(id) ON DELETE CASCADE,
  name        TEXT         NOT NULL,
  handles     JSONB        NOT NULL DEFAULT '{}'::jsonb,
  active      BOOLEAN      DEFAULT TRUE,
  created_at  TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX idx_profiles_user ON profiles (user_id);
CREATE INDEX idx_profiles_handles ON profiles USING GIN (handles);


-- ── SOCIAL POSTS (unificado) ─────────────────────────────────
-- IDs são TEXT pq os collectors usam prefixos: yt_xxx, tw_xxx, instagram raw
CREATE TABLE social_posts (
  id              TEXT         PRIMARY KEY,
  profile_id      UUID         NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  platform        TEXT         NOT NULL CHECK (platform IN ('instagram','twitter','youtube')),
  external_id     TEXT,
  author_username TEXT,
  author_name     TEXT,
  content         TEXT,
  post_type       TEXT         DEFAULT 'post',
  url             TEXT,
  source_domain   TEXT,
  metrics         JSONB        DEFAULT '{}'::jsonb,
  hashtags        JSONB        DEFAULT '[]'::jsonb,
  posted_at       TIMESTAMPTZ,
  created_at      TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX idx_posts_profile_platform ON social_posts (profile_id, platform, posted_at DESC);
CREATE INDEX idx_posts_platform_author  ON social_posts (platform, author_username);


-- ── SOCIAL COMMENTS (unificado) ──────────────────────────────
CREATE TABLE social_comments (
  id              TEXT         PRIMARY KEY,
  post_id         TEXT         REFERENCES social_posts(id) ON DELETE SET NULL,
  profile_id      UUID         NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  platform        TEXT         NOT NULL CHECK (platform IN ('instagram','twitter','youtube')),
  external_id     TEXT,
  author_username TEXT,
  content         TEXT,
  sentiment       TEXT         CHECK (sentiment IN ('positive','negative','neutral')),
  metrics         JSONB        DEFAULT '{}'::jsonb,
  reply_to_id     TEXT,
  posted_at       TIMESTAMPTZ,
  created_at      TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX idx_comments_profile_posted  ON social_comments (profile_id, posted_at DESC);
CREATE INDEX idx_comments_platform        ON social_comments (profile_id, platform);
CREATE INDEX idx_comments_sentiment       ON social_comments (profile_id, sentiment) WHERE sentiment IS NOT NULL;


-- ── ANALYSIS REPORTS (cross-platform) ────────────────────────
CREATE TABLE analysis_reports (
  id                  UUID         DEFAULT gen_random_uuid() PRIMARY KEY,
  profile_id          UUID         NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  platforms_analyzed  JSONB,
  comments_analyzed   INT          DEFAULT 0,
  positive_pct        FLOAT,
  negative_pct        FLOAT,
  neutral_pct         FLOAT,
  overall_score       FLOAT,
  crisis_alert        BOOLEAN      DEFAULT FALSE,
  crisis_reason       TEXT,
  main_themes         JSONB,
  top_positive_quote  TEXT,
  top_negative_quote  TEXT,
  narrative           TEXT,
  by_platform         JSONB,
  created_at          TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX idx_reports_profile  ON analysis_reports (profile_id, created_at DESC);
CREATE INDEX idx_reports_crisis   ON analysis_reports (crisis_alert) WHERE crisis_alert = TRUE;


-- ── SIMULACOES ───────────────────────────────────────────────
CREATE TABLE simulacoes (
  id          UUID         DEFAULT gen_random_uuid() PRIMARY KEY,
  profile_id  UUID         REFERENCES profiles(id) ON DELETE CASCADE,
  user_id     UUID         REFERENCES users(id) ON DELETE CASCADE,
  conteudo    TEXT         NOT NULL,
  n_agentes   INT          DEFAULT 100,
  filtros     JSONB,
  contexto    TEXT,
  forecast    JSONB        NOT NULL,
  created_at  TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX idx_sim_profile ON simulacoes (profile_id, created_at DESC);
CREATE INDEX idx_sim_user    ON simulacoes (user_id, created_at DESC);


-- ── WAR ROOM RESPONSES ───────────────────────────────────────
CREATE TABLE war_room_responses (
  id              UUID         DEFAULT gen_random_uuid() PRIMARY KEY,
  profile_id      UUID         REFERENCES profiles(id) ON DELETE CASCADE,
  user_id         UUID         REFERENCES users(id) ON DELETE CASCADE,
  attack_content  TEXT         NOT NULL,
  response_text   TEXT         NOT NULL,
  strategy        TEXT         CHECK (strategy IN ('defensiva','ofensiva','desvio')),
  simulation_data JSONB,
  created_at      TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX idx_warroom_profile ON war_room_responses (profile_id, created_at DESC);


-- ── RLS ──────────────────────────────────────────────────────
-- Service key (Flask) bypassa RLS, mas mantém ativo pra segurança
ALTER TABLE users               ENABLE ROW LEVEL SECURITY;
ALTER TABLE profiles            ENABLE ROW LEVEL SECURITY;
ALTER TABLE social_posts        ENABLE ROW LEVEL SECURITY;
ALTER TABLE social_comments     ENABLE ROW LEVEL SECURITY;
ALTER TABLE analysis_reports    ENABLE ROW LEVEL SECURITY;
ALTER TABLE simulacoes          ENABLE ROW LEVEL SECURITY;
ALTER TABLE war_room_responses  ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_all" ON users              FOR ALL USING (true);
CREATE POLICY "service_all" ON profiles           FOR ALL USING (true);
CREATE POLICY "service_all" ON social_posts       FOR ALL USING (true);
CREATE POLICY "service_all" ON social_comments    FOR ALL USING (true);
CREATE POLICY "service_all" ON analysis_reports   FOR ALL USING (true);
CREATE POLICY "service_all" ON simulacoes         FOR ALL USING (true);
CREATE POLICY "service_all" ON war_room_responses FOR ALL USING (true);