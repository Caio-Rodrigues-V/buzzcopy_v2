"""
analyzer.py — Análise de sentimento com Claude:
  - Claude: classifica cada caption em relação ao perfil (alta precisão)
  - Claude: gera narrative, temas, crisis_alert e quotes
"""

import json
import anthropic
from json_repair import repair_json

MODEL_CLAUDE = "claude-haiku-4-5-20251001"

PROMPT_CLASSIFY = """\
Você é um analista de reputação política especializado em redes sociais brasileiras.

Analise as captions abaixo e classifique o sentimento de cada uma em relação ao perfil "{profile_name}".

Regras CRÍTICAS:
- Classifique APENAS o sentimento direcionado a "{profile_name}" como pessoa/figura pública
- Se a caption fala sobre um assunto/tema mas NÃO expressa opinião sobre "{profile_name}", classifique como "neutral"
- Se há ironia ou crítica implícita a "{profile_name}", classifique como "negative"
- Se há elogio, apoio ou defesa de "{profile_name}", classifique como "positive"

Retorne SOMENTE um JSON válido, sem texto extra ou markdown:
[
  {{"id": "id_do_post", "sentiment": "positive" | "negative" | "neutral"}},
  ...
]

CAPTIONS PARA ANALISAR:
{captions}
"""

PROMPT_NARRATIVE = """\
Você é um analista de reputação política especializado em redes sociais brasileiras.

Com base nos dados de sentimento abaixo, gere um relatório executivo.
Retorne SOMENTE um JSON válido, sem texto extra, markdown ou explicação.

Estrutura esperada:
{{
  "main_themes":        ["tema1", "tema2", "tema3"],
  "crisis_alert":       false,
  "crisis_reason":      null,
  "top_positive_quote": "caption positiva mais representativa",
  "top_negative_quote": "caption negativa mais representativa",
  "narrative":          "Resumo executivo em 2-3 frases para um assessor político."
}}

Regras:
- crisis_alert = true se sentimento negativo > 60% ou houver ataque coordenado
- main_themes: os 3-5 temas mais recorrentes nas captions
- narrative: escreva em português, tom executivo. Use EXATAMENTE os percentuais fornecidos.
- Analise sentimento direcionado ESPECIFICAMENTE a "{profile_name}", ignore opiniões sobre temas.

PERFIL: {profile_name}

DADOS DE SENTIMENTO:
- Positivo: {positive_pct}%
- Neutro:   {neutral_pct}%
- Negativo: {negative_pct}%
- Score geral: {overall_score}
- Total de captions: {total}

CAPTIONS MAIS POSITIVAS:
{top_positives}

CAPTIONS MAIS NEGATIVAS:
{top_negatives}
"""


class SentimentAnalyzer:

    def __init__(self, anthropic_key: str, hf_token: str = None):
        self.client = anthropic.Anthropic(api_key=anthropic_key)

    def _classify(self, comments: list, profile_name: str) -> list:
        """Claude classifica todas as captions de uma vez."""
        captions_text = "\n".join(
            f'ID: {c["comment_id"]}\nCaption: {c["text"]}\n'
            for c in comments
        )

        prompt = PROMPT_CLASSIFY.format(
            profile_name=profile_name,
            captions=captions_text,
        )

        response = self.client.messages.create(
            model=MODEL_CLAUDE,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = response.content[0].text.strip()
        print(f"[Claude Classify RAW] {raw[:300]}")

        # Limpa markdown se vier
        if "```" in raw:
            parts = raw.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                if part.startswith("["):
                    raw = part
                    break

        start = raw.find("[")
        end = raw.rfind("]") + 1
        if start != -1 and end > start:
            raw = raw[start:end]

        return json.loads(repair_json(raw))

    def _aggregate(self, classified: list, comments: list) -> dict:
        """Agrega os resultados da classificação."""
        total = len(classified) or 1

        comment_map = {c["comment_id"]: c["text"] for c in comments}

        positives = [c for c in classified if c["sentiment"] == "positive"]
        negatives = [c for c in classified if c["sentiment"] == "negative"]
        neutrals  = [c for c in classified if c["sentiment"] == "neutral"]

        pos_pct = round(len(positives) / total * 100, 1)
        neg_pct = round(len(negatives) / total * 100, 1)
        neu_pct = round(len(neutrals)  / total * 100, 1)

        overall = round((len(positives) - len(negatives)) / total, 3)

        top_positives = [comment_map.get(c["id"], "") for c in positives[:5]]
        top_negatives = [comment_map.get(c["id"], "") for c in negatives[:5]]

        return {
            "positive_pct":  pos_pct,
            "negative_pct":  neg_pct,
            "neutral_pct":   neu_pct,
            "overall_score": overall,
            "total":         total,
            "top_positives": [t for t in top_positives if t],
            "top_negatives": [t for t in top_negatives if t],
            "classified":    classified,
        }

    def _generate_narrative(self, aggregated: dict, profile_name: str) -> dict:
        """Claude gera o relatório narrativo."""
        prompt = PROMPT_NARRATIVE.format(
            profile_name=profile_name,
            positive_pct=aggregated["positive_pct"],
            neutral_pct=aggregated["neutral_pct"],
            negative_pct=aggregated["negative_pct"],
            overall_score=aggregated["overall_score"],
            total=aggregated["total"],
            top_positives="\n".join(f"- {t}" for t in aggregated["top_positives"]) or "Nenhum",
            top_negatives="\n".join(f"- {t}" for t in aggregated["top_negatives"]) or "Nenhum",
        )

        response = self.client.messages.create(
            model=MODEL_CLAUDE,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = response.content[0].text.strip()
        print(f"[Claude Narrative RAW] {raw[:300]}")

        if "```" in raw:
            parts = raw.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                if part.startswith("{"):
                    raw = part
                    break

        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start != -1 and end > start:
            raw = raw[start:end]

        return json.loads(repair_json(raw))

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

        print(f"[Claude] Classificando {len(comments)} captions para: {profile_name}")
        classified = self._classify(comments, profile_name)

        aggregated = self._aggregate(classified, comments)
        print(f"[Claude] Positivo: {aggregated['positive_pct']}% | Negativo: {aggregated['negative_pct']}% | Score: {aggregated['overall_score']}")

        print(f"[Claude] Gerando relatório narrativo...")
        narrative = self._generate_narrative(aggregated, profile_name)

        sentiments = [
            {
                "id":        c["id"],
                "sentiment": c["sentiment"],
                "score":     1.0 if c["sentiment"] == "positive" else -1.0 if c["sentiment"] == "negative" else 0.0,
            }
            for c in classified
        ]

        return {
            "sentiments": sentiments,
            "summary": {
                "positive_pct":       aggregated["positive_pct"],
                "negative_pct":       aggregated["negative_pct"],
                "neutral_pct":        aggregated["neutral_pct"],
                "overall_score":      aggregated["overall_score"],
                "main_themes":        narrative.get("main_themes", []),
                "crisis_alert":       narrative.get("crisis_alert", False),
                "crisis_reason":      narrative.get("crisis_reason", None),
                "top_positive_quote": narrative.get("top_positive_quote", ""),
                "top_negative_quote": narrative.get("top_negative_quote", ""),
                "narrative":          narrative.get("narrative", ""),
                "comments_analyzed":  aggregated["total"],
            },
        }