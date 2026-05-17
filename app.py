"""
app.py — Pulse API v3 (multi-handle unificado).

Modelo: 1 profile = 1 alvo monitorado, com N handles (instagram, twitter, youtube, ...).
Todas as rotas operam por profile_id. Tabelas social_posts e social_comments
unificam todas as plataformas.
"""
import os
import json
import logging
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from flask import Flask, jsonify, request, g
from flask_cors import CORS
from supabase import create_client, Client
from json_repair import repair_json

from analyzer import SentimentAnalyzer
from pulse_simulator import simular
from auth import register_auth_routes, require_auth, guard_profile
from charts import register_chart_routes

from collectors.instagram import InstagramCollector
from collectors.twitter   import TwitterCollector
from collectors.youtube   import YouTubeCollector

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# ── LOGGING ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("pulse")

# ── VALIDAÇÃO DE ENV ──────────────────────────────────────────────────────────
REQUIRED_ENV = ["SUPABASE_URL", "SUPABASE_KEY", "ANTHROPIC_API_KEY", "JWT_SECRET"]
missing = [k for k in REQUIRED_ENV if not os.getenv(k)]
if missing:
    raise RuntimeError(f"Variáveis de ambiente obrigatórias faltando: {missing}")

app = Flask(__name__)
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")
CORS(app, resources={r"/*": {"origins": ALLOWED_ORIGINS}})

register_auth_routes(app)
register_chart_routes(app)


# ── SINGLETONS ────────────────────────────────────────────────────────────────
_db_client = None
_analyzer_client = None


def get_db() -> Client:
    global _db_client
    if _db_client is None:
        _db_client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    return _db_client


def get_analyzer() -> SentimentAnalyzer:
    global _analyzer_client
    if _analyzer_client is None:
        _analyzer_client = SentimentAnalyzer(
            anthropic_key=os.environ["ANTHROPIC_API_KEY"],
            hf_token=os.environ.get("HF_TOKEN"),
        )
    return _analyzer_client


# ── REGISTRY DE COLETORES ─────────────────────────────────────────────────────
COLLECTOR_REGISTRY = {
    "instagram": InstagramCollector,
    "twitter":   TwitterCollector,
    "youtube":   YouTubeCollector,
}


def _get_collector(platform: str):
    klass = COLLECTOR_REGISTRY.get(platform)
    return klass() if klass else None


# ── HEALTH ────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "pulse-api",
        "version": os.getenv("APP_VERSION", "3.0.0"),
        "platforms": list(COLLECTOR_REGISTRY.keys()),
    })


# ── PROFILES (ALVOS MONITORADOS) ──────────────────────────────────────────────

@app.route("/profiles", methods=["GET"])
@require_auth
def list_profiles():
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
    """
    Cria um alvo monitorado.
    Body: { "name": "Lula", "handles": {"instagram": "...", "twitter": "...", "youtube": "UCxxx"} }
    """
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    handles = data.get("handles") or {}

    if not name:
        return jsonify({"error": "Campo 'name' é obrigatório"}), 400

    # Limpa handles vazios e normaliza
    clean_handles = {}
    for k, v in handles.items():
        if v and isinstance(v, str):
            clean_handles[k] = v.strip().lstrip("@")

    if not clean_handles:
        return jsonify({"error": "Pelo menos 1 handle deve ser informado"}), 400

    # Valida que todas as plataformas têm coletor implementado
    unknown = [p for p in clean_handles if p not in COLLECTOR_REGISTRY]
    if unknown:
        return jsonify({"error": f"Plataformas não suportadas: {unknown}"}), 400

    try:
        db = get_db()
        result = db.table("profiles").insert({
            "name":    name,
            "handles": clean_handles,
            "user_id": g.user_id,
        }).execute()
        return jsonify({"profile": result.data[0]}), 201
    except Exception as e:
        log.exception("add_profile failed")
        return jsonify({"error": str(e)}), 500


