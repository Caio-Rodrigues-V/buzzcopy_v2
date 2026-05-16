"""
collectors/base.py — Interface comum pra todos os coletores de fontes.

Toda fonte (Instagram, Twitter, News, etc.) implementa essa interface.
Retorna dicts no formato unificado de social_posts e social_comments.
"""
from abc import ABC, abstractmethod
from typing import List, Dict, Optional
from datetime import datetime, timezone


class BaseCollector(ABC):
    """Contrato pra qualquer fonte de dados."""

    platform: str = ""  # 'instagram', 'twitter', 'news', etc.

    @abstractmethod
    def collect_posts(self, handle: str, profile_id: str, limit: int = 50) -> List[Dict]:
        """
        Coleta posts/conteúdo principal de um perfil/canal/fonte.

        Args:
            handle: identificador na plataforma (username, channel_id, query, etc.)
            profile_id: UUID do profile no Supabase (pra vincular)
            limit: máximo de itens a coletar

        Returns:
            Lista de dicts no formato social_posts.
        """
        raise NotImplementedError

    @abstractmethod
    def collect_reactions(
        self,
        handle: str,
        profile_id: str,
        posts_limit: int = 10,
        reactions_per_post: int = 20,
    ) -> List[Dict]:
        """
        Coleta reações (comentários/replies) dos posts coletados.

        Returns:
            Lista de dicts no formato social_comments.
        """
        raise NotImplementedError

    # ── Helpers compartilhados ────────────────────────────────────────

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _build_post(
        self,
        post_id: str,
        profile_id: str,
        author_username: str,
        content: str,
        url: Optional[str] = None,
        post_type: Optional[str] = None,
        posted_at: Optional[str] = None,
        metrics: Optional[Dict] = None,
        hashtags: Optional[List[str]] = None,
        author_name: Optional[str] = None,
        source_domain: Optional[str] = None,
        external_id: Optional[str] = None,
    ) -> Dict:
        """Constrói um dict de social_posts no formato canônico."""
        return {
            "id":               post_id,
            "profile_id":       profile_id,
            "platform":         self.platform,
            "external_id":      external_id or post_id,
            "author_username":  author_username,
            "author_name":      author_name,
            "content":          content,
            "post_type":        post_type or "post",
            "url":              url,
            "source_domain":    source_domain,
            "metrics":          metrics or {},
            "hashtags":         hashtags or [],
            "posted_at":        posted_at,
        }

    def _build_comment(
        self,
        comment_id: str,
        post_id: Optional[str],
        profile_id: str,
        author_username: str,
        content: str,
        metrics: Optional[Dict] = None,
        posted_at: Optional[str] = None,
        reply_to_id: Optional[str] = None,
        external_id: Optional[str] = None,
    ) -> Dict:
        """Constrói um dict de social_comments no formato canônico."""
        return {
            "id":               comment_id,
            "post_id":          post_id,
            "profile_id":       profile_id,
            "platform":         self.platform,
            "external_id":      external_id or comment_id,
            "author_username":  author_username,
            "content":          content,
            "metrics":          metrics or {},
            "reply_to_id":      reply_to_id,
            "posted_at":        posted_at,
        }