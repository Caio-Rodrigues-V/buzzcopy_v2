"""
analyzer.py — Análise de sentimento e geração de relatório via Claude API
"""

import json
import anthropic

MODEL = "claude-haiku-4-5-20251001"
BATCH_SIZE = 200


PROMPT_TEMPLATE = """\
Você é um analista de reputação política especializado em redes sociais brasileiras.

Analise os comentários do YouTube abaixo referentes ao político/perfil indicado.
Retorne SOMENTE um JSON válido, sem texto extra, markdown ou explicação.

Estrutura esperada:
{{
  "sentiments": [
    {{"id": "comment_id", "sentiment": "positive|negative|neutral", "score": 0.85}}
  ],
  "summary": {{
    "positive_pct":       45.0,
    "negative_pct":       35.0,
    "neutral_pct":        20.0,
    "overall_score":      0.1,
    "main_themes":        ["tema1", "tema2", "tema3"],
    "crisis_alert":       false,
    "crisis_reason":      null,
    "top_positive_quote": "trecho positivo representativo",
    "top_negative_quote": "trecho negativo representativo",
    "narrative":          "Resumo executivo em 2-3 frases sobre o sentimento geral do período."
  }}
}}

Regras:
- score vai de -1.0 (muito negativo) a 1.0 (muito positivo)
- overall_score é a média ponderada de todos os comentários
- crisis_alert = true se houver ataque coordenado, escândalo ou sentimento negativo > 60%
- main_themes: os 3-5 temas mais recorrentes nos comentários
- narrative: escreva em português, de forma executiva, para um assessor político

PERFIL MONITORADO: {profile_name}

COMENTÁRIOS ({total} comentários):
{comments}
"""


class SentimentAnalyzer:

    def __init__(self, api_key: str):
        self.client = anthropic.Anthropic(api_key=api_key)

    def _analyze_batch(self, comments: list, profile_name: str) -> dict:
        comments_text = "\n".join(
        f"[{c['comment_id']}] {c['text'][:150]}"
        for c in comments
        )

        prompt = PROMPT_TEMPLATE.format(
            profile_name=profile_name,
            total=len(comments),
            comments=comments_text,
        )

        response = self.client.messages.create(
            model=MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = response.content[0].text.strip()

        # Remove blocos markdown
        if "```" in raw:
            parts = raw.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                if part.startswith("{"):
                    raw = part
                    break

        # Extrai só o JSON ignorando texto antes/depois
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start != -1 and end > start:
            raw = raw[start:end]

        return json.loads(raw)

    def _merge_batches(self, batch_results: list, total_comments: int) -> dict:
        all_sentiments = []
        for b in batch_results:
            all_sentiments.extend(b.get("sentiments", []))

        pos = sum(1 for s in all_sentiments if s["sentiment"] == "positive")
        neg = sum(1 for s in all_sentiments if s["sentiment"] == "negative")
        neu = sum(1 for s in all_sentiments if s["sentiment"] == "neutral")
        total = len(all_sentiments) or 1
        overall = sum(s["score"] for s in all_sentiments) / total

        all_themes = []
        for b in batch_results:
            all_themes.extend(b["summary"].get("main_themes", []))
        seen, unique_themes = set(), []
        for t in all_themes:
            if t.lower() not in seen:
                seen.add(t.lower())
                unique_themes.append(t)

        crisis_alert = any(b["summary"].get("crisis_alert") for b in batch_results)
        crisis_reason = next(
            (b["summary"].get("crisis_reason") for b in batch_results if b["summary"].get("crisis_reason")),
            None,
        )
        narrative = batch_results[-1]["summary"].get("narrative", "")

        return {
            "sentiments": all_sentiments,
            "summary": {
                "positive_pct":       round(pos / total * 100, 1),
                "negative_pct":       round(neg / total * 100, 1),
                "neutral_pct":        round(neu / total * 100, 1),
                "overall_score":      round(overall, 3),
                "main_themes":        unique_themes[:5],
                "crisis_alert":       crisis_alert,
                "crisis_reason":      crisis_reason,
                "top_positive_quote": batch_results[0]["summary"].get("top_positive_quote", ""),
                "top_negative_quote": batch_results[0]["summary"].get("top_negative_quote", ""),
                "narrative":          narrative,
                "comments_analyzed":  len(all_sentiments),
            },
        }

    def analyze(self, comments: list, profile_name: str) -> dict:
        if not comments:
            return {
                "sentiments": [],
                "summary": {
                    "positive_pct": 0, "negative_pct": 0, "neutral_pct": 0,
                    "overall_score": 0, "main_themes": [],
                    "crisis_alert": False, "crisis_reason": None,
                    "top_positive_quote": "", "top_negative_quote": "",
                    "narrative": "Nenhum comentário coletado neste período.",
                    "comments_analyzed": 0,
                },
            }

        batches = [
            comments[i: i + BATCH_SIZE]
            for i in range(0, len(comments), BATCH_SIZE)
        ]

        print(f"[Claude] Analisando {len(comments)} comentários em {len(batches)} batch(es)...")

        results = []
        for i, batch in enumerate(batches):
            print(f"[Claude]  └─ Batch {i+1}/{len(batches)} ({len(batch)} comentários)")
            result = self._analyze_batch(batch, profile_name)
            results.append(result)

        if len(results) == 1:
            return results[0]

        return self._merge_batches(results, len(comments))