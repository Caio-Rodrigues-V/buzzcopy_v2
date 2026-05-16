"""
collectors/youtube.py — Coleta YouTube via Data API v3, formato unificado.
"""
import os
import logging
from typing import List, Dict
from googleapiclient.discovery import build
from datetime import datetime, timedelta, timezone

from .base import BaseCollector

log = logging.getLogger("pulse.collector.youtube")


class YouTubeCollector(BaseCollector):
    platform = "youtube"

    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.environ["YOUTUBE_API_KEY"]
        self.youtube = build("youtube", "v3", developerKey=self.api_key)

    def collect_posts(self, handle: str, profile_id: str, limit: int = 50) -> List[Dict]:
        """handle = channel_id (UCxxx)."""
        days = 30
        published_after = (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            search_resp = self.youtube.search().list(
                part="snippet",
                channelId=handle,
                type="video",
                order="date",
                publishedAfter=published_after,
                maxResults=min(limit, 50),
            ).execute()
        except Exception:
            log.exception("Falha buscando vídeos do canal %s", handle)
            return []

        video_ids = [item["id"]["videoId"] for item in search_resp.get("items", [])]
        if not video_ids:
            return []

        try:
            videos_resp = self.youtube.videos().list(
                part="snippet,statistics",
                id=",".join(video_ids),
            ).execute()
        except Exception:
            log.exception("Falha buscando stats dos vídeos")
            return []

        rows = []
        for item in videos_resp.get("items", []):
            s = item.get("statistics", {})
            sn = item.get("snippet", {})
            video_id = item["id"]
            rows.append(self._build_post(
                post_id=f"yt_{video_id}",
                profile_id=profile_id,
                author_username=sn.get("channelTitle") or handle,
                author_name=sn.get("channelTitle"),
                content=f'{sn.get("title", "")}\n\n{sn.get("description", "")[:500]}',
                url=f"https://youtube.com/watch?v={video_id}",
                post_type="video",
                posted_at=sn.get("publishedAt"),
                metrics={
                    "views":    int(s.get("viewCount", 0)),
                    "likes":    int(s.get("likeCount", 0)),
                    "comments": int(s.get("commentCount", 0)),
                },
                external_id=video_id,
            ))
        log.info("YouTube: %s vídeos coletados de %s", len(rows), handle)
        return rows

    def collect_reactions(
        self,
        handle: str,
        profile_id: str,
        posts_limit: int = 10,
        reactions_per_post: int = 20,
    ) -> List[Dict]:
        from supabase import create_client
        db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

        recent = (
            db.table("social_posts")
            .select("id, external_id")
            .eq("platform", "youtube")
            .eq("profile_id", profile_id)
            .order("posted_at", desc=True)
            .limit(posts_limit)
            .execute()
        ).data or []

        if not recent:
            return []

        rows = []
        for v in recent:
            video_id = v["external_id"]
            try:
                resp = self.youtube.commentThreads().list(
                    part="snippet",
                    videoId=video_id,
                    order="relevance",
                    maxResults=min(reactions_per_post, 100),
                    textFormat="plainText",
                ).execute()
            except Exception:
                continue  # comentários desativados

            for item in resp.get("items", []):
                c = item["snippet"]["topLevelComment"]["snippet"]
                text = (c.get("textDisplay") or "").strip()
                if len(text) < 5:
                    continue
                rows.append(self._build_comment(
                    comment_id=f"yt_c_{item['id']}",
                    post_id=v["id"],
                    profile_id=profile_id,
                    author_username=c.get("authorDisplayName") or "",
                    content=text[:500],
                    posted_at=c.get("publishedAt"),
                    metrics={"likes": c.get("likeCount", 0)},
                    external_id=item["id"],
                ))
        log.info("YouTube: %s comentários coletados (%s vídeos) do canal %s",
                 len(rows), len(recent), handle)
        return rows