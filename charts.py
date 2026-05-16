"""
charts.py — 4 endpoints de agregação, todos por profile_id e lendo de social_*.
Cross-platform por padrão; filtra por platform via query param.
"""
import os
import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from flask import jsonify, request
from supabase import create_client

from auth import require_auth, guard_profile

log = logging.getLogger("pulse.charts")

_db_client = None


def _db():
    global _db_client
    if _db_client is None:
        _db_client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    return _db_client


def register_chart_routes(app):

    @app.route("/charts/sentiment-timeline/<profile_id>", methods=["GET"])
    @require_auth
    def sentiment_timeline(profile_id):
        denied = guard_profile(profile_id)
        if denied: return denied

        days = int(request.args.get("days", 30))
        platform = request.args.get("platform")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        try:
            db = _db()
            q = (
                db.table("social_comments")
                .select("sentiment, platform, posted_at")
                .eq("profile_id", profile_id)
                .gte("posted_at", cutoff)
                .not_.is_("sentiment", "null")
            )
            if platform:
                q = q.eq("platform", platform)

            buckets = defaultdict(lambda: defaultdict(lambda: {"positive": 0, "negative": 0, "neutral": 0}))
            for c in q.execute().data or []:
                if not c.get("posted_at"):
                    continue
                day = c["posted_at"][:10]
                plat = c["platform"]
                s = c.get("sentiment", "neutral")
                if s in buckets[plat][day]:
                    buckets[plat][day][s] += 1

            # Estrutura: por plataforma e total
            timeline_by_platform = {}
            all_days = set()
            for plat, days_data in buckets.items():
                series = []
                for day in sorted(days_data.keys()):
                    b = days_data[day]
                    total = b["positive"] + b["negative"] + b["neutral"] or 1
                    series.append({
                        "date":         day,
                        "positive":     b["positive"],
                        "negative":     b["negative"],
                        "neutral":      b["neutral"],
                        "total":        total,
                        "score":        round((b["positive"] - b["negative"]) / total, 3),
                        "negative_pct": round(b["negative"] / total * 100, 1),
                    })
                    all_days.add(day)
                timeline_by_platform[plat] = series

            # Timeline total (soma todas plataformas por dia)
            total_by_day = defaultdict(lambda: {"positive": 0, "negative": 0, "neutral": 0})
            for plat_data in buckets.values():
                for day, vals in plat_data.items():
                    for k, v in vals.items():
                        total_by_day[day][k] += v

            timeline_total = []
            for day in sorted(total_by_day.keys()):
                b = total_by_day[day]
                total = b["positive"] + b["negative"] + b["neutral"] or 1
                timeline_total.append({
                    "date":         day,
                    "positive":     b["positive"],
                    "negative":     b["negative"],
                    "neutral":      b["neutral"],
                    "total":        total,
                    "score":        round((b["positive"] - b["negative"]) / total, 3),
                    "negative_pct": round(b["negative"] / total * 100, 1),
                })

            return jsonify({
                "profile_id": profile_id,
                "days":       days,
                "timeline":   timeline_total,
                "by_platform": timeline_by_platform,
            })
        except Exception as e:
            log.exception("sentiment_timeline failed")
            return jsonify({"error": str(e)}), 500


    @app.route("/charts/engagement-by-type/<profile_id>", methods=["GET"])
    @require_auth
    def engagement_by_type(profile_id):
        denied = guard_profile(profile_id)
        if denied: return denied

        platform = request.args.get("platform")

        try:
            db = _db()
            q = (
                db.table("social_posts")
                .select("post_type, platform, metrics")
                .eq("profile_id", profile_id)
            )
            if platform:
                q = q.eq("platform", platform)

            grouped = defaultdict(lambda: {"posts": 0, "likes": 0, "comments": 0, "views": 0, "shares": 0})
            for p in q.execute().data or []:
                t = (p.get("post_type") or "unknown").lower()
                m = p.get("metrics") or {}
                grouped[t]["posts"]    += 1
                grouped[t]["likes"]    += int(m.get("likes") or 0)
                grouped[t]["comments"] += int(m.get("comments") or 0)
                grouped[t]["views"]    += int(m.get("views") or 0)
                grouped[t]["shares"]   += int(m.get("retweets") or m.get("shares") or 0)

            breakdown = []
            for ptype, d in grouped.items():
                n = d["posts"]
                breakdown.append({
                    "post_type":      ptype,
                    "post_count":     n,
                    "total_likes":    d["likes"],
                    "total_comments": d["comments"],
                    "total_views":    d["views"],
                    "avg_likes":      round(d["likes"] / n, 1) if n else 0,
                    "avg_comments":   round(d["comments"] / n, 1) if n else 0,
                    "avg_engagement": round((d["likes"] + d["comments"]) / n, 1) if n else 0,
                })
            breakdown.sort(key=lambda x: x["avg_engagement"], reverse=True)
            return jsonify({"profile_id": profile_id, "breakdown": breakdown})
        except Exception as e:
            log.exception("engagement_by_type failed")
            return jsonify({"error": str(e)}), 500


    @app.route("/charts/peak-hours/<profile_id>", methods=["GET"])
    @require_auth
    def peak_hours(profile_id):
        denied = guard_profile(profile_id)
        if denied: return denied

        platform = request.args.get("platform")

        try:
            db = _db()
            q = (
                db.table("social_comments")
                .select("posted_at, metrics, platform")
                .eq("profile_id", profile_id)
                .not_.is_("posted_at", "null")
            )
            if platform:
                q = q.eq("platform", platform)

            matrix = [[{"count": 0, "likes": 0} for _ in range(24)] for _ in range(7)]
            weekday_labels = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]

            for c in q.execute().data or []:
                try:
                    dt = datetime.fromisoformat(c["posted_at"].replace("Z", "+00:00"))
                    dt_br = dt - timedelta(hours=3)
                    wd, h = dt_br.weekday(), dt_br.hour
                    matrix[wd][h]["count"] += 1
                    matrix[wd][h]["likes"] += int((c.get("metrics") or {}).get("likes") or 0)
                except Exception:
                    continue

            heatmap = []
            for wd in range(7):
                for h in range(24):
                    cell = matrix[wd][h]
                    heatmap.append({
                        "weekday":       wd,
                        "weekday_label": weekday_labels[wd],
                        "hour":          h,
                        "count":         cell["count"],
                        "likes":         cell["likes"],
                        "score":         cell["count"] + cell["likes"] * 0.1,
                    })
            peaks = sorted(heatmap, key=lambda x: x["score"], reverse=True)[:5]
            return jsonify({"profile_id": profile_id, "heatmap": heatmap, "top_peaks": peaks})
        except Exception as e:
            log.exception("peak_hours failed")
            return jsonify({"error": str(e)}), 500


    @app.route("/charts/themes/<profile_id>", methods=["GET"])
    @require_auth
    def themes(profile_id):
        denied = guard_profile(profile_id)
        if denied: return denied

        days = int(request.args.get("days", 30))
        platform = request.args.get("platform")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        try:
            db = _db()

            # Temas dos relatórios desse profile
            reports_res = (
                db.table("analysis_reports")
                .select("main_themes, created_at")
                .eq("profile_id", profile_id)
                .gte("created_at", cutoff)
                .execute()
            )

            theme_counter = Counter()
            for r in reports_res.data or []:
                for theme in (r.get("main_themes") or []):
                    if theme:
                        theme_counter[theme.lower().strip()] += 1
            top_themes = [{"theme": t, "count": c} for t, c in theme_counter.most_common(15)]

            # Word frequency dos comentários
            q = (
                db.table("social_comments")
                .select("content")
                .eq("profile_id", profile_id)
                .gte("posted_at", cutoff)
                .limit(2000)
            )
            if platform:
                q = q.eq("platform", platform)
            comments_res = q.execute()

            stopwords = {
                "a","o","e","de","da","do","das","dos","em","no","na","nos","nas","para","pra",
                "por","com","que","se","um","uma","uns","umas","os","as","mais","menos","muito",
                "ja","já","ai","aí","ta","tá","ne","né","eh","é","ou","sem","sob","sobre","entre",
                "ate","até","quando","onde","como","porque","pois","mas","também","tambem","só","so",
                "essa","esse","esses","essas","isso","isto","aquele","aquela","aqui","ali","la","lá",
                "eu","tu","ele","ela","nós","nos","eles","elas","você","voce","vocês","voces",
                "meu","minha","seu","sua","nosso","nossa","dele","dela","deles","delas",
                "está","esta","estão","estao","tem","têm","tinha","será","sera","foi","foram","era","eram",
                "são","sao","https","http","www","tudo","nada","sim","não","nao","pelo","pela",
            }

            word_counter = Counter()
            for c in comments_res.data or []:
                text = (c.get("content") or "").lower()
                for word in text.split():
                    word = "".join(ch for ch in word if ch.isalpha())
                    if len(word) >= 4 and word not in stopwords:
                        word_counter[word] += 1
            word_cloud = [{"word": w, "count": c} for w, c in word_counter.most_common(50)]

            return jsonify({
                "profile_id": profile_id,
                "days":       days,
                "top_themes": top_themes,
                "word_cloud": word_cloud,
            })
        except Exception as e:
            log.exception("themes failed")
            return jsonify({"error": str(e)}), 500