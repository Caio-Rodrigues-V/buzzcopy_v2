# Social Monitor — MVP Backend

API Flask para monitoramento e análise de sentimento de perfis políticos no YouTube.

---

## Setup Local

```bash
# 1. Instalar dependências
pip install -r requirements.txt

# 2. Criar .env a partir do exemplo
cp .env.example .env
# Preencher as chaves no .env

# 3. Rodar o banco (SQL Editor do Supabase)
# Colar e executar o conteúdo de schema.sql

# 4. Iniciar a API
python app.py
```

---

## Obter as chaves

### YouTube Data API
1. Acesse https://console.cloud.google.com
2. Crie um projeto novo
3. Habilite "YouTube Data API v3"
4. Credenciais → Criar chave de API → copie para `YOUTUBE_API_KEY`

### Anthropic (Claude)
1. https://console.anthropic.com
2. API Keys → Create Key → copie para `ANTHROPIC_API_KEY`

### Supabase
1. https://supabase.com → New Project
2. Settings → API → copie `URL` e `anon public key`
3. Execute o `schema.sql` no SQL Editor

---

## Endpoints

| Método | Rota | Descrição |
|--------|------|-----------|
| GET | `/` | Health check |
| GET | `/profiles` | Lista perfis monitorados |
| POST | `/profiles` | Adiciona perfil |
| DELETE | `/profiles/:id` | Remove perfil |
| POST | `/collect/youtube/:channel_id` | Coleta dados do canal |
| POST | `/analyze/youtube/:channel_id` | Coleta + analisa sentimento |
| GET | `/reports/:channel_id` | Histórico de relatórios |
| GET | `/reports/latest` | Último relatório de cada canal |
| GET | `/snapshots/:channel_id` | Histórico de métricas |

### Exemplo de uso

```bash
# Adicionar um político para monitorar
curl -X POST http://localhost:5000/profiles \
  -H "Content-Type: application/json" \
  -d '{"platform":"youtube","platform_id":"UCxxxxxx","name":"Nome do Político"}'

# Coletar + analisar (Fase 1 MVP)
curl -X POST "http://localhost:5000/analyze/youtube/UCxxxxxx?name=Nome%20do%20Político&days=30"
```

---

## Deploy no Render.com

1. Suba o projeto para um repositório GitHub
2. Render → New Web Service → conecte o repo
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120`
5. Adicione as variáveis de ambiente no painel do Render

---

## N8n — Agendamento

No N8n, crie um workflow com:
1. **Trigger**: Schedule (diário, ex: 06:00)
2. **HTTP Request**: `POST /analyze/youtube/{channel_id}?days=7&name={nome}`
3. **IF**: `crisis_alert == true` → alerta no WhatsApp
4. **Supabase Node** (opcional): leitura adicional dos dados

---

## Próximas fases

- **Fase 2**: Instagram via Apify (`POST /collect/instagram/:username`)
- **Fase 3**: Twitter/X via Apify
- **Fase 4**: Dashboard React + autenticação multi-cliente
