"""
collectors/instagram.py — Coleta Instagram via Apify, formato unificado.
"""
import os
import logging
import requests
from typing import List, Dict

from .base import BaseCollector

log = logging.getLogger("pulse.collector.instagram")

APIFY_BASE = "https://api.apify.com/v2/acts"


class InstagramCollector(BaseCollector):
    platform = "instagram"

    def __init__(self, apify_token: str = None):
        self.token = apify_token or os.environ["APIFY_TOKEN"]

    # ── POSTS ─────────────────────────────────────────────────────────

    def collect_posts(self, handle: str, profile_id: str, limit: int = 50) -> List[Dict]:
        url = f"{APIFY_BASE}/apify~instagram-scraper/run-sync-get-dataset-items?token={self.token}"
        payload = {
            "directUrls":    [f"https://www.instagram.com/{handle}/"],
            "resultsType":   "posts",
            "resultsLimit":  limit,
        }

        try:
            response = requests.post(url, json=payload, timeout=120)
            posts = response.json()
        except Exception as e:
            log.exception("Falha ao coletar Instagram %s", handle)
            return []

        if not isinstance(posts, list):
            log.warning("Resposta inesperada Apify Instagram: %s", str(posts)[:200])
            return []

        rows = []
        for p in posts:
            if not p.get("id"):
                continue
            rows.append(self._build_post(
                post_id=p["id"],
                profile_id=profile_id,
                author_username=p.get("ownerUsername") or handle,
                content=p.get("caption") or "",
                url=p.get("url"),
                post_type=(p.get("type") or "post").lower(),
                posted_at=p.get("timestamp"),
                hashtags=p.get("hashtags", []),
                metrics={
                    "likes":    p.get("likesCount") or 0,
                    "comments": p.get("commentsCount") or 0,
                    "views":    p.get("videoViewCount") or 0,
                },
            ))
        log.info("Instagram: %s posts coletados de @%s", len(rows), handle)
        return rows

    # ── COMENTÁRIOS ───────────────────────────────────────────────────

    def collect_reactions(
        self,
        handle: str,
        profile_id: str,
        posts_limit: int = 10,
        reactions_per_post: int = 20,
    ) -> List[Dict]:
        from supabase import create_client
        db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

        # Busca posts recentes desse handle já em social_posts
        recent = (
            db.table("social_posts")
            .select("id, url")
            .eq("platform", "instagram")
            .eq("author_username", handle)
            .not_.is_("url", "null")
            .order("posted_at", desc=True)
            .limit(posts_limit)
            .execute()
        ).data or []

        if not recent:
            log.warning("Nenhum post Instagram em social_posts pra @%s", handle)
            return []

        urls = [p["url"] for p in recent if p.get("url")]
        url_to_post_id = {p["url"]: p["id"] for p in recent}

        scraper_url = f"{APIFY_BASE}/apify~instagram-comment-scraper/run-sync-get-dataset-items?token={self.token}"
        payload = {
            "directUrls":    urls,
            "resultsLimit":  reactions_per_post,
        }

        try:
            response = requests.post(scraper_url, json=payload, timeout=300)
            comments = response.json()
        except Exception:
            log.exception("Falha ao coletar comentários Instagram de @%s", handle)
            return []

        if not isinstance(comments, list):
            return []

        rows = []
        for c in comments:
            if not c.get("id"):
                continue
            post_url = c.get("postUrl") or c.get("url", "")
            rows.append(self._build_comment(
                comment_id=str(c["id"]),
                post_id=url_to_post_id.get(post_url),
                profile_id=profile_id,
                author_username=c.get("ownerUsername") or "",
                content=c.get("text") or "",
                metrics={"likes": c.get("likesCount") or 0},
                posted_at=c.get("timestamp"),
            ))
        log.info("Instagram: %s comentários coletados de @%s", len(rows), handle)
        return rows