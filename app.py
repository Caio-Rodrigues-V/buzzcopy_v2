"""
app.py — API Flask do Social Monitor MVP
Endpoints chamados pelo N8n para coletar e analisar perfis políticos
"""
import os
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS
from supabase import create_client, Client

from collector import YouTubeCollector
from analyzer import SentimentAnalyzer
from pulse_simulator import simular

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

print("SUPABASE_URL =", os.getenv("SUPABASE_URL"))
print("SUPABASE_KEY carregada =", bool(os.getenv("SUPABASE_KEY")))

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})


# ── CLIENTES ──────────────────────────────────────────────────────────────────

def get_youtube() -> YouTubeCollector:
    return YouTubeCollector(os.environ["YOUTUBE_API_KEY"])

def get_analyzer() -> SentimentAnalyzer:
    return SentimentAnalyzer(
        anthropic_key=os.environ["ANTHROPIC_API_KEY"],
        hf_token=os.environ["HF_TOKEN"],
    )

def get_db() -> Client:
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


# ── HEALTH ────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "social-monitor-api", "version": "1.1.0"})


# ── PERFIS ────────────────────────────────────────────────────────────────────

@app.route("/profiles", methods=["GET"])
def list_profiles():
    """Lista todos os perfis sendo monitorados."""
    try:
        db = get_db()
        result = db.table("profiles").select("*").order("created_at", desc=True).execute()
        return jsonify({"profiles": result.data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/profiles", methods=["POST"])
def add_profile():
    """
    Adiciona um perfil para monitorar.
    Body: { "platform": "youtube", "platform_id": "UCxxxxxx", "name": "Nome do Político" }
    """
    data = request.get_json()
    required = ["platform", "platform_id", "name"]
    if not all(k in data for k in required):
        return jsonify({"error": f"Campos obrigatórios: {required}"}), 400

    try:
        db = get_db()
        result = db.table("profiles").insert({
            "platform":    data["platform"],
            "platform_id": data["platform_id"],
            "name":        data["name"],
        }).execute()
        return jsonify({"profile": result.data[0]}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/profiles/<profile_id>", methods=["DELETE"])
def delete_profile(profile_id):
    try:
        db = get_db()

        # Busca o username antes de deletar
        profile = db.table("profiles").select("platform_id").eq("id", profile_id).execute()
        username = profile.data[0]["platform_id"] if profile.data else None

        # Deleta posts por profile_id E por username
        if username:
            db.table("instagram_comments").delete().eq("owner_username", username).execute()
            db.table("instagram_posts").delete().eq("owner_username", username).execute()
        db.table("instagram_posts").delete().eq("profile_id", profile_id).execute()

        # Deleta o perfil
        db.table("profiles").delete().eq("id", profile_id).execute()

        return jsonify({"deleted": profile_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── COLETA ────────────────────────────────────────────────────────────────────

@app.route("/collect/youtube/<channel_id>", methods=["POST"])
def collect_youtube(channel_id):
    """
    Coleta dados de um canal do YouTube e armazena no Supabase.
    Query params: days (default 30)
    """
    days = int(request.args.get("days", 30))

    try:
        collector = get_youtube()
        data      = collector.collect_full_profile(channel_id, days=days)
        db        = get_db()

        db.table("channel_snapshots").insert({
            "channel_id":       channel_id,
            "name":             data["channel"]["name"],
            "subscriber_count": data["channel"]["subscriber_count"],
            "video_count":      data["channel"]["video_count"],
            "total_views":      data["channel"]["total_views"],
            "engagement_rate":  data["metrics"]["engagement_rate_pct"],
            "collected_at":     datetime.now(timezone.utc).isoformat(),
        }).execute()

        if data["videos"]:
            db.table("videos").upsert(
                [{**v, "channel_id": channel_id} for v in data["videos"]],
                on_conflict="video_id"
            ).execute()

        return jsonify({
            "channel":  data["channel"]["name"],
            "videos":   data["metrics"]["total_videos"],
            "comments": data["metrics"]["comments_collected"],
            "metrics":  data["metrics"],
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/collect/instagram/<username>", methods=["POST"])
def collect_instagram(username):
    """
    Coleta posts do Instagram via Apify e armazena no Supabase.
    Query params: limit (default 50)
    """
    limit = int(request.args.get("limit", 50))

    try:
        import requests as req

        apify_token = os.environ["APIFY_TOKEN"]
        url = f"https://api.apify.com/v2/acts/apify~instagram-scraper/run-sync-get-dataset-items?token={apify_token}"

        payload = {
            "directUrls": [f"https://www.instagram.com/{username}/"],
            "resultsType": "posts",
            "resultsLimit": limit,
        }

        response = req.post(url, json=payload, timeout=120)
        posts = response.json()

        if not isinstance(posts, list):
            return jsonify({"error": "Resposta inesperada do Apify", "raw": posts}), 500

        db = get_db()

        try:
            profile = (
                db.table("profiles")
                .select("id")
                .eq("platform", "instagram")
                .eq("platform_id", username)
                .execute()
            )
            profile_id = profile.data[0]["id"] if profile.data else None
        except Exception:
            profile_id = None

        rows = []
        for post in posts:
            rows.append({
                "id":               post.get("id"),
                "profile_id":       profile_id,
                "owner_username":   post.get("ownerUsername"),
                "caption":          post.get("caption"),
                "post_type":        post.get("type"),
                "likes_count":      post.get("likesCount"),
                "comments_count":   post.get("commentsCount"),
                "video_view_count": post.get("videoViewCount"),
                "url":              post.get("url"),
                "hashtags":         post.get("hashtags", []),
                "posted_at":        post.get("timestamp"),
            })

        if rows:
            db.table("instagram_posts").upsert(rows, on_conflict="id").execute()

        return jsonify({
            "username":    username,
            "posts_saved": len(rows),
            "profile_id":  profile_id,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/collect/comments/<username>", methods=["POST"])
def collect_comments(username):
    """
    Coleta comentários dos posts do Instagram via Apify.
    Query params: posts_limit (quantos posts buscar, default 10), comments_per_post (default 20)
    """
    posts_limit = int(request.args.get("posts_limit", 10))
    comments_per_post = int(request.args.get("comments_per_post", 20))

    try:
        import requests as req

        db = get_db()

        # Busca as URLs dos posts já coletados
        result = (
            db.table("instagram_posts")
            .select("id, url")
            .eq("owner_username", username)
            .not_.is_("url", "null")
            .order("posted_at", desc=True)
            .limit(posts_limit)
            .execute()
        )

        posts = result.data
        if not posts:
            return jsonify({"error": "Nenhum post coletado para este perfil."}), 404

        urls = [p["url"] for p in posts if p.get("url")]
        post_url_map = {p["url"]: p["id"] for p in posts}

        # Chama o Apify comment scraper
        apify_token = os.environ["APIFY_TOKEN"]
        url = f"https://api.apify.com/v2/acts/apify~instagram-comment-scraper/run-sync-get-dataset-items?token={apify_token}"

        payload = {
            "directUrls": urls,
            "resultsLimit": comments_per_post,
        }

        response = req.post(url, json=payload, timeout=300)
        comments = response.json()

        if not isinstance(comments, list):
            return jsonify({"error": "Resposta inesperada do Apify", "raw": comments}), 500

        rows = []
        for c in comments:
            post_url = c.get("postUrl") or c.get("url", "")
            post_id = post_url_map.get(post_url)
            rows.append({
                "id":                 str(c.get("id")),
                "post_id":            post_id,
                "owner_username":     username,
                "post_url":           post_url,
                "text":               c.get("text"),
                "likes_count":        c.get("likesCount", 0),
                "timestamp":          c.get("timestamp"),
                "commenter_username": c.get("ownerUsername"),
            })

        if rows:
            db.table("instagram_comments").upsert(rows, on_conflict="id").execute()

        return jsonify({
            "username":         username,
            "posts_scraped":    len(urls),
            "comments_saved":   len(rows),
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── ANÁLISE ───────────────────────────────────────────────────────────────────

@app.route("/analyze/youtube/<channel_id>", methods=["POST"])
def analyze_youtube(channel_id):
    """
    Coleta + analisa sentimento de um canal do YouTube.
    Salva o relatório no Supabase.
    Query params: days (default 30)
    """
    days         = int(request.args.get("days", 30))
    profile_name = request.args.get("name", channel_id)

    try:
        collector = get_youtube()
        data      = collector.collect_full_profile(channel_id, days=days)

        analyzer = get_analyzer()
        analysis = analyzer.analyze(data["comments"], profile_name)

        db = get_db()
        report = {
            "channel_id":           channel_id,
            "profile_name":         profile_name,
            "period_days":          days,
            "comments_analyzed":    analysis["summary"]["comments_analyzed"],
            "positive_pct":         analysis["summary"]["positive_pct"],
            "negative_pct":         analysis["summary"]["negative_pct"],
            "neutral_pct":          analysis["summary"]["neutral_pct"],
            "overall_score":        analysis["summary"]["overall_score"],
            "crisis_alert":         analysis["summary"]["crisis_alert"],
            "crisis_reason":        analysis["summary"]["crisis_reason"],
            "main_themes":          analysis["summary"]["main_themes"],
            "top_positive_quote":   analysis["summary"]["top_positive_quote"],
            "top_negative_quote":   analysis["summary"]["top_negative_quote"],
            "narrative":            analysis["summary"]["narrative"],
            "channel_metrics":      data["metrics"],
            "created_at":           datetime.now(timezone.utc).isoformat(),
        }
        result = db.table("analysis_reports").insert(report).execute()

        return jsonify({
            "report_id": result.data[0]["id"],
            "channel":   profile_name,
            "summary":   analysis["summary"],
            "metrics":   data["metrics"],
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/analyze/instagram/<username>", methods=["POST"])
def analyze_instagram(username):
    """
    Analisa sentimento das captions dos posts do Instagram.
    Reutiliza o mesmo pipeline HuggingFace + Claude do YouTube.
    """
    try:
        db = get_db()

        result = (
            db.table("instagram_posts")
            .select("id, caption")
            .eq("owner_username", username)
            .not_.is_("caption", "null")
            .order("posted_at", desc=True)
            .limit(50)
            .execute()
        )

        posts = result.data
        if not posts:
            return jsonify({"error": "Nenhum post coletado para este perfil."}), 404

        comments = [
            {"comment_id": p["id"], "text": p["caption"]}
            for p in posts if p.get("caption")
        ]

        analyzer = get_analyzer()
        analysis = analyzer.analyze(comments, username)

        for s in analysis["sentiments"]:
            db.table("instagram_posts").update({
                "sentiment": s["sentiment"]
            }).eq("id", s["id"]).execute()

        summary = analysis["summary"]
        db.table("instagram_posts").update({
            "ai_summary": summary["narrative"]
        }).eq("owner_username", username).execute()

        return jsonify({
            "username":       username,
            "posts_analyzed": summary["comments_analyzed"],
            "summary":        summary,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/analyze/comments/<username>", methods=["POST"])
def analyze_comments(username):
    """
    Analisa sentimento dos comentários do Instagram via Claude.
    """
    try:
        db = get_db()

        result = (
            db.table("instagram_comments")
            .select("id, text")
            .eq("owner_username", username)
            .not_.is_("text", "null")
            .order("likes_count", desc=True)
            .limit(100)
            .execute()
        )

        comments = result.data
        if not comments:
            return jsonify({"error": "Nenhum comentário coletado para este perfil."}), 404

        formatted = [
            {"comment_id": c["id"], "text": c["text"]}
            for c in comments if c.get("text")
        ]

        analyzer = get_analyzer()
        analysis = analyzer.analyze(formatted, username)

        for s in analysis["sentiments"]:
            db.table("instagram_comments").update({
                "sentiment": s["sentiment"]
            }).eq("id", s["id"]).execute()

        return jsonify({
            "username":          username,
            "comments_analyzed": analysis["summary"]["comments_analyzed"],
            "summary":           analysis["summary"],
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── SIMULADOR DE CENÁRIOS (MiroFish-inspired) ─────────────────────────────────

@app.route("/simulate/scenario", methods=["POST"])
def simulate_scenario():
    """
    Simula reação do eleitorado brasileiro a um conteúdo político.

    Body JSON:
    {
        "conteudo": "texto do post/fala a testar",           // obrigatório
        "n_agentes": 100,                                     // opcional, max 500
        "filtros": {"regiao": "nordeste", "classe": "C"},     // opcional
        "contexto": "candidato a prefeito em 2º turno",       // opcional
        "username": "joaopolitico"                            // opcional
    }
    """
    try:
        data = request.get_json(force=True) or {}
        conteudo = data.get("conteudo", "").strip()

        if not conteudo:
            return jsonify({"erro": "Campo 'conteudo' é obrigatório."}), 400

        n_agentes = min(int(data.get("n_agentes", 100)), 500)
        filtros = data.get("filtros")
        contexto = data.get("contexto", "")
        username = data.get("username")

        # Roda a simulação multi-agente
        forecast = simular(
            conteudo,
            n_agentes=n_agentes,
            filtros=filtros,
            contexto=contexto,
        )

        # Persiste no Supabase
        sim_id = str(uuid.uuid4())
        db = get_db()
        db.table("simulacoes").insert({
            "id":         sim_id,
            "username":   username,
            "conteudo":   conteudo,
            "n_agentes":  n_agentes,
            "filtros":    filtros,
            "contexto":   contexto,
            "forecast":   forecast,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()

        forecast["simulacao_id"] = sim_id
        return jsonify(forecast)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/simulate/history/<username>", methods=["GET"])
def simulate_history(username):
    """Lista simulações anteriores associadas a um perfil."""
    limit = int(request.args.get("limit", 20))
    try:
        db = get_db()
        res = (
            db.table("simulacoes")
            .select("*")
            .eq("username", username)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return jsonify({"username": username, "simulations": res.data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/simulate/<sim_id>", methods=["GET"])
def get_simulation(sim_id):
    """Recupera uma simulação específica pelo ID."""
    try:
        db = get_db()
        res = db.table("simulacoes").select("*").eq("id", sim_id).limit(1).execute()
        if not res.data:
            return jsonify({"error": "Simulação não encontrada"}), 404
        return jsonify(res.data[0])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── WAR ROOM ─────────────────────────────────────────────────────────────────

@app.route("/warroom/threat-level", methods=["GET"])
def warroom_threat_level():
    """
    Calcula o threat level global (DEFCON) somando dados de todos os perfis ativos.
    Considera:
    - % comentários negativos nas últimas 24h
    - Volume de comentários novos (pico anormal)
    - Menções negativas na imprensa (SerpApi)
    """
    try:
        from datetime import timedelta
        db = get_db()
        cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        cutoff_7d = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

        # Perfis ativos
        profiles_res = db.table("profiles").select("*").execute()
        profiles = profiles_res.data or []

        # ── Comentários das últimas 24h ──
        recent_comments_res = (
            db.table("instagram_comments")
            .select("sentiment, owner_username, likes_count, timestamp")
            .gte("timestamp", cutoff_24h)
            .execute()
        )
        recent = recent_comments_res.data or []

        # ── Comentários dos últimos 7d pra calcular baseline ──
        baseline_res = (
            db.table("instagram_comments")
            .select("timestamp, owner_username")
            .gte("timestamp", cutoff_7d)
            .execute()
        )
        baseline = baseline_res.data or []

        total_24h = len(recent)
        total_7d = len(baseline) or 1
        avg_daily = total_7d / 7

        # Pico anormal: volume das últimas 24h > 1.5x média diária
        spike_factor = total_24h / avg_daily if avg_daily > 0 else 0
        has_spike = spike_factor > 1.5

        # % negativo nas últimas 24h
        analyzed = [c for c in recent if c.get("sentiment")]
        neg_count = len([c for c in analyzed if c.get("sentiment") == "negative"])
        neg_pct = (neg_count / len(analyzed) * 100) if analyzed else 0

        # ── Threat Level (DEFCON) ──
        # 5 = calmo, 1 = crise total
        threat_score = 0
        if neg_pct > 60: threat_score += 40
        elif neg_pct > 40: threat_score += 25
        elif neg_pct > 25: threat_score += 12

        if has_spike: threat_score += 25
        if spike_factor > 3: threat_score += 15  # pico extremo

        # Perfis em crise individual
        crisis_profiles_count = 0
        per_profile = {}
        for p in profiles:
            if p["platform"] != "instagram":
                continue
            user = p["platform_id"]
            user_comments = [c for c in analyzed if c.get("owner_username") == user]
            if not user_comments:
                continue
            user_neg = len([c for c in user_comments if c["sentiment"] == "negative"])
            user_neg_pct = (user_neg / len(user_comments)) * 100
            per_profile[user] = {
                "name": p["name"],
                "username": user,
                "neg_pct": round(user_neg_pct, 1),
                "comments_24h": len(user_comments),
                "in_crisis": user_neg_pct > 50,
            }
            if user_neg_pct > 50:
                crisis_profiles_count += 1
                threat_score += 10

        threat_score = min(100, threat_score)

        if threat_score >= 70: defcon = 1; label = "CRISIS ACTIVE"; color = "red"
        elif threat_score >= 50: defcon = 2; label = "EMERGENCY"; color = "red"
        elif threat_score >= 30: defcon = 3; label = "ELEVATED ALERT"; color = "amber"
        elif threat_score >= 15: defcon = 4; label = "WATCH"; color = "amber"
        else: defcon = 5; label = "ALL CLEAR"; color = "green"

        return jsonify({
            "defcon":               defcon,
            "label":                label,
            "color":                color,
            "threat_score":         threat_score,
            "negative_pct_24h":     round(neg_pct, 1),
            "comments_24h":         total_24h,
            "spike_factor":         round(spike_factor, 2),
            "has_spike":            has_spike,
            "crisis_profiles":      crisis_profiles_count,
            "total_profiles":       len([p for p in profiles if p["platform"] == "instagram"]),
            "per_profile":          per_profile,
            "calculated_at":        datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/warroom/negative-feed", methods=["GET"])
def warroom_negative_feed():
    """
    Feed de comentários negativos mais recentes/relevantes de TODOS os perfis.
    Query params: limit (default 30), hours (default 24)
    """
    try:
        from datetime import timedelta
        limit = int(request.args.get("limit", 30))
        hours = int(request.args.get("hours", 24))
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

        db = get_db()
        res = (
            db.table("instagram_comments")
            .select("*")
            .eq("sentiment", "negative")
            .gte("timestamp", cutoff)
            .order("likes_count", desc=True)
            .limit(limit)
            .execute()
        )

        comments = res.data or []

        # Enriquece com nome do político (lookup profiles)
        usernames = list(set(c.get("owner_username") for c in comments if c.get("owner_username")))
        profiles_res = (
            db.table("profiles")
            .select("name, platform_id")
            .in_("platform_id", usernames)
            .execute() if usernames else None
        )
        name_map = {p["platform_id"]: p["name"] for p in (profiles_res.data if profiles_res else [])}

        for c in comments:
            c["politician_name"] = name_map.get(c.get("owner_username"), c.get("owner_username"))

        return jsonify({"comments": comments, "total": len(comments)})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/warroom/generate-response", methods=["POST"])
def warroom_generate_response():
    """
    Gera 3 respostas estratégicas a um ataque e simula cada uma.

    Body JSON:
    {
        "attack": "texto do ataque/crítica que o político está sofrendo",
        "username": "lulaoficial",          // opcional
        "politician_name": "Lula",          // opcional
        "context": "contexto adicional",    // opcional
        "simulate": true                    // se true, simula cada resposta
    }
    """
    try:
        from anthropic import Anthropic

        data = request.get_json(force=True) or {}
        attack = data.get("attack", "").strip()
        username = data.get("username")
        politician_name = data.get("politician_name", "candidato")
        context = data.get("context", "")
        simulate = data.get("simulate", True)

        if not attack:
            return jsonify({"error": "Campo 'attack' é obrigatório."}), 400

        # ── Geração das 3 respostas via Claude ──
        client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

        prompt = f"""Você é um estrategista de comunicação política sênior numa War Room de campanha brasileira.

POLÍTICO: {politician_name}
ATAQUE RECEBIDO: "{attack}"
CONTEXTO: {context or 'campanha eleitoral em andamento'}

Gere EXATAMENTE 3 respostas estratégicas, cada uma com abordagem diferente:

1. DEFENSIVA — esclarece, contextualiza, desarma sem confrontar
2. OFENSIVA — vira o jogo, ataca quem atacou, expõe contradições
3. DESVIO — muda o assunto para pauta forte do candidato

Cada resposta deve:
- Ter 2-3 frases curtas (formato post de redes sociais)
- Soar autêntica em português brasileiro (não acadêmica)
- Ser publicável diretamente

Responda APENAS com JSON válido, sem texto antes ou depois:

{{
  "respostas": [
    {{"estrategia": "defensiva", "texto": "...", "tom": "calmo|sereno|firme", "risco": "baixo|medio|alto"}},
    {{"estrategia": "ofensiva", "texto": "...", "tom": "...", "risco": "..."}},
    {{"estrategia": "desvio", "texto": "...", "tom": "...", "risco": "..."}}
  ]
}}"""

        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1500,
            temperature=0.8,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = msg.content[0].text.strip()
        if "```" in raw:
            raw = raw.split("```")[1] if raw.count("```") >= 2 else raw
            if raw.startswith("json"):
                raw = raw[4:]
        if "{" in raw and "}" in raw:
            raw = raw[raw.index("{"):raw.rindex("}")+1]

        parsed = json.loads(raw.strip()) if False else __import__("json").loads(raw.strip())
        respostas = parsed.get("respostas", [])

        # ── Simulação de cada resposta (opcional) ──
        if simulate and respostas:
            for r in respostas:
                try:
                    sim = simular(
                        conteudo=r["texto"],
                        n_agentes=30,  # amostra menor pra ser rápido
                        contexto=f"resposta de {politician_name} ao ataque: {attack[:100]}"
                    )
                    r["simulation"] = {
                        "crisis_score":  sim.get("crisis_score"),
                        "viral_risk":    sim.get("risco_viralizacao_medio"),
                        "share_pct":     sim.get("engajamento", {}).get("compartilhamento_pct"),
                        "sentiment":     sim.get("sentimento_distribuicao", {}),
                        "agents":        sim.get("total_agentes"),
                    }
                except Exception as sim_err:
                    r["simulation"] = {"error": str(sim_err)}

        # ── Persiste histórico ──
        try:
            db = get_db()
            for r in respostas:
                db.table("war_room_responses").insert({
                    "username":        username,
                    "attack_content":  attack,
                    "response_text":   r["texto"],
                    "strategy":        r["estrategia"],
                    "simulation_data": r.get("simulation"),
                }).execute()
        except Exception:
            pass  # histórico é opcional

        return jsonify({
            "politician":   politician_name,
            "attack":       attack,
            "respostas":    respostas,
            "simulated":    simulate,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500
# ── BUSCA DE MENÇÕES ──────────────────────────────────────────────────────────

@app.route("/search/mentions", methods=["GET"])
def search_mentions():
    """
    Busca menções públicas de um termo via SerpApi.
    Query params: q (termo de busca), limit (default 10)
    """
    query = request.args.get("q", "").strip()
    limit = int(request.args.get("limit", 10))

    if not query:
        return jsonify({"error": "Parâmetro 'q' é obrigatório"}), 400

    try:
        import requests as req

        response = req.get("https://serpapi.com/search", params={
            "q":       query,
            "api_key": os.environ["SERPAPI_KEY"],
            "engine":  "google",
            "hl":      "pt",
            "gl":      "br",
            "num":     min(limit, 10),
            "tbs":     "qdr:w",  # últimos 7 dias
        }, timeout=15)

        data = response.json()

        if "error" in data:
            return jsonify({"error": data["error"]}), 500

        mentions = []
        for item in data.get("organic_results", []):
            mentions.append({
                "title":   item.get("title"),
                "source":  item.get("source") or item.get("displayed_link"),
                "url":     item.get("link"),
                "snippet": item.get("snippet"),
                "date":    item.get("date"),
            })

        if mentions:
            analyzer = get_analyzer()
            formatted = [
                {"comment_id": str(i), "text": f"{m['title']}. {m['snippet']}"}
                for i, m in enumerate(mentions)
            ]
            analysis = analyzer.analyze(formatted, query)
            for i, s in enumerate(analysis["sentiments"]):
                if i < len(mentions):
                    mentions[i]["sentiment"] = s["sentiment"]
                    mentions[i]["score"]     = s["score"]

        return jsonify({
            "query":    query,
            "total":    len(mentions),
            "mentions": mentions,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── LEITURA INSTAGRAM ─────────────────────────────────────────────────────────

@app.route("/instagram/posts/<username>", methods=["GET"])
def get_instagram_posts(username):
    """Retorna posts do Instagram já coletados no Supabase."""
    limit = int(request.args.get("limit", 20))
    try:
        db = get_db()
        result = (
            db.table("instagram_posts")
            .select("*")
            .eq("owner_username", username)
            .order("posted_at", desc=True)
            .limit(limit)
            .execute()
        )
        return jsonify({"username": username, "posts": result.data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/instagram/comments/<username>", methods=["GET"])
def get_comment_analysis(username):
    """Retorna comentários e análise do Instagram."""
    try:
        db = get_db()
        result = (
            db.table("instagram_comments")
            .select("*")
            .eq("owner_username", username)
            .order("likes_count", desc=True)
            .limit(50)
            .execute()
        )
        return jsonify({"username": username, "comments": result.data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── RELATÓRIOS ────────────────────────────────────────────────────────────────

@app.route("/reports/<channel_id>", methods=["GET"])
def get_reports(channel_id):
    """Retorna histórico de relatórios de um canal YouTube."""
    limit = int(request.args.get("limit", 10))
    try:
        db = get_db()
        result = (
            db.table("analysis_reports")
            .select("*")
            .eq("channel_id", channel_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return jsonify({"channel_id": channel_id, "reports": result.data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/reports/latest", methods=["GET"])
def get_all_latest():
    """Retorna o relatório mais recente de cada canal."""
    try:
        db = get_db()
        result = (
            db.table("analysis_reports")
            .select("*")
            .order("created_at", desc=True)
            .limit(50)
            .execute()
        )

        seen, latest = set(), []
        for r in result.data:
            if r["channel_id"] not in seen:
                seen.add(r["channel_id"])
                latest.append(r)

        return jsonify({"reports": latest})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── SNAPSHOTS ────────────────────────────────────────────────────────────────

@app.route("/snapshots/<channel_id>", methods=["GET"])
def get_snapshots(channel_id):
    """Retorna histórico de métricas do canal (para gráfico de crescimento)."""
    limit = int(request.args.get("limit", 30))
    try:
        db = get_db()
        result = (
            db.table("channel_snapshots")
            .select("subscriber_count,engagement_rate,collected_at")
            .eq("channel_id", channel_id)
            .order("collected_at", desc=True)
            .limit(limit)
            .execute()
        )
        return jsonify({"channel_id": channel_id, "snapshots": result.data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)