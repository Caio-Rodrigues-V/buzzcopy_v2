"""
collectors/twitter.py — Coleta tweets e replies via Apify (otimizado, 1 call).

Usa o ator apidojo/twitter-profile-scraper com includeTweetReplies=True numa
única chamada, e separa posts vs replies localmente em Python.
"""
import os
import logging
import requests
from typing import List, Dict, Tuple
from datetime import datetime, timedelta, timezone

from .base import BaseCollector

log = logging.getLogger("pulse.collector.twitter")

APIFY_BASE = "https://api.apify.com/v2/acts"
ACTOR_ID = "apidojo~twitter-profile-scraper"


class TwitterCollector(BaseCollector):
    platform = "twitter"

    def __init__(self, apify_token: str = None):
        self.token = apify_token or os.environ["APIFY_TOKEN"]
        # Cache do último run pra evitar chamar 2x quando collect_posts
        # e collect_reactions são disparados na mesma request
        self._cache: Dict[str, Tuple[float, List[Dict]]] = {}
        self._cache_ttl = 60  # segundos

    # ── HELPER: 1 chamada ao Apify, retorna tudo ──────────────────────

    def _fetch_all(self, handle: str, total_items: int) -> List[Dict]:
        """Chama o ator UMA vez com replies=True e cacheia por 60s."""
        cache_key = f"{handle}:{total_items}"
        now = datetime.now(timezone.utc).timestamp()

        # Cache hit (se collect_reactions vier logo depois de collect_posts)
        if cache_key in self._cache:
            ts, data = self._cache[cache_key]
            if now - ts < self._cache_ttl:
                log.info("Twitter: cache hit pra @%s (%s items)", handle, len(data))
                return data

        start = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
        end = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        payload = {
            "twitterHandles":        [handle],
            "startDate":             start,
            "endDate":               end,
            "maxItems":              total_items,
            "includeNativeRetweets": False,
            "onlyImages":            False,
            "includeTweetReplies":   True,
            "minReplyCount":         0,
        }

        url = f"{APIFY_BASE}/{ACTOR_ID}/run-sync-get-dataset-items?token={self.token}"
        try:
            response = requests.post(url, json=payload, timeout=300)
            data = response.json()
        except Exception:
            log.exception("Falha chamando ator Twitter")
            return []

        if not isinstance(data, list):
            log.warning("Resposta inesperada Apify Twitter: %s", str(data)[:300])
            return []

        self._cache[cache_key] = (now, data)
        log.info("Twitter: %s items retornados pra @%s (posts + replies)", len(data), handle)
        return data

    # ── POSTS (tweets originais do perfil) ───────────────────────────

    def collect_posts(self, handle: str, profile_id: str, limit: int = 30) -> List[Dict]:
        """
        Coleta tweets do perfil. Pede 1x ao Apify com replies juntas e
        filtra só os tweets originais (autor = handle, sem reply parent).
        """
        # Pede limite generoso pra ter posts + replies suficientes
        total_items = limit * 10
        items = self._fetch_all(handle, total_items)
        handle_lower = handle.lower()

        rows = []
        for t in items:
            tweet_id = t.get("id") or t.get("tweetId") or t.get("id_str")
            if not tweet_id:
                continue

            author = t.get("author") or {}
            author_username = (
                author.get("userName")
                or author.get("screen_name")
                or t.get("user", {}).get("screen_name")
                or ""
            )

            # Só tweets do próprio handle, e não-replies
            if author_username.lower() != handle_lower:
                continue
            is_reply = bool(
                t.get("inReplyToId")
                or t.get("in_reply_to_status_id")
                or t.get("inReplyToStatusId")
            )
            if is_reply:
                continue

            rows.append(self._build_post(
                post_id=f"tw_{tweet_id}",
                profile_id=profile_id,
                author_username=author_username,
                author_name=author.get("name") or t.get("user", {}).get("name"),
                content=t.get("text") or t.get("full_text") or "",
                url=t.get("url") or t.get("twitterUrl") or f"https://x.com/{author_username}/status/{tweet_id}",
                post_type="retweet" if t.get("isRetweet") else "tweet",
                posted_at=t.get("createdAt") or t.get("created_at"),
                hashtags=[h.get("text") if isinstance(h, dict) else h
                          for h in (t.get("hashtags") or [])],
                metrics={
                    "likes":     t.get("likeCount")    or t.get("favorite_count") or 0,
                    "retweets":  t.get("retweetCount") or 0,
                    "replies":   t.get("replyCount")   or 0,
                    "quotes":    t.get("quoteCount")   or 0,
                    "views":     t.get("viewCount")    or 0,
                    "bookmarks": t.get("bookmarkCount") or 0,
                },
                external_id=str(tweet_id),
            ))

            if len(rows) >= limit:
                break

        log.info("Twitter: %s tweets originais filtrados de @%s", len(rows), handle)
        return rows

    # ── COMENTÁRIOS (replies aos tweets) ──────────────────────────────

    def collect_reactions(
        self,
        handle: str,
        profile_id: str,
        posts_limit: int = 10,
        reactions_per_post: int = 20,
    ) -> List[Dict]:
        """
        Lê do CACHE do collect_posts (mesma run do Apify, 0 chamadas extras).
        Se cache vazio, faz 1 chamada.
        """
        from supabase import create_client
        db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

        recent_posts = (
            db.table("social_posts")
            .select("id, external_id")
            .eq("platform", "twitter")
            .eq("profile_id", profile_id)
            .order("posted_at", desc=True)
            .limit(posts_limit)
            .execute()
        ).data or []

        ext_to_post_id = {p["external_id"]: p["id"] for p in recent_posts if p.get("external_id")}

        if not ext_to_post_id:
            log.warning("Nenhum tweet em social_posts pra @%s — rode collect_posts primeiro", handle)
            return []

        # Mesma chamada cacheada de collect_posts
        total_items = posts_limit * 10
        items = self._fetch_all(handle, total_items)
        handle_lower = handle.lower()

        rows = []
        for r in items:
            reply_id = r.get("id") or r.get("tweetId") or r.get("id_str")
            if not reply_id:
                continue

            author = r.get("author") or {}
            author_username = (
                author.get("userName")
                or author.get("screen_name")
                or ""
            )

            # Skip se é do próprio dono (não é reply de outro)
            if author_username.lower() == handle_lower:
                continue

            text = r.get("text") or r.get("full_text") or ""
            if not text.strip():
                continue

            parent_id = (
                r.get("inReplyToId")
                or r.get("conversationId")
                or r.get("in_reply_to_status_id")
                or r.get("inReplyToStatusId")
            )
            post_id = ext_to_post_id.get(str(parent_id)) if parent_id else None

            # Só guarda se for reply a um tweet que coletamos
            if not post_id:
                continue

            rows.append(self._build_comment(
                comment_id=f"tw_r_{reply_id}",
                post_id=post_id,
                profile_id=profile_id,
                author_username=author_username,
                content=text,
                posted_at=r.get("createdAt") or r.get("created_at"),
                metrics={
                    "likes":    r.get("likeCount")    or 0,
                    "retweets": r.get("retweetCount") or 0,
                    "replies":  r.get("replyCount")   or 0,
                    "views":    r.get("viewCount")    or 0,
                },
                reply_to_id=str(parent_id) if parent_id else None,
                external_id=str(reply_id),
            ))

        log.info("Twitter: %s replies filtrados de @%s", len(rows), handle)
        return rows