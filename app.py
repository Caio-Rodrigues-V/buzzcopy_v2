"""
app.py — API Flask do Social Monitor MVP
Endpoints chamados pelo N8n para coletar e analisar perfis políticos.
Multi-tenant com JWT (ver auth.py).
"""
import os
import json
import uuid
import logging
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from flask import Flask, jsonify, request, g
from flask_cors import CORS
from supabase import create_client, Client
from json_repair import repair_json

from collector import YouTubeCollector
from analyzer import SentimentAnalyzer
from pulse_simulator import simular
from auth import register_auth_routes, require_auth, user_owns_profile
from charts import register_chart_routes

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# ── LOGGING ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("pulse")

# ── VALIDAÇÃO DE ENV NO STARTUP (Fix #8) ──────────────────────────────────────
REQUIRED_ENV = ["SUPABASE_URL", "SUPABASE_KEY", "ANTHROPIC_API_KEY", "JWT_SECRET"]
missing = [k for k in REQUIRED_ENV if not os.getenv(k)]
if missing:
    raise RuntimeError(f"Variáveis de ambiente obrigatórias faltando: {missing}")

if os.getenv("JWT_SECRET") == "change-me-in-prod":
    raise RuntimeError("JWT_SECRET ainda está no valor default. Defina um secret real.")

log.info("SUPABASE_URL = %s", os.getenv("SUPABASE_URL"))

app = Flask(__name__)

# ── CORS RESTRITO (Fix #10) ───────────────────────────────────────────────────
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")
CORS(app, resources={r"/*": {"origins": ALLOWED_ORIGINS}})

register_auth_routes(app)
register_chart_routes(app)


# ── SINGLETONS (Fix #9) ───────────────────────────────────────────────────────
_db_client = None
_youtube_client = None
_analyzer_client = None


def get_db() -> Client:
    global _db_client
    if _db_client is None:
        _db_client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    return _db_client


def get_youtube() -> YouTubeCollector:
    global _youtube_client
    if _youtube_client is None:
        _youtube_client = YouTubeCollector(os.environ["YOUTUBE_API_KEY"])
    return _youtube_client


def get_analyzer() -> SentimentAnalyzer:
    global _analyzer_client
    if _analyzer_client is None:
        _analyzer_client = SentimentAnalyzer(
            anthropic_key=os.environ["ANTHROPIC_API_KEY"],
            hf_token=os.environ.get("HF_TOKEN"),
        )
    return _analyzer_client


def user_profile_ids(user_id: str, platform: str = None) -> list:
    """Retorna os platform_ids (usernames/channel_ids) que o user é dono."""
    db = get_db()
    q = db.table("profiles").select("platform_id, platform").eq("user_id", user_id)
    if platform:
        q = q.eq("platform", platform)
    res = q.execute()
    return [p["platform_id"] for p in (res.data or [])]


# ── HEALTH ────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "social-monitor-api",
        "version": os.getenv("APP_VERSION", "1.3.0"),
    })


# ── PERFIS ────────────────────────────────────────────────────────────────────

@app.route("/profiles", methods=["GET"])
@require_auth
def list_profiles():
    """Lista perfis monitorados pelo usuário logado (admin vê tudo)."""
    try:
        db = get_db()
        query = db.table("profiles").select("*").order("created_at", desc=True)
        if g.role != "admin":
            query = query.eq("user_id", g.user_id)
        result = query.execute()
        return jsonify({"profiles": result.data})
    except Exception as e:
        log.exception("list_profiles failed")
        return jsonify({"error": str(e)}), 500


@app.route("/profiles", methods=["POST"])
@require_auth
def add_profile():
    """Cria um perfil monitorado vinculado ao usuário logado."""
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
            "user_id":     g.user_id,
        }).execute()
        return jsonify({"profile": result.data[0]}), 201
    except Exception as e:
        log.exception("add_profile failed")
        return jsonify({"error": str(e)}), 500