@app.route("/profiles/<profile_id>", methods=["PATCH"])
@require_auth
def update_profile(profile_id):
    """Atualiza name ou handles de um profile."""
    denied = guard_profile(profile_id)
    if denied: return denied

    data = request.get_json() or {}
    update = {}

    if "name" in data:
        update["name"] = (data["name"] or "").strip()

    if "handles" in data:
        handles = data["handles"] or {}
        clean = {}
        for k, v in handles.items():
            if v and isinstance(v, str):
                clean[k] = v.strip().lstrip("@")
        unknown = [p for p in clean if p not in COLLECTOR_REGISTRY]
        if unknown:
            return jsonify({"error": f"Plataformas não suportadas: {unknown}"}), 400
        update["handles"] = clean

    if not update:
        return jsonify({"error": "Nada pra atualizar"}), 400

    try:
        db = get_db()
        result = db.table("profiles").update(update).eq("id", profile_id).execute()
        return jsonify({"profile": result.data[0] if result.data else None})
    except Exception as e:
        log.exception("update_profile failed")
        return jsonify({"error": str(e)}), 500


@app.route("/profiles/<profile_id>", methods=["DELETE"])
@require_auth
def delete_profile(profile_id):
    """Deleta o profile e tudo associado (cascade via FKs)."""
    denied = guard_profile(profile_id)
    if denied: return denied

    try:
        db = get_db()
        db.table("profiles").delete().eq("id", profile_id).execute()
        return jsonify({"deleted": profile_id})
    except Exception as e:
        log.exception("delete_profile failed")
        return jsonify({"error": str(e)}), 500


# ── COLETA UNIFICADA ──────────────────────────────────────────────────────────

@app.route("/v2/collect/<profile_id>", methods=["POST"])
@require_auth
def v2_collect(profile_id):
    """
    Coleta tudo: pega o profile, lê os handles, dispara coleta em cada plataforma.
    Query params:
      - platforms: csv opcional (default: todos os handles do profile)
      - posts_limit: posts por plataforma (default 30)
      - reactions_per_post: comentários por post (default 20)
    """
    denied = guard_profile(profile_id)
    if denied: return denied

    try:
        db = get_db()
        profile = db.table("profiles").select("*").eq("id", profile_id).execute().data[0]
        handles = profile.get("handles") or {}

        if not handles:
            return jsonify({"error": "Profile sem handles configurados"}), 400

        requested = request.args.get("platforms")
        platforms = [p.strip() for p in requested.split(",")] if requested else list(handles.keys())

        posts_limit = int(request.args.get("posts_limit", 30))
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

            try:
                posts = collector.collect_posts(handle, profile_id, limit=posts_limit)
                if posts:
                    db.table("social_posts").upsert(posts, on_conflict="id").execute()

                reactions = collector.collect_reactions(
                    handle, profile_id,
                    posts_limit=min(posts_limit, 10),
                    reactions_per_post=reactions_per_post,
                )
                if reactions:
                    db.table("social_comments").upsert(reactions, on_conflict="id").execute()

                results[platform] = {
                    "handle":          handle,
                    "posts_saved":     len(posts),
                    "reactions_saved": len(reactions),
                }
            except Exception as platform_err:
                log.exception("Falha ao coletar %s pra profile %s", platform, profile_id)
                results[platform] = {"error": str(platform_err)}

        return jsonify({
            "profile_id": profile_id,
            "name":       profile["name"],
            "results":    results,
        })

    except Exception as e:
        log.exception("v2_collect failed")
        return jsonify({"error": str(e)}), 500


# ── ANÁLISE UNIFICADA ─────────────────────────────────────────────────────────

