"""
collectors/ — Drivers de coleta multi-plataforma para o Pulse.

Cada driver implementa BaseCollector e retorna dados no formato unificado
de social_posts/social_comments.
"""
from .base import BaseCollector

__all__ = ["BaseCollector"]