-- ============================================================
-- Social Monitor — Schema Supabase
-- Rodar no SQL Editor do painel Supabase
-- ============================================================

-- Extensão para UUID automático (já vem ativa no Supabase)
CREATE EXTENSION IF NOT EXISTS "pgcrypto";


-- ── PERFIS MONITORADOS ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS profiles (
  id          UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
  platform    TEXT        NOT NULL CHECK (platform IN ('youtube','instagram','twitter','facebook','tiktok')),
  platform_id TEXT        NOT NULL,
  name        TEXT        NOT NULL,
  active      BOOLEAN     DEFAULT TRUE,
  created_at  TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (platform, platform_id)
);


-- ── SNAPSHOTS DO CANAL (métricas ao longo do tempo) ──────────────────────────
CREATE TABLE IF NOT EXISTS channel_snapshots (
  id               UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
  channel_id       TEXT        NOT NULL,
  name             TEXT,
  subscriber_count BIGINT,
  video_count      INT,
  total_views      BIGINT,
  engagement_rate  FLOAT,
  collected_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_snapshots_channel ON channel_snapshots (channel_id, collected_at DESC);


-- ── VÍDEOS ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS videos (
  id            UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
  channel_id    TEXT        NOT NULL,
  video_id      TEXT        UNIQUE NOT NULL,
  title         TEXT,
  published_at  TIMESTAMPTZ,
  view_count    BIGINT      DEFAULT 0,
  like_count    BIGINT      DEFAULT 0,
  comment_count INT         DEFAULT 0,
  url           TEXT,
  thumbnail     TEXT,
  collected_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos (channel_id, published_at DESC);


-- ── RELATÓRIOS DE ANÁLISE ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS analysis_reports (
  id                  UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
  channel_id          TEXT        NOT NULL,
  profile_name        TEXT,
  period_days         INT         DEFAULT 30,
  comments_analyzed   INT         DEFAULT 0,

  -- Sentimento (percentuais)
  positive_pct        FLOAT,
  negative_pct        FLOAT,
  neutral_pct         FLOAT,
  overall_score       FLOAT,      -- -1.0 a 1.0

  -- Alertas
  crisis_alert        BOOLEAN     DEFAULT FALSE,
  crisis_reason       TEXT,

  -- Conteúdo qualitativo
  main_themes         JSONB,      -- ["tema1", "tema2"]
  top_positive_quote  TEXT,
  top_negative_quote  TEXT,
  narrative           TEXT,       -- resumo executivo

  -- Métricas do canal no período
  channel_metrics     JSONB,

  created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_reports_channel    ON analysis_reports (channel_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reports_crisis     ON analysis_reports (crisis_alert) WHERE crisis_alert = TRUE;
CREATE INDEX IF NOT EXISTS idx_reports_score      ON analysis_reports (overall_score);


-- ── VIEW: último relatório por canal ─────────────────────────────────────────
CREATE OR REPLACE VIEW latest_reports AS
SELECT DISTINCT ON (channel_id) *
FROM analysis_reports
ORDER BY channel_id, created_at DESC;


-- ── RLS (segurança básica — ajuste conforme necessidade) ─────────────────────
ALTER TABLE profiles          ENABLE ROW LEVEL SECURITY;
ALTER TABLE channel_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE videos            ENABLE ROW LEVEL SECURITY;
ALTER TABLE analysis_reports  ENABLE ROW LEVEL SECURITY;

-- Permite leitura e escrita com a service key (usada pelo Flask)
CREATE POLICY "allow_all_service" ON profiles          FOR ALL USING (true);
CREATE POLICY "allow_all_service" ON channel_snapshots FOR ALL USING (true);
CREATE POLICY "allow_all_service" ON videos            FOR ALL USING (true);
CREATE POLICY "allow_all_service" ON analysis_reports  FOR ALL USING (true);