@app.route("/profiles/<profile_id>", methods=["DELETE"])
@require_auth
def delete_profile(profile_id):
    """
    Deleta perfil + dados associados.
    Fix #4: só deleta dados Instagram se NENHUM outro perfil ainda referenciar
    o mesmo username.
    """
    try:
        db = get_db()
        profile = db.table("profiles").select("platform_id, platform, user_id").eq("id", profile_id).execute()
        if not profile.data:
            return jsonify({"error": "Perfil não encontrado"}), 404

        p = profile.data[0]
        if g.role != "admin" and p["user_id"] != g.user_id:
            return jsonify({"error": "Sem permissão pra deletar este perfil"}), 403

        username = p["platform_id"]
        platform = p["platform"]

        # Verifica se outros perfis ainda referenciam esse username
        others = (
            db.table("profiles")
            .select("id")
            .eq("platform", platform)
            .eq("platform_id", username)
            .neq("id", profile_id)
            .execute()
        )
        outros_referenciam = bool(others.data)

        # Deleta dados Instagram só se mais ninguém referenciar (Fix #4)
        if username and platform == "instagram" and not outros_referenciam:
            db.table("instagram_comments").delete().eq("owner_username", username).execute()
            db.table("instagram_posts").delete().eq("owner_username", username).execute()

        # Posts ligados diretamente a este profile_id (caso o owner_username não exista)
        db.table("instagram_posts").delete().eq("profile_id", profile_id).execute()
        db.table("profiles").delete().eq("id", profile_id).execute()

        return jsonify({"deleted": profile_id, "shared_data_kept": outros_referenciam})
    except Exception as e:
        log.exception("delete_profile failed")
        return jsonify({"error": str(e)}), 500


# ── COLETA ────────────────────────────────────────────────────────────────────

@app.route("/collect/youtube/<channel_id>", methods=["POST"])
@require_auth
def collect_youtube(channel_id):
    if g.role != "admin" and not user_owns_profile(channel_id, g.user_id, platform="youtube"):
        return jsonify({"error": "Acesso negado a este canal"}), 403

    days = int(request.args.get("days", 30))

    try:
        collector = get_youtube()
        data = collector.collect_full_profile(channel_id, days=days)
        db = get_db()

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
        log.exception("collect_youtube failed")
        return jsonify({"error": str(e)}), 500

# ── COLETA UNIFICADA (v2) ─────────────────────────────────────────────────────

from collectors.instagram import InstagramCollector

COLLECTOR_REGISTRY = {
    "instagram": InstagramCollector,
    # "twitter": TwitterCollector,   ← Rodada 2
    # "news":    NewsCollector,      ← Rodada 3
}


def _get_collector(platform: str):
    klass = COLLECTOR_REGISTRY.get(platform)
    if not klass:
        return None
    return klass()


