"""
charts.py — 4 endpoints de agregação pro dashboard.
Todos protegidos por JWT + checagem de ownership.
"""
import os
import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from flask import jsonify, request, g
from supabase import create_client

from auth import require_auth, user_owns_profile

log = logging.getLogger("pulse.charts")

# ── SINGLETON (Fix #9) ────────────────────────────────────────────────────────
_db_client = None


def _db():
    global _db_client
    if _db_client is None:
        _db_client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    return _db_client


def _guard(username):
    if g.role == "admin":
        return None
    if not user_owns_profile(username, g.user_id):
        return jsonify({"error": "Acesso negado a este perfil"}), 403
    return None


def register_chart_routes(app):

    @app.route("/charts/sentiment-timeline/<username>", methods=["GET"])
    @require_auth
    def sentiment_timeline(username):
        denied = _guard(username)
        if denied: return denied

        days = int(request.args.get("days", 30))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        try:
            db = _db()
            res = (
                db.table("instagram_comments")
                .select("sentiment, timestamp")
                .eq("owner_username", username)
                .gte("timestamp", cutoff)
                .not_.is_("sentiment", "null")
                .execute()
            )

            buckets = defaultdict(lambda: {"positive": 0, "negative": 0, "neutral": 0})
            for c in res.data or []:
                if not c.get("timestamp"):
                    continue
                day = c["timestamp"][:10]
                s = c.get("sentiment", "neutral")
                if s in buckets[day]:
                    buckets[day][s] += 1

            timeline = []
            for day in sorted(buckets.keys()):
                b = buckets[day]
                total = b["positive"] + b["negative"] + b["neutral"] or 1
                timeline.append({
                    "date": day,
                    "positive": b["positive"],
                    "negative": b["negative"],
                    "neutral": b["neutral"],
                    "total": total,
                    "score": round((b["positive"] - b["negative"]) / total, 3),
                    "negative_pct": round(b["negative"] / total * 100, 1),
                })

            return jsonify({"username": username, "days": days, "timeline": timeline})
        except Exception as e:
            log.exception("sentiment_timeline failed")
            return jsonify({"error": str(e)}), 500


    @app.route("/charts/engagement-by-type/<username>", methods=["GET"])
    @require_auth
    def engagement_by_type(username):
        denied = _guard(username)
        if denied: return denied

        try:
            db = _db()
            res = (
                db.table("instagram_posts")
                .select("post_type, likes_count, comments_count, video_view_count")
                .eq("owner_username", username)
                .execute()
            )

            grouped = defaultdict(lambda: {"posts": 0, "likes": 0, "comments": 0, "views": 0})
            for p in res.data or []:
                t = (p.get("post_type") or "unknown").lower()
                grouped[t]["posts"] += 1
                grouped[t]["likes"] += p.get("likes_count") or 0
                grouped[t]["comments"] += p.get("comments_count") or 0
                grouped[t]["views"] += p.get("video_view_count") or 0

            breakdown = []
            for ptype, d in grouped.items():
                n = d["posts"]
                breakdown.append({
                    "post_type": ptype,
                    "post_count": n,
                    "total_likes": d["likes"],
                    "total_comments": d["comments"],
                    "total_views": d["views"],
                    "avg_likes": round(d["likes"] / n, 1) if n else 0,
                    "avg_comments": round(d["comments"] / n, 1) if n else 0,
                    "avg_engagement": round((d["likes"] + d["comments"]) / n, 1) if n else 0,
                })

            breakdown.sort(key=lambda x: x["avg_engagement"], reverse=True)
            return jsonify({"username": username, "breakdown": breakdown})
        except Exception as e:
            log.exception("engagement_by_type failed")
            return jsonify({"error": str(e)}), 500


    @app.route("/charts/peak-hours/<username>", methods=["GET"])
    @require_auth
    def peak_hours(username):
        denied = _guard(username)
        if denied: return denied

        try:
            db = _db()
            res = (
                db.table("instagram_comments")
                .select("timestamp, likes_count")
                .eq("owner_username", username)
                .not_.is_("timestamp", "null")
                .execute()
            )

            matrix = [[{"count": 0, "likes": 0} for _ in range(24)] for _ in range(7)]
            weekday_labels = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]

            for c in res.data or []:
                try:
                    dt = datetime.fromisoformat(c["timestamp"].replace("Z", "+00:00"))
                    dt_br = dt - timedelta(hours=3)
                    wd = dt_br.weekday()
                    h = dt_br.hour
                    matrix[wd][h]["count"] += 1
                    matrix[wd][h]["likes"] += c.get("likes_count") or 0
                except Exception:
                    continue

            heatmap = []
            for wd in range(7):
                for h in range(24):
                    cell = matrix[wd][h]
                    heatmap.append({
                        "weekday": wd,
                        "weekday_label": weekday_labels[wd],
                        "hour": h,
                        "count": cell["count"],
                        "likes": cell["likes"],
                        "score": cell["count"] + cell["likes"] * 0.1,
                    })

            peaks = sorted(heatmap, key=lambda x: x["score"], reverse=True)[:5]
            return jsonify({"username": username, "heatmap": heatmap, "top_peaks": peaks})
        except Exception as e:
            log.exception("peak_hours failed")
            return jsonify({"error": str(e)}), 500


    @app.route("/charts/themes/<username>", methods=["GET"])
    @require_auth
    def themes(username):
        denied = _guard(username)
        if denied: return denied

        days = int(request.args.get("days", 30))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        try:
            db = _db()

            reports_res = (
                db.table("analysis_reports")
                .select("main_themes, created_at")
                .ilike("channel_id", f"%{username}%")
                .gte("created_at", cutoff)
                .execute()
            )

            theme_counter = Counter()
            for r in reports_res.data or []:
                for theme in (r.get("main_themes") or []):
                    if theme:
                        theme_counter[theme.lower().strip()] += 1

            top_themes = [{"theme": t, "count": c} for t, c in theme_counter.most_common(15)]

            comments_res = (
                db.table("instagram_comments")
                .select("text")
                .eq("owner_username", username)
                .gte("timestamp", cutoff)
                .limit(2000)
                .execute()
            )

            stopwords = {
                "a","o","e","de","da","do","das","dos","em","no","na","nos","nas","para","pra",
                "por","com","que","se","um","uma","uns","umas","os","as","mais","menos","muito",
                "ja","já","ai","aí","ta","tá","ne","né","eh","é","ou","sem","sob","sobre","entre",
                "ate","até","quando","onde","como","porque","pois","mas","também","tambem","só","so",
                "essa","esse","esses","essas","isso","isto","aquele","aquela","aqui","ali","la","lá",
                "eu","tu","ele","ela","nós","nos","eles","elas","você","voce","vocês","voces",
                "meu","minha","seu","sua","nosso","nossa","dele","dela","deles","delas",
                "está","esta","estão","estao","tem","têm","tinha","será","sera","foi","foram","era","eram",
                "são","sao","https","http","www","com","tudo","nada","sim","não","nao","pelo","pela",
            }

            word_counter = Counter()
            for c in comments_res.data or []:
                text = (c.get("text") or "").lower()
                for word in text.split():
                    word = "".join(ch for ch in word if ch.isalpha())
                    if len(word) >= 4 and word not in stopwords:
                        word_counter[word] += 1

            word_cloud = [{"word": w, "count": c} for w, c in word_counter.most_common(50)]

            return jsonify({
                "username": username,
                "days": days,
                "top_themes": top_themes,
                "word_cloud": word_cloud,
            })
        except Exception as e:
            log.exception("themes failed")
            return jsonify({"error": str(e)}), 500