@app.route("/v2/analyze/<profile_id>", methods=["POST"])
@require_auth
def v2_analyze(profile_id):
    """
    Analisa sentimento dos comentários do profile via Claude.
    Cross-platform: mistura Instagram + Twitter + YouTube no mesmo prompt
    e gera 1 relatório consolidado + breakdown por plataforma.

    Query params:
      - platforms: csv opcional (default: todas)
      - limit: máximo de comentários (default 200)
    """
    denied = guard_profile(profile_id)
    if denied: return denied

    try:
        db = get_db()
        profile = db.table("profiles").select("*").eq("id", profile_id).execute().data[0]

        requested = request.args.get("platforms")
        platforms = [p.strip() for p in requested.split(",")] if requested else None
        limit = int(request.args.get("limit", 200))

        # Busca comentários
        query = (
            db.table("social_comments")
            .select("id, content, platform, metrics")
            .eq("profile_id", profile_id)
            .not_.is_("content", "null")
        )
        if platforms:
            query = query.in_("platform", platforms)

        comments = (
            query.order("posted_at", desc=True)
            .limit(limit)
            .execute()
        ).data or []

        if not comments:
            return jsonify({"error": "Nenhum comentário coletado pra analisar."}), 404

        # Manda pro Claude com tag de plataforma no texto
        formatted = [
            {
                "comment_id": c["id"],
                "text":       f"[{c['platform'].upper()}] {c['content']}",
            }
            for c in comments if c.get("content")
        ]

        analyzer = get_analyzer()
        analysis = analyzer.analyze(formatted, profile["name"])

        # Atualiza sentiment dos comentários
        for s in analysis["sentiments"]:
            db.table("social_comments").update({
                "sentiment": s["sentiment"]
            }).eq("id", s["id"]).execute()

        # Breakdown por plataforma (lê do banco após update)
        platform_breakdown = {}
        for c in comments:
            plat = c["platform"]
            if plat not in platform_breakdown:
                platform_breakdown[plat] = {"total": 0, "positive": 0, "negative": 0, "neutral": 0}
            platform_breakdown[plat]["total"] += 1

        # Mapeia sentiment_id → sentiment
        sentiment_map = {s["id"]: s["sentiment"] for s in analysis["sentiments"]}
        for c in comments:
            sent = sentiment_map.get(c["id"])
            if sent and sent in platform_breakdown[c["platform"]]:
                platform_breakdown[c["platform"]][sent] += 1

        for plat, data in platform_breakdown.items():
            total = data["total"] or 1
            data["positive_pct"] = round(data["positive"] / total * 100, 1)
            data["negative_pct"] = round(data["negative"] / total * 100, 1)
            data["neutral_pct"]  = round(data["neutral"] / total * 100, 1)

        # Salva relatório
        summary = analysis["summary"]
        report = {
            "profile_id":           profile_id,
            "platforms_analyzed":   platforms or list(platform_breakdown.keys()),
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
            "by_platform":          platform_breakdown,
            "created_at":           datetime.now(timezone.utc).isoformat(),
        }
        db.table("analysis_reports").insert(report).execute()

        return jsonify({
            "profile_id":   profile_id,
            "name":         profile["name"],
            "summary":      summary,
            "by_platform":  platform_breakdown,
        })

    except Exception as e:
        log.exception("v2_analyze failed")
        return jsonify({"error": str(e)}), 500


# ── LEITURA UNIFICADA ─────────────────────────────────────────────────────────

@app.route("/v2/posts/<profile_id>", methods=["GET"])
@require_auth
def v2_posts(profile_id):
    """Lista posts do profile, opcionalmente filtrados por plataforma."""
    denied = guard_profile(profile_id)
    if denied: return denied

    platform = request.args.get("platform")
    limit = int(request.args.get("limit", 30))

    try:
        db = get_db()
        q = (
            db.table("social_posts")
            .select("*")
            .eq("profile_id", profile_id)
            .order("posted_at", desc=True)
            .limit(limit)
        )
        if platform:
            q = q.eq("platform", platform)

        return jsonify({"profile_id": profile_id, "posts": q.execute().data})
    except Exception as e:
        log.exception("v2_posts failed")
        return jsonify({"error": str(e)}), 500


@app.route("/v2/comments/<profile_id>", methods=["GET"])
@require_auth
def v2_comments(profile_id):
    """
    Lista comentários do profile.
    Query params: platform, sentiment, hours, limit
    """
    denied = guard_profile(profile_id)
    if denied: return denied

    platform = request.args.get("platform")
    sentiment = request.args.get("sentiment")
    hours = request.args.get("hours")
    limit = int(request.args.get("limit", 50))

    try:
        db = get_db()
        q = (
            db.table("social_comments")
            .select("*")
            .eq("profile_id", profile_id)
        )
        if platform:
            q = q.eq("platform", platform)
        if sentiment:
            q = q.eq("sentiment", sentiment)
        if hours:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=int(hours))).isoformat()
            q = q.gte("posted_at", cutoff)

        result = q.order("posted_at", desc=True).limit(limit).execute()
        return jsonify({"profile_id": profile_id, "comments": result.data})
    except Exception as e:
        log.exception("v2_comments failed")
        return jsonify({"error": str(e)}), 500


