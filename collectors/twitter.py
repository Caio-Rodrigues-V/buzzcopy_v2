"""
collectors/twitter.py — Coleta tweets e replies via Apify.

Usa o ator apidojo/tweet-scraper:
  https://apify.com/apidojo/tweet-scraper
"""
import os
import logging
import requests
from typing import List, Dict

from .base import BaseCollector

log = logging.getLogger("pulse.collector.twitter")

APIFY_BASE = "https://api.apify.com/v2/acts"
ACTOR_ID = "apidojo~tweet-scraper"


class TwitterCollector(BaseCollector):
    platform = "twitter"

    def __init__(self, apify_token: str = None):
        self.token = apify_token or os.environ["APIFY_TOKEN"]

    # ── POSTS (tweets do perfil) ─────────────────────────────────────

    def collect_posts(self, handle: str, profile_id: str, limit: int = 50) -> List[Dict]:
        """
        Coleta tweets recentes de um perfil Twitter.

        handle: username sem @ (ex: 'jairbolsonaro', 'LulaOficial')
        """
        url = f"{APIFY_BASE}/{ACTOR_ID}/run-sync-get-dataset-items?token={self.token}"

        payload = {
            "twitterHandles":   [handle],
            "maxItems":         limit,
            "sort":             "Latest",
            "tweetLanguage":    "pt",
            "includeSearchTerms": False,
            "onlyImage":        False,
            "onlyQuote":        False,
            "onlyTwitterBlue":  False,
            "onlyVerifiedUsers": False,
            "onlyVideo":        False,
        }

        try:
            response = requests.post(url, json=payload, timeout=180)
            tweets = response.json()
        except Exception:
            log.exception("Falha ao coletar tweets de @%s", handle)
            return []

        if not isinstance(tweets, list):
            log.warning("Resposta inesperada Apify Twitter: %s", str(tweets)[:300])
            return []

        rows = []
        for t in tweets:
            tweet_id = t.get("id") or t.get("id_str") or t.get("tweetId")
            if not tweet_id:
                continue

            author = t.get("author") or {}
            author_username = (
                author.get("userName")
                or author.get("screen_name")
                or t.get("user", {}).get("screen_name")
                or handle
            )
            author_name = author.get("name") or t.get("user", {}).get("name")

            rows.append(self._build_post(
                post_id=f"tw_{tweet_id}",
                profile_id=profile_id,
                author_username=author_username,
                author_name=author_name,
                content=t.get("text") or t.get("full_text") or "",
                url=t.get("url") or t.get("twitterUrl"),
                post_type="tweet" if not t.get("isRetweet") else "retweet",
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

        log.info("Twitter: %s tweets coletados de @%s", len(rows), handle)
        return rows

    # ── COMENTÁRIOS (replies) ─────────────────────────────────────────

    def collect_reactions(
        self,
        handle: str,
        profile_id: str,
        posts_limit: int = 10,
        reactions_per_post: int = 20,
    ) -> List[Dict]:
        """
        Coleta replies aos tweets recentes do perfil.

        Estratégia: pega os tweets mais recentes do perfil no Supabase
        (já coletados pelo collect_posts) e busca replies de cada um.
        """
        from supabase import create_client
        db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

        recent = (
            db.table("social_posts")
            .select("id, external_id, url")
            .eq("platform", "twitter")
            .eq("author_username", handle)
            .order("posted_at", desc=True)
            .limit(posts_limit)
            .execute()
        ).data or []

        if not recent:
            log.warning("Nenhum tweet em social_posts pra @%s", handle)
            return []

        tweet_ids = [p["external_id"] for p in recent if p.get("external_id")]
        ext_to_post_id = {p["external_id"]: p["id"] for p in recent}

        url = f"{APIFY_BASE}/{ACTOR_ID}/run-sync-get-dataset-items?token={self.token}"

        # O ator suporta busca de conversas por tweet ID via conversationIds
        payload = {
            "conversationIds":  tweet_ids,
            "maxItems":         reactions_per_post * len(tweet_ids),
            "sort":             "Latest",
            "tweetLanguage":    "pt",
        }

        try:
            response = requests.post(url, json=payload, timeout=300)
            replies = response.json()
        except Exception:
            log.exception("Falha ao coletar replies Twitter de @%s", handle)
            return []

        if not isinstance(replies, list):
            log.warning("Resposta inesperada Apify (replies): %s", str(replies)[:300])
            return []

        rows = []
        for r in replies:
            reply_id = r.get("id") or r.get("tweetId")
            if not reply_id:
                continue

            # Ignora se o próprio perfil é quem replyou (não é "reação", é continuação)
            author = r.get("author") or {}
            author_un = author.get("userName") or author.get("screen_name") or ""
            if author_un.lower() == handle.lower():
                continue

            # Liga ao tweet pai
            parent_id = (
                r.get("inReplyToId")
                or r.get("conversationId")
                or r.get("in_reply_to_status_id")
            )
            post_id = ext_to_post_id.get(str(parent_id)) if parent_id else None

            rows.append(self._build_comment(
                comment_id=f"tw_{reply_id}",
                post_id=post_id,
                profile_id=profile_id,
                author_username=author_un,
                content=r.get("text") or r.get("full_text") or "",
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

        log.info("Twitter: %s replies coletados de @%s", len(rows), handle)
        return rows