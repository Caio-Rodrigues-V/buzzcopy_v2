"""
app.py — API Flask do Social Monitor MVP
Endpoints chamados pelo N8n para coletar e analisar perfis políticos
"""

import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS
from supabase import create_client, Client

from collector import YouTubeCollector
from analyzer import SentimentAnalyzer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

print("SUPABASE_URL =", os.getenv("SUPABASE_URL"))
print("SUPABASE_KEY carregada =", bool(os.getenv("SUPABASE_KEY")))

load_dotenv()

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
    return jsonify({"status": "ok", "service": "social-monitor-api", "version": "1.0.0"})


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
    """Remove um perfil do monitoramento."""
    try:
        db = get_db()
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
    Chamado pelo N8n no agendamento diário.
    """
    days = int(request.args.get("days", 30))

    try:
        collector = get_youtube()
        data      = collector.collect_full_profile(channel_id, days=days)
        db        = get_db()

        # Salva snapshot do canal
        db.table("channel_snapshots").insert({
            "channel_id":       channel_id,
            "name":             data["channel"]["name"],
            "subscriber_count": data["channel"]["subscriber_count"],
            "video_count":      data["channel"]["video_count"],
            "total_views":      data["channel"]["total_views"],
            "engagement_rate":  data["metrics"]["engagement_rate_pct"],
            "collected_at":     datetime.now(timezone.utc).isoformat(),
        }).execute()

        # Salva vídeos (upsert por video_id)
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


# ── ANÁLISE ───────────────────────────────────────────────────────────────────
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
@app.route("/analyze/youtube/<channel_id>", methods=["POST"])
def analyze_youtube(channel_id):
    """
    Coleta + analisa sentimento de um canal em uma única chamada.
    Salva o relatório no Supabase.
    Query params: days (default 30)
    """
    days         = int(request.args.get("days", 30))
    profile_name = request.args.get("name", channel_id)

    try:
        # 1. Coleta
        collector = get_youtube()
        data      = collector.collect_full_profile(channel_id, days=days)

        # 2. Análise
        analyzer = get_analyzer()
        analysis = analyzer.analyze(data["comments"], profile_name)

        # 3. Salva relatório
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

        # 1. Busca posts já coletados
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

        # 2. Adapta pro formato que o analyzer espera
        comments = [
            {"comment_id": p["id"], "text": p["caption"]}
            for p in posts if p.get("caption")
        ]

        # 3. Roda o mesmo pipeline
        analyzer = get_analyzer()
        analysis = analyzer.analyze(comments, username)

        # 4. Atualiza sentiment em cada post
        for s in analysis["sentiments"]:
            db.table("instagram_posts").update({
                "sentiment": s["sentiment"]
            }).eq("id", s["id"]).execute()

        # 5. Salva resumo geral
        summary = analysis["summary"]
        db.table("instagram_posts").update({
            "ai_summary": summary["narrative"]
        }).eq("owner_username", username).execute()

        return jsonify({
            "username":    username,
            "posts_analyzed": summary["comments_analyzed"],
            "summary":     summary,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500
# ── INSTAGRAM ────────────────────────────────────────────────────────────────

@app.route("/collect/instagram/<username>", methods=["POST"])
def collect_instagram(username):
    """
    Coleta posts do Instagram via Apify e armazena no Supabase.
    Query params: limit (default 10)
    """
    limit = int(request.args.get("limit", 10))

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

        # Busca profile_id correspondente
        profile = (
            db.table("profiles")
            .select("id")
            .eq("platform", "instagram")
            .eq("platform_id", username)
            .single()
            .execute()
        )
        profile_id = profile.data["id"] if profile.data else None

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
            "username":      username,
            "posts_saved":   len(rows),
            "profile_id":    profile_id,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── RELATÓRIOS ────────────────────────────────────────────────────────────────

@app.route("/reports/<channel_id>", methods=["GET"])
def get_reports(channel_id):
    """
    Retorna histórico de relatórios de um canal.
    Query params: limit (default 10)
    """
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
    """
    Retorna o relatório mais recente de cada canal.
    Útil para o dashboard mostrar visão geral de todos os perfis.
    """
    try:
        db = get_db()
        result = (
            db.table("analysis_reports")
            .select("*")
            .order("created_at", desc=True)
            .limit(50)
            .execute()
        )

        # Pega somente o mais recente por canal
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
