"""
collector.py — Coleta dados de canais e vídeos via YouTube Data API v3
"""

from googleapiclient.discovery import build
from datetime import datetime, timedelta, timezone


class YouTubeCollector:

    def __init__(self, api_key: str):
        self.youtube = build("youtube", "v3", developerKey=api_key)

    # ── CHANNEL ───────────────────────────────────────────────────────────────

    def get_channel_info(self, channel_id: str) -> dict:
        """Retorna informações básicas e estatísticas do canal."""
        response = self.youtube.channels().list(
            part="snippet,statistics",
            id=channel_id
        ).execute()

        if not response.get("items"):
            raise ValueError(f"Canal não encontrado: {channel_id}")

        item    = response["items"][0]
        stats   = item["statistics"]
        snippet = item["snippet"]

        return {
            "channel_id":       channel_id,
            "name":             snippet["title"],
            "description":      snippet.get("description", "")[:500],
            "subscriber_count": int(stats.get("subscriberCount", 0)),
            "video_count":      int(stats.get("videoCount", 0)),
            "total_views":      int(stats.get("viewCount", 0)),
            "thumbnail":        snippet["thumbnails"].get("high", {}).get("url", ""),
            "collected_at":     datetime.now(timezone.utc).isoformat(),
        }

    # ── VIDEOS ────────────────────────────────────────────────────────────────

    def get_recent_videos(self, channel_id: str, days: int = 30, max_results: int = 15) -> list:
        """Retorna vídeos publicados nos últimos N dias."""
        published_after = (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        search_resp = self.youtube.search().list(
            part="snippet",
            channelId=channel_id,
            type="video",
            order="date",
            publishedAfter=published_after,
            maxResults=min(max_results, 50),
        ).execute()

        video_ids = [item["id"]["videoId"] for item in search_resp.get("items", [])]
        if not video_ids:
            return []

        videos_resp = self.youtube.videos().list(
            part="snippet,statistics",
            id=",".join(video_ids),
        ).execute()

        videos = []
        for item in videos_resp.get("items", []):
            s = item["statistics"]
            sn = item["snippet"]
            videos.append({
                "video_id":      item["id"],
                "title":         sn["title"],
                "published_at":  sn["publishedAt"],
                "view_count":    int(s.get("viewCount", 0)),
                "like_count":    int(s.get("likeCount", 0)),
                "comment_count": int(s.get("commentCount", 0)),
                "thumbnail":     sn["thumbnails"].get("high", {}).get("url", ""),
                "url":           f"https://youtube.com/watch?v={item['id']}",
            })

        return sorted(videos, key=lambda x: x["published_at"], reverse=True)

    # ── COMMENTS ──────────────────────────────────────────────────────────────

    def get_video_comments(self, video_id: str, max_comments: int = 100) -> list:
        """Retorna os comentários mais relevantes de um vídeo."""
        try:
            response = self.youtube.commentThreads().list(
                part="snippet",
                videoId=video_id,
                order="relevance",
                maxResults=min(max_comments, 100),
                textFormat="plainText",
            ).execute()
        except Exception:
            # Comentários desativados ou vídeo restrito
            return []

        comments = []
        for item in response.get("items", []):
            c = item["snippet"]["topLevelComment"]["snippet"]
            text = c["textDisplay"].strip()
            if len(text) < 5:     # ignora comentários vazios
                continue
            comments.append({
                "comment_id":   item["id"],
                "text":         text[:500],   # limita tamanho
                "author":       c["authorDisplayName"],
                "like_count":   c.get("likeCount", 0),
                "published_at": c["publishedAt"],
                "video_id":     video_id,
            })

        return comments

    # ── FULL PROFILE ──────────────────────────────────────────────────────────

    def collect_full_profile(self, channel_id: str, days: int = 30) -> dict:
        """
        Coleta tudo de um canal: info, vídeos recentes e comentários.
        Retorna um dict pronto para passar ao analisador.
        """
        print(f"[YouTube] Coletando canal: {channel_id}")

        channel_info = self.get_channel_info(channel_id)
        videos       = self.get_recent_videos(channel_id, days=days)

        print(f"[YouTube] {len(videos)} vídeos nos últimos {days} dias")

        all_comments = []
        for video in videos[:5]:          # máx 10 vídeos para poupar quota
            comments = self.get_video_comments(video["video_id"])
            for c in comments:
                c["video_title"] = video["title"]
            all_comments.extend(comments)
            print(f"[YouTube]  └─ '{video['title'][:45]}' — {len(comments)} comentários")

        # Métricas agregadas do período
        total_views    = sum(v["view_count"] for v in videos)
        total_likes    = sum(v["like_count"] for v in videos)
        total_comments = sum(v["comment_count"] for v in videos)

        engagement_rate = (
            round((total_likes + total_comments) / total_views * 100, 2)
            if total_views > 0 else 0.0
        )

        return {
            "channel":    channel_info,
            "period_days": days,
            "videos":     videos,
            "comments":   all_comments,
            "metrics": {
                "total_videos":           len(videos),
                "total_views":            total_views,
                "total_likes":            total_likes,
                "total_comments_channel": total_comments,
                "comments_collected":     len(all_comments),
                "engagement_rate_pct":    engagement_rate,
            },
        }