# ── SIMULADOR ─────────────────────────────────────────────────────────────────

@app.route("/simulate/scenario", methods=["POST"])
@require_auth
def simulate_scenario():
    try:
        data = request.get_json(force=True) or {}
        conteudo = data.get("conteudo", "").strip()
        if not conteudo:
            return jsonify({"erro": "Campo 'conteudo' é obrigatório."}), 400

        profile_id = data.get("profile_id")
        if profile_id:
            denied = guard_profile(profile_id)
            if denied: return denied

        n_agentes = min(int(data.get("n_agentes", 100)), 500)
        filtros = data.get("filtros")
        contexto = data.get("contexto", "")

        forecast = simular(conteudo, n_agentes=n_agentes, filtros=filtros, contexto=contexto)

        db = get_db()
        result = db.table("simulacoes").insert({
            "profile_id": profile_id,
            "user_id":    g.user_id,
            "conteudo":   conteudo,
            "n_agentes":  n_agentes,
            "filtros":    filtros,
            "contexto":   contexto,
            "forecast":   forecast,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()

        forecast["simulacao_id"] = result.data[0]["id"]
        return jsonify(forecast)
    except Exception as e:
        log.exception("simulate_scenario failed")
        return jsonify({"error": str(e)}), 500


@app.route("/simulate/history/<profile_id>", methods=["GET"])
@require_auth
def simulate_history(profile_id):
    denied = guard_profile(profile_id)
    if denied: return denied

    limit = int(request.args.get("limit", 20))
    try:
        db = get_db()
        q = (
            db.table("simulacoes")
            .select("*")
            .eq("profile_id", profile_id)
            .order("created_at", desc=True)
            .limit(limit)
        )
        if g.role != "admin":
            q = q.eq("user_id", g.user_id)
        return jsonify({"profile_id": profile_id, "simulations": q.execute().data})
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
        if g.role != "admin" and sim.get("user_id") != g.user_id:
            return jsonify({"error": "Acesso negado a esta simulação"}), 403
        return jsonify(sim)
    except Exception as e:
        log.exception("get_simulation failed")
        return jsonify({"error": str(e)}), 500


# ── WAR ROOM ──────────────────────────────────────────────────────────────────

@app.route("/warroom/threat-level", methods=["GET"])
@require_auth
def warroom_threat_level():
    """Threat level cross-platform sobre todos os profiles do user."""
    try:
        db = get_db()
        cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        cutoff_7d  = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

        q = db.table("profiles").select("*")
        if g.role != "admin":
            q = q.eq("user_id", g.user_id)
        profiles = q.execute().data or []

        if not profiles:
            return _empty_threat()

        profile_ids = [p["id"] for p in profiles]

        recent = (
            db.table("social_comments")
            .select("sentiment, platform, profile_id, posted_at")
            .in_("profile_id", profile_ids)
            .gte("posted_at", cutoff_24h)
            .execute()
        ).data or []

        baseline = (
            db.table("social_comments")
            .select("posted_at, profile_id")
            .in_("profile_id", profile_ids)
            .gte("posted_at", cutoff_7d)
            .execute()
        ).data or []

        total_24h = len(recent)
        total_7d = len(baseline) or 1
        avg_daily = total_7d / 7

        spike_factor = total_24h / avg_daily if avg_daily > 0 else 0
        has_spike = spike_factor > 1.5

        analyzed = [c for c in recent if c.get("sentiment")]
        neg_count = len([c for c in analyzed if c["sentiment"] == "negative"])
        neg_pct = (neg_count / len(analyzed) * 100) if analyzed else 0

        threat_score = 0
        if neg_pct > 60: threat_score += 40
        elif neg_pct > 40: threat_score += 25
        elif neg_pct > 25: threat_score += 12

        if has_spike: threat_score += 25
        if spike_factor > 3: threat_score += 15

        per_profile = {}
        crisis_count = 0
        for p in profiles:
            pid = p["id"]
            p_comments = [c for c in analyzed if c["profile_id"] == pid]
            if not p_comments:
                continue
            p_neg = len([c for c in p_comments if c["sentiment"] == "negative"])
            p_neg_pct = p_neg / len(p_comments) * 100
            per_profile[pid] = {
                "name":         p["name"],
                "handles":      p.get("handles", {}),
                "neg_pct":      round(p_neg_pct, 1),
                "comments_24h": len(p_comments),
                "in_crisis":    p_neg_pct > 50,
            }
            if p_neg_pct > 50:
                crisis_count += 1
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
            "crisis_profiles":  crisis_count,
            "total_profiles":   len(profiles),
            "per_profile":      per_profile,
            "calculated_at":    datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        log.exception("warroom_threat_level failed")
        return jsonify({"error": str(e)}), 500


def _empty_threat():
    return jsonify({
        "defcon": 5, "label": "ALL CLEAR", "color": "green",
        "threat_score": 0, "negative_pct_24h": 0, "comments_24h": 0,
        "spike_factor": 0, "has_spike": False, "crisis_profiles": 0,
        "total_profiles": 0, "per_profile": {},
        "calculated_at": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/warroom/negative-feed", methods=["GET"])
@require_auth
def warroom_negative_feed():
    """Feed cross-platform de comentários negativos."""
    try:
        limit = int(request.args.get("limit", 30))
        hours = int(request.args.get("hours", 24))
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

        db = get_db()
        q = db.table("profiles").select("id, name, handles")
        if g.role != "admin":
            q = q.eq("user_id", g.user_id)
        profiles = q.execute().data or []
        if not profiles:
            return jsonify({"comments": [], "total": 0})

        profile_ids = [p["id"] for p in profiles]
        name_map = {p["id"]: p["name"] for p in profiles}

        res = (
            db.table("social_comments")
            .select("*")
            .in_("profile_id", profile_ids)
            .eq("sentiment", "negative")
            .gte("posted_at", cutoff)
            .order("posted_at", desc=True)
            .limit(limit)
            .execute()
        )

        comments = res.data or []
        for c in comments:
            c["target_name"] = name_map.get(c.get("profile_id"), "?")

        return jsonify({"comments": comments, "total": len(comments)})
    except Exception as e:
        log.exception("warroom_negative_feed failed")
        return jsonify({"error": str(e)}), 500


@app.route("/warroom/generate-response", methods=["POST"])
@require_auth
def warroom_generate_response():
    try:
        from anthropic import AnthropicBedrock

        data = request.get_json(force=True) or {}
        attack = data.get("attack", "").strip()
        profile_id = data.get("profile_id")
        politician_name = data.get("politician_name", "candidato")
        context = data.get("context", "")
        simulate_flag = data.get("simulate", True)

        if not attack:
            return jsonify({"error": "Campo 'attack' é obrigatório."}), 400

        if profile_id:
            denied = guard_profile(profile_id)
            if denied: return denied

        client = AnthropicBedrock(aws_region=os.environ.get("AWS_REGION", "us-east-1"))

        prompt = f"""Você é um estrategista de comunicação política sênior numa War Room de campanha brasileira.

POLÍTICO: {politician_name}
ATAQUE RECEBIDO: "{attack}"
CONTEXTO: {context or 'campanha eleitoral em andamento'}

Gere EXATAMENTE 3 respostas estratégicas:

1. DEFENSIVA — esclarece, contextualiza, desarma sem confrontar
2. OFENSIVA — vira o jogo, ataca quem atacou
3. DESVIO — muda o assunto para pauta forte do candidato

Cada resposta: 2-3 frases curtas, formato post, autêntica em PT-BR.

Responda APENAS com JSON válido:
{{
  "respostas": [
    {{"estrategia": "defensiva", "texto": "...", "tom": "...", "risco": "baixo|medio|alto"}},
    {{"estrategia": "ofensiva", "texto": "...", "tom": "...", "risco": "..."}},
    {{"estrategia": "desvio", "texto": "...", "tom": "...", "risco": "..."}}
  ]
}}"""

        msg = client.messages.create(
            model=os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-haiku-4-5-20251001-v1:0"),
            max_tokens=1500,
            temperature=0.8,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = msg.content[0].text.strip()
        if "```" in raw:
            for part in raw.split("```"):
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                if part.startswith("{"):
                    raw = part
                    break

        start, end = raw.find("{"), raw.rfind("}") + 1
        if start != -1 and end > start:
            raw = raw[start:end]

        try:
            parsed = json.loads(repair_json(raw))
        except Exception as parse_err:
            log.error("Falha parse war room: %s | raw=%s", parse_err, raw[:300])
            return jsonify({"error": "Resposta inválida do modelo. Tente novamente."}), 502

        respostas = parsed.get("respostas", [])

        if simulate_flag and respostas:
            for r in respostas:
                try:
                    sim = simular(conteudo=r["texto"], n_agentes=30,
                                   contexto=f"resposta de {politician_name}: {attack[:100]}")
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
                    "profile_id":      profile_id,
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
            "simulated":    simulate_flag,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        log.exception("warroom_generate_response failed")
        return jsonify({"error": str(e)}), 500


# ── BUSCA DE MENÇÕES (SerpApi) ────────────────────────────────────────────────

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
            "q": query, "api_key": os.environ["SERPAPI_KEY"],
            "engine": "google", "hl": "pt", "gl": "br",
            "num": min(limit, 10), "tbs": "qdr:w",
        }, timeout=15)
        data = response.json()

        if "error" in data:
            return jsonify({"error": data["error"]}), 500

        mentions = [{
            "title":   item.get("title"),
            "source":  item.get("source") or item.get("displayed_link"),
            "url":     item.get("link"),
            "snippet": item.get("snippet"),
            "date":    item.get("date"),
        } for item in data.get("organic_results", [])]

        if mentions:
            analyzer = get_analyzer()
            formatted = [{"comment_id": str(i), "text": f"{m['title']}. {m['snippet']}"} for i, m in enumerate(mentions)]
            analysis = analyzer.analyze(formatted, query)
            for i, s in enumerate(analysis["sentiments"]):
                if i < len(mentions):
                    mentions[i]["sentiment"] = s["sentiment"]
                    mentions[i]["score"] = s["score"]

        return jsonify({"query": query, "total": len(mentions), "mentions": mentions})
    except Exception as e:
        log.exception("search_mentions failed")
        return jsonify({"error": str(e)}), 500


# ── RELATÓRIOS ────────────────────────────────────────────────────────────────

@app.route("/reports/<profile_id>", methods=["GET"])
@require_auth
def get_reports(profile_id):
    denied = guard_profile(profile_id)
    if denied: return denied

    limit = int(request.args.get("limit", 10))
    try:
        db = get_db()
        result = (
            db.table("analysis_reports")
            .select("*")
            .eq("profile_id", profile_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return jsonify({"profile_id": profile_id, "reports": result.data})
    except Exception as e:
        log.exception("get_reports failed")
        return jsonify({"error": str(e)}), 500


@app.route("/reports/latest", methods=["GET"])
@require_auth
def get_all_latest():
    try:
        db = get_db()
        if g.role == "admin":
            result = db.table("analysis_reports").select("*").order("created_at", desc=True).limit(50).execute()
        else:
            profiles = db.table("profiles").select("id").eq("user_id", g.user_id).execute().data or []
            ids = [p["id"] for p in profiles]
            if not ids:
                return jsonify({"reports": []})
            result = (
                db.table("analysis_reports")
                .select("*")
                .in_("profile_id", ids)
                .order("created_at", desc=True)
                .limit(50)
                .execute()
            )

        seen, latest = set(), []
        for r in result.data or []:
            if r["profile_id"] not in seen:
                seen.add(r["profile_id"])
                latest.append(r)
        return jsonify({"reports": latest})
    except Exception as e:
        log.exception("get_all_latest failed")
        return jsonify({"error": str(e)}), 500


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)