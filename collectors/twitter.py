"""
collectors/twitter.py — Coleta tweets + replies via 2 atores Apify.

Atores:
  - apidojo/tweet-scraper           → tweets do perfil ($0.40 / 1000 tweets)
  - scraper_one/x-post-replies-scraper → replies por post URL (pay per event)

Fluxo:
  1. collect_posts: usa apidojo/tweet-scraper com handle, devolve N tweets com URL
  2. collect_reactions: lê URLs do banco, chama scraper_one pra cada batch
"""
import os
import logging
import requests
from typing import List, Dict
from datetime import datetime, timezone

from .base import BaseCollector

log = logging.getLogger("pulse.collector.twitter")

APIFY_BASE = "https://api.apify.com/v2/acts"
ACTOR_TWEETS  = "apidojo~tweet-scraper"
ACTOR_REPLIES = "scraper_one~x-post-replies-scraper"


class TwitterCollector(BaseCollector):
    platform = "twitter"

    def __init__(self, apify_token: str = None):
        self.token = apify_token or os.environ["APIFY_TOKEN"]

    # ── HELPER ────────────────────────────────────────────────────────

def _run_actor(self, actor_id: str, payload: Dict, timeout: int = 300) -> List[Dict]:
    url = f"{APIFY_BASE}/{actor_id}/run-sync-get-dataset-items?token={self.token}"
    try:
        response = requests.post(url, json=payload, timeout=timeout)
        data = response.json()
    except Exception:
        log.exception("Falha chamando ator %s", actor_id)
        return []

    if not isinstance(data, list):
        log.warning("Resposta inesperada do %s: %s", actor_id, str(data)[:300])
        return []

    # Detecta resposta "demo" (twitter-profile-scraper bugado)
    if data and all(item.get("demo") is True for item in data):
        log.error("Ator %s retornou apenas dados demo (token/ator com bug).", actor_id)
        return []

    # Detecta resposta "noResults" (tweet-scraper sem matches do filtro)
    if data and all(item.get("noResults") is True for item in data):
        log.error(
            "Ator %s retornou apenas 'noResults' — filtros muito restritivos "
            "(idioma errado, sem tweets no período, perfil bloqueado). "
            "Cobrança AINDA foi feita pelo Apify.",
            actor_id,
        )
        return []

    return data
    # ── POSTS (tweets originais) ─────────────────────────────────────

    def collect_posts(self, handle: str, profile_id: str, limit: int = 30) -> List[Dict]:
        """
        Coleta tweets via apidojo/tweet-scraper.
        handle: username sem @ (ex: 'LulaOficial')
        """
        payload = {
            "twitterHandles": [handle],
            "maxItems":       limit,
            "sort":           "Latest",
            "tweetLanguage":  "pt",
        }

        items = self._run_actor(ACTOR_TWEETS, payload)
        if not items:
            return []

        rows = []
        handle_lower = handle.lower()

        for t in items:
            tweet_id = t.get("id") or t.get("tweetId") or t.get("id_str")
            if not tweet_id:
                continue

            author = t.get("author") or {}
            author_username = (
                author.get("userName")
                or author.get("screen_name")
                or t.get("user", {}).get("screen_name")
                or handle
            )

            # Filtra só tweets do próprio handle
            if author_username.lower() != handle_lower:
                continue

            # Skip retweets nativos
            if t.get("isRetweet") or t.get("retweeted_status"):
                continue

            rows.append(self._build_post(
                post_id=f"tw_{tweet_id}",
                profile_id=profile_id,
                author_username=author_username,
                author_name=author.get("name") or t.get("user", {}).get("name"),
                content=t.get("text") or t.get("full_text") or "",
                url=t.get("url") or t.get("twitterUrl") or f"https://x.com/{author_username}/status/{tweet_id}",
                post_type="tweet",
                posted_at=t.get("createdAt") or t.get("created_at"),
                hashtags=[h.get("text") if isinstance(h, dict) else h
                          for h in (t.get("hashtags") or [])],
                metrics={
                    "likes":     t.get("likeCount")     or 0,
                    "retweets":  t.get("retweetCount")  or 0,
                    "replies":   t.get("replyCount")    or 0,
                    "quotes":    t.get("quoteCount")    or 0,
                    "views":     t.get("viewCount")     or 0,
                    "bookmarks": t.get("bookmarkCount") or 0,
                },
                external_id=str(tweet_id),
            ))

        log.info("Twitter: %s tweets coletados de @%s", len(rows), handle)
        return rows

    # ── REPLIES (comments dos tweets) ────────────────────────────────

    def collect_reactions(
        self,
        handle: str,
        profile_id: str,
        posts_limit: int = 10,
        reactions_per_post: int = 20,
    ) -> List[Dict]:
        """
        Coleta replies dos tweets já salvos via scraper_one/x-post-replies-scraper.
        """
        from supabase import create_client
        db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

        # Pega URLs dos tweets recentes desse profile
        recent_posts = (
            db.table("social_posts")
            .select("id, external_id, url")
            .eq("platform", "twitter")
            .eq("profile_id", profile_id)
            .not_.is_("url", "null")
            .order("posted_at", desc=True)
            .limit(posts_limit)
            .execute()
        ).data or []

        if not recent_posts:
            log.warning("Nenhum tweet em social_posts pra @%s — rode collect_posts primeiro", handle)
            return []

        urls = [p["url"] for p in recent_posts if p.get("url")]
        # Mapa external_id (raw tweet ID, sem prefixo tw_) → row id no banco
        ext_to_post_id = {p["external_id"]: p["id"] for p in recent_posts if p.get("external_id")}

        if not urls:
            return []

        payload = {
            "postUrls":     urls,
            "resultsLimit": reactions_per_post,
        }

        items = self._run_actor(ACTOR_REPLIES, payload, timeout=600)
        if not items:
            return []

        rows = []
        for r in items:
            reply_id = r.get("replyId") or r.get("id")
            if not reply_id:
                continue

            text = r.get("replyText") or r.get("text") or ""
            if not text.strip():
                continue

            author = r.get("author") or {}
            author_username = (
                author.get("screenName")
                or author.get("userName")
                or author.get("screen_name")
                or ""
            )

            parent_id = r.get("inReplyTo") or r.get("postId") or r.get("conversationId")
            post_id = ext_to_post_id.get(str(parent_id)) if parent_id else None

            # Converte timestamp ms pra ISO
            ts = r.get("timestamp")
            posted_at = None
            if ts:
                try:
                    posted_at = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()
                except Exception:
                    posted_at = None

            rows.append(self._build_comment(
                comment_id=f"tw_r_{reply_id}",
                post_id=post_id,
                profile_id=profile_id,
                author_username=author_username,
                content=text,
                posted_at=posted_at,
                metrics={
                    "likes":    r.get("favouriteCount") or 0,
                    "retweets": r.get("repostCount")    or 0,
                    "replies":  r.get("replyCount")     or 0,
                    "quotes":   r.get("quoteCount")     or 0,
                    "views":    int(r.get("viewsCount") or 0) if str(r.get("viewsCount") or "").isdigit() else 0,
                },
                reply_to_id=str(parent_id) if parent_id else None,
                external_id=str(reply_id),
            ))

        log.info("Twitter: %s replies coletados de @%s (%s posts)", len(rows), handle, len(urls))
        return rows