@app.route("/v2/collect/<profile_id>", methods=["POST"])
@require_auth
def v2_collect(profile_id):
    """
    Coleta unificada: pega o profile, lê os handles, e dispara coleta
    em cada plataforma configurada.

    Query params:
      - platforms: csv de plataformas a coletar (default: todas que o profile tem)
      - posts_limit: posts por plataforma (default 50)
      - reactions_per_post: comentários por post (default 20)
    """
    try:
        db = get_db()
        profile_res = db.table("profiles").select("*").eq("id", profile_id).execute()
        if not profile_res.data:
            return jsonify({"error": "Profile não encontrado"}), 404

        profile = profile_res.data[0]
        if g.role != "admin" and profile["user_id"] != g.user_id:
            return jsonify({"error": "Acesso negado a este profile"}), 403

        handles = profile.get("handles") or {}
        if not handles:
            return jsonify({"error": "Profile sem handles configurados"}), 400

        requested = request.args.get("platforms")
        platforms = [p.strip() for p in requested.split(",")] if requested else list(handles.keys())

        posts_limit = int(request.args.get("posts_limit", 50))
        reactions_per_post = int(request.args.get("reactions_per_post", 20))

        results = {}
        for platform in platforms:
            handle = handles.get(platform)
            if not handle:
                results[platform] = {"skipped": "handle não configurado"}
                continue

            collector = _get_collector(platform)
            if not collector:
                results[platform] = {"skipped": "coletor não implementado"}
                continue

            # Coleta posts
            posts = collector.collect_posts(handle, profile_id, limit=posts_limit)
            if posts:
                db.table("social_posts").upsert(posts, on_conflict="id").execute()

            # Coleta reactions
            reactions = collector.collect_reactions(
                handle, profile_id,
                posts_limit=min(posts_limit, 10),
                reactions_per_post=reactions_per_post,
            )
            if reactions:
                db.table("social_comments").upsert(reactions, on_conflict="id").execute()

            results[platform] = {
                "handle":           handle,
                "posts_saved":      len(posts),
                "reactions_saved":  len(reactions),
            }

        return jsonify({
            "profile_id": profile_id,
            "name":       profile["name"],
            "results":    results,
        })

    except Exception as e:
        log.exception("v2_collect failed")
        return jsonify({"error": str(e)}), 500
    
@app.route("/collect/instagram/<username>", methods=["POST"])
@require_auth
def collect_instagram(username):
    if g.role != "admin" and not user_owns_profile(username, g.user_id):
        return jsonify({"error": "Acesso negado a este perfil"}), 403

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
            # Fix #7: garante que o id existe antes de inserir
            if not post.get("id"):
                continue
            rows.append({
                "id":               post["id"],
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
        log.exception("collect_instagram failed")
        return jsonify({"error": str(e)}), 500


@app.route("/collect/comments/<username>", methods=["POST"])
@require_auth
def collect_comments(username):
    if g.role != "admin" and not user_owns_profile(username, g.user_id):
        return jsonify({"error": "Acesso negado a este perfil"}), 403

    posts_limit = int(request.args.get("posts_limit", 10))
    comments_per_post = int(request.args.get("comments_per_post", 20))

    try:
        import requests as req

        db = get_db()

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
            if not c.get("id"):
                continue
            post_url = c.get("postUrl") or c.get("url", "")
            post_id = post_url_map.get(post_url)
            rows.append({
                "id":                 str(c["id"]),
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
        log.exception("collect_comments failed")
        return jsonify({"error": str(e)}), 500


# ── ANÁLISE ───────────────────────────────────────────────────────────────────

@app.route("/analyze/youtube/<channel_id>", methods=["POST"])
@require_auth
def analyze_youtube(channel_id):
    """
    Fix #2: agora persiste vídeos e snapshot junto com a análise.
    """
    if g.role != "admin" and not user_owns_profile(channel_id, g.user_id, platform="youtube"):
        return jsonify({"error": "Acesso negado a este canal"}), 403

    days = int(request.args.get("days", 30))
    profile_name = request.args.get("name", channel_id)

    try:
        collector = get_youtube()
        data = collector.collect_full_profile(channel_id, days=days)

        db = get_db()

        # Fix #2: persiste snapshot e vídeos (antes só consumia quota e descartava)
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

        analyzer = get_analyzer()
        analysis = analyzer.analyze(data["comments"], profile_name)

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
        log.exception("analyze_youtube failed")
        return jsonify({"error": str(e)}), 500


@app.route("/analyze/instagram/<username>", methods=["POST"])
@require_auth
def analyze_instagram(username):
    """
    Fix #1: não escreve mais ai_summary em TODOS os posts (corrompia dados).
    O resumo geral vai pra analysis_reports com platform='instagram'.
    """
    if g.role != "admin" and not user_owns_profile(username, g.user_id):
        return jsonify({"error": "Acesso negado a este perfil"}), 403

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

        # Atualiza sentimento por post (legítimo: cada post tem seu próprio sentimento)
        for s in analysis["sentiments"]:
            db.table("instagram_posts").update({
                "sentiment": s["sentiment"]
            }).eq("id", s["id"]).execute()

        # Fix #1: salva resumo geral em analysis_reports (não corrompe mais os posts)
        summary = analysis["summary"]
        report = {
            "channel_id":           username,  # reusa o campo channel_id pra username IG
            "profile_name":         username,
            "period_days":          0,  # 0 = análise pontual, não baseada em dias
            "comments_analyzed":    summary["comments_analyzed"],
            "positive_pct":         summary["positive_pct"],
            "negative_pct":         summary["negative_pct"],
            "neutral_pct":          summary["neutral_pct"],
            "overall_score":        summary["overall_score"],
            "crisis_alert":         summary["crisis_alert"],
            "crisis_reason":        summary["crisis_reason"],
            "main_themes":          summary["main_themes"],
            "top_positive_quote":   summary["top_positive_quote"],
            "top_negative_quote":   summary["top_negative_quote"],
            "narrative":            summary["narrative"],
            "created_at":           datetime.now(timezone.utc).isoformat(),
        }
        db.table("analysis_reports").insert(report).execute()

        return jsonify({
            "username":       username,
            "posts_analyzed": summary["comments_analyzed"],
            "summary":        summary,
        })

    except Exception as e:
        log.exception("analyze_instagram failed")
        return jsonify({"error": str(e)}), 500


@app.route("/analyze/comments/<username>", methods=["POST"])
@require_auth
def analyze_comments(username):
    if g.role != "admin" and not user_owns_profile(username, g.user_id):
        return jsonify({"error": "Acesso negado a este perfil"}), 403

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
        log.exception("analyze_comments failed")
        return jsonify({"error": str(e)}), 500


# ── SIMULADOR DE CENÁRIOS ─────────────────────────────────────────────────────

@app.route("/simulate/scenario", methods=["POST"])
@require_auth
def simulate_scenario():
    try:
        data = request.get_json(force=True) or {}
        conteudo = data.get("conteudo", "").strip()

        if not conteudo:
            return jsonify({"erro": "Campo 'conteudo' é obrigatório."}), 400

        username = data.get("username")
        if username and g.role != "admin" and not user_owns_profile(username, g.user_id):
            return jsonify({"error": "Acesso negado a este perfil"}), 403

        n_agentes = min(int(data.get("n_agentes", 100)), 500)
        filtros = data.get("filtros")
        contexto = data.get("contexto", "")

        forecast = simular(
            conteudo,
            n_agentes=n_agentes,
            filtros=filtros,
            contexto=contexto,
        )

        sim_id = str(uuid.uuid4())
        db = get_db()
        db.table("simulacoes").insert({
            "id":         sim_id,
            "username":   username,
            "user_id":    g.user_id,
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
        log.exception("simulate_scenario failed")
        return jsonify({"error": str(e)}), 500


@app.route("/simulate/history/<username>", methods=["GET"])
@require_auth
def simulate_history(username):
    """
    Fix #3: filtra por user_id também, não só username.
    Antes: dois users monitorando o mesmo perfil viam simulações um do outro.
    """
    if g.role != "admin" and not user_owns_profile(username, g.user_id):
        return jsonify({"error": "Acesso negado a este perfil"}), 403

    limit = int(request.args.get("limit", 20))
    try:
        db = get_db()
        query = (
            db.table("simulacoes")
            .select("*")
            .eq("username", username)
            .order("created_at", desc=True)
            .limit(limit)
        )
        # Admin vê tudo, cliente só as próprias
        if g.role != "admin":
            query = query.eq("user_id", g.user_id)

        res = query.execute()
        return jsonify({"username": username, "simulations": res.data})
    except Exception as e:
        log.exception("simulate_history failed")
        return jsonify({"error": str(e)}), 500


@app.route("/simulate/<sim_id>", methods=["GET"])
@require_auth
def get_simulation(sim_id):
    try:
        db = get_db()
        res = db.table("simulacoes").select("*").eq("id", sim_id).limit(1).execute()
        if not res.data:
            return jsonify({"error": "Simulação não encontrada"}), 404

        sim = res.data[0]
        if g.role != "admin":
            owner_user_id = sim.get("user_id")
            sim_username = sim.get("username")
            allowed = (
                owner_user_id == g.user_id
                or (sim_username and user_owns_profile(sim_username, g.user_id))
            )
            if not allowed:
                return jsonify({"error": "Acesso negado a esta simulação"}), 403

        return jsonify(sim)
    except Exception as e:
        log.exception("get_simulation failed")
        return jsonify({"error": str(e)}), 500


# ── WAR ROOM ─────────────────────────────────────────────────────────────────

@app.route("/warroom/threat-level", methods=["GET"])
@require_auth
def warroom_threat_level():
    """
    Fix #5: agora considera tanto Instagram (comentários 24h) quanto YouTube
    (analysis_reports com crisis_alert). Antes só olhava Instagram.
    """
    try:
        db = get_db()
        cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        cutoff_7d = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

        # Perfis do usuário
        profiles_q = db.table("profiles").select("*")
        if g.role != "admin":
            profiles_q = profiles_q.eq("user_id", g.user_id)
        profiles = profiles_q.execute().data or []

        ig_usernames = [p["platform_id"] for p in profiles if p["platform"] == "instagram"]
        yt_channels = [p["platform_id"] for p in profiles if p["platform"] == "youtube"]

        threat_score = 0
        per_profile = {}
        crisis_profiles_count = 0
        neg_pct = 0
        total_24h = 0
        spike_factor = 0
        has_spike = False

        # ── Instagram (mesma lógica de antes) ──
        if ig_usernames:
            recent = (
                db.table("instagram_comments")
                .select("sentiment, owner_username, likes_count, timestamp")
                .in_("owner_username", ig_usernames)
                .gte("timestamp", cutoff_24h)
                .execute()
            ).data or []

            baseline = (
                db.table("instagram_comments")
                .select("timestamp, owner_username")
                .in_("owner_username", ig_usernames)
                .gte("timestamp", cutoff_7d)
                .execute()
            ).data or []

            total_24h = len(recent)
            total_7d = len(baseline) or 1
            avg_daily = total_7d / 7

            spike_factor = total_24h / avg_daily if avg_daily > 0 else 0
            has_spike = spike_factor > 1.5

            analyzed = [c for c in recent if c.get("sentiment")]
            neg_count = len([c for c in analyzed if c.get("sentiment") == "negative"])
            neg_pct = (neg_count / len(analyzed) * 100) if analyzed else 0

            if neg_pct > 60: threat_score += 40
            elif neg_pct > 40: threat_score += 25
            elif neg_pct > 25: threat_score += 12

            if has_spike: threat_score += 25
            if spike_factor > 3: threat_score += 15

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
                    "platform": "instagram",
                    "neg_pct": round(user_neg_pct, 1),
                    "comments_24h": len(user_comments),
                    "in_crisis": user_neg_pct > 50,
                }
                if user_neg_pct > 50:
                    crisis_profiles_count += 1
                    threat_score += 10

        # ── YouTube (Fix #5: considera crisis_alert dos relatórios) ──
        yt_crisis_count = 0
        if yt_channels:
            yt_reports = (
                db.table("analysis_reports")
                .select("channel_id, profile_name, crisis_alert, negative_pct, narrative, created_at")
                .in_("channel_id", yt_channels)
                .gte("created_at", cutoff_7d)
                .order("created_at", desc=True)
                .execute()
            ).data or []

            seen_channels = set()
            for r in yt_reports:
                ch = r["channel_id"]
                if ch in seen_channels:
                    continue
                seen_channels.add(ch)

                p_match = next((p for p in profiles if p["platform_id"] == ch), None)
                if not p_match:
                    continue

                per_profile[ch] = {
                    "name": r.get("profile_name") or p_match["name"],
                    "platform": "youtube",
                    "neg_pct": r.get("negative_pct") or 0,
                    "in_crisis": bool(r.get("crisis_alert")),
                    "last_report_at": r.get("created_at"),
                }
                if r.get("crisis_alert"):
                    yt_crisis_count += 1
                    crisis_profiles_count += 1
                    threat_score += 20
                elif (r.get("negative_pct") or 0) > 50:
                    threat_score += 10

        threat_score = min(100, threat_score)

        if threat_score >= 70:   defcon, label, color = 1, "CRISIS ACTIVE", "red"
        elif threat_score >= 50: defcon, label, color = 2, "EMERGENCY", "red"
        elif threat_score >= 30: defcon, label, color = 3, "ELEVATED ALERT", "amber"
        elif threat_score >= 15: defcon, label, color = 4, "WATCH", "amber"
        else:                    defcon, label, color = 5, "ALL CLEAR", "green"

        return jsonify({
            "defcon":           defcon,
            "label":            label,
            "color":            color,
            "threat_score":     threat_score,
            "negative_pct_24h": round(neg_pct, 1),
            "comments_24h":     total_24h,
            "spike_factor":     round(spike_factor, 2),
            "has_spike":        has_spike,
            "crisis_profiles":  crisis_profiles_count,
            "yt_crisis_count":  yt_crisis_count,
            "total_profiles":   len(ig_usernames) + len(yt_channels),
            "per_profile":      per_profile,
            "calculated_at":    datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        log.exception("warroom_threat_level failed")
        return jsonify({"error": str(e)}), 500


@app.route("/warroom/negative-feed", methods=["GET"])
@require_auth
def warroom_negative_feed():
    try:
        limit = int(request.args.get("limit", 30))
        hours = int(request.args.get("hours", 24))
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

        db = get_db()

        profiles_q = db.table("profiles").select("platform_id, name").eq("platform", "instagram")
        if g.role != "admin":
            profiles_q = profiles_q.eq("user_id", g.user_id)
        user_profiles = profiles_q.execute().data or []
        usernames = [p["platform_id"] for p in user_profiles]

        if not usernames:
            return jsonify({"comments": [], "total": 0})

        res = (
            db.table("instagram_comments")
            .select("*")
            .in_("owner_username", usernames)
            .eq("sentiment", "negative")
            .gte("timestamp", cutoff)
            .order("likes_count", desc=True)
            .limit(limit)
            .execute()
        )

        comments = res.data or []
        name_map = {p["platform_id"]: p["name"] for p in user_profiles}
        for c in comments:
            c["politician_name"] = name_map.get(c.get("owner_username"), c.get("owner_username"))

        return jsonify({"comments": comments, "total": len(comments)})

    except Exception as e:
        log.exception("warroom_negative_feed failed")
        return jsonify({"error": str(e)}), 500


@app.route("/warroom/generate-response", methods=["POST"])
@require_auth
def warroom_generate_response():
    """
    Gera 3 respostas estratégicas. Usa json_repair pra ser robusto a parsing.
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

        if username and g.role != "admin" and not user_owns_profile(username, g.user_id):
            return jsonify({"error": "Acesso negado a este perfil"}), 403

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

        # Fix #6: parsing robusto via json_repair
        raw = msg.content[0].text.strip()
        if "```" in raw:
            parts = raw.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                if part.startswith("{"):
                    raw = part
                    break

        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start != -1 and end > start:
            raw = raw[start:end]

        try:
            parsed = json.loads(repair_json(raw))
        except Exception as parse_err:
            log.error("Falha ao parsear resposta de Claude no war room: %s | raw=%s", parse_err, raw[:300])
            return jsonify({"error": "Resposta inválida do modelo. Tente novamente."}), 502

        respostas = parsed.get("respostas", [])

        if simulate and respostas:
            for r in respostas:
                try:
                    sim = simular(
                        conteudo=r["texto"],
                        n_agentes=30,
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

        try:
            db = get_db()
            for r in respostas:
                db.table("war_room_responses").insert({
                    "username":        username,
                    "user_id":         g.user_id,
                    "attack_content":  attack,
                    "response_text":   r["texto"],
                    "strategy":        r["estrategia"],
                    "simulation_data": r.get("simulation"),
                }).execute()
        except Exception as db_err:
            log.warning("Falha ao salvar war_room_responses: %s", db_err)

        return jsonify({
            "politician":   politician_name,
            "attack":       attack,
            "respostas":    respostas,
            "simulated":    simulate,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        })

    except Exception as e:
        log.exception("warroom_generate_response failed")
        return jsonify({"error": str(e)}), 500


# ── BUSCA DE MENÇÕES ──────────────────────────────────────────────────────────

@app.route("/search/mentions", methods=["GET"])
@require_auth
def search_mentions():
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
            "tbs":     "qdr:w",
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
                    mentions[i]["score"] = s["score"]

        return jsonify({
            "query":    query,
            "total":    len(mentions),
            "mentions": mentions,
        })

    except Exception as e:
        log.exception("search_mentions failed")
        return jsonify({"error": str(e)}), 500


# ── LEITURA INSTAGRAM ─────────────────────────────────────────────────────────

@app.route("/instagram/posts/<username>", methods=["GET"])
@require_auth
def get_instagram_posts(username):
    if g.role != "admin" and not user_owns_profile(username, g.user_id):
        return jsonify({"error": "Acesso negado a este perfil"}), 403

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
        log.exception("get_instagram_posts failed")
        return jsonify({"error": str(e)}), 500


@app.route("/instagram/comments/<username>", methods=["GET"])
@require_auth
def get_comment_analysis(username):
    if g.role != "admin" and not user_owns_profile(username, g.user_id):
        return jsonify({"error": "Acesso negado a este perfil"}), 403

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
        log.exception("get_comment_analysis failed")
        return jsonify({"error": str(e)}), 500


# ── RELATÓRIOS ────────────────────────────────────────────────────────────────

@app.route("/reports/<channel_id>", methods=["GET"])
@require_auth
def get_reports(channel_id):
    if g.role != "admin" and not user_owns_profile(channel_id, g.user_id, platform="youtube"):
        return jsonify({"error": "Acesso negado a este canal"}), 403

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
        log.exception("get_reports failed")
        return jsonify({"error": str(e)}), 500


@app.route("/reports/latest", methods=["GET"])
@require_auth
def get_all_latest():
    try:
        db = get_db()

        if g.role == "admin":
            result = (
                db.table("analysis_reports")
                .select("*")
                .order("created_at", desc=True)
                .limit(50)
                .execute()
            )
        else:
            channel_ids = user_profile_ids(g.user_id, platform="youtube")
            if not channel_ids:
                return jsonify({"reports": []})
            result = (
                db.table("analysis_reports")
                .select("*")
                .in_("channel_id", channel_ids)
                .order("created_at", desc=True)
                .limit(50)
                .execute()
            )

        seen, latest = set(), []
        for r in result.data or []:
            if r["channel_id"] not in seen:
                seen.add(r["channel_id"])
                latest.append(r)

        return jsonify({"reports": latest})
    except Exception as e:
        log.exception("get_all_latest failed")
        return jsonify({"error": str(e)}), 500


# ── SNAPSHOTS ────────────────────────────────────────────────────────────────

@app.route("/snapshots/<channel_id>", methods=["GET"])
@require_auth
def get_snapshots(channel_id):
    if g.role != "admin" and not user_owns_profile(channel_id, g.user_id, platform="youtube"):
        return jsonify({"error": "Acesso negado a este canal"}), 403

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
        log.exception("get_snapshots failed")
        return jsonify({"error": str(e)}), 500


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)