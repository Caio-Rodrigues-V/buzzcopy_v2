"""
analyzer.py — Análise de sentimento híbrida:
  - HuggingFace: classifica sentimento comentário por comentário (grátis)
  - Claude: gera narrative, temas, crisis_alert e quotes (onde realmente brilha)
"""

import os
import json
import anthropic
from huggingface_hub import InferenceClient
from json_repair import repair_json

MODEL_HF     = "lxyuan/distilbert-base-multilingual-cased-sentiments-student"
MODEL_CLAUDE = "claude-haiku-4-5-20251001"


# ── PARTE 1: HUGGING FACE ─────────────────────────────────────────────────────

class SentimentClassifier:

    def __init__(self, hf_token: str):
        self.client = InferenceClient(
            provider="hf-inference",
            api_key=hf_token,
        )

    def classify(self, text: str) -> dict:
        """Classifica um único comentário."""
        try:
            result = self.client.text_classification(
                text[:512],
                model=MODEL_HF,
            )
            top = result[0]
            return {
                "label": top.label.lower(),
                "score": round(top.score, 3),
            }
        except Exception as e:
            print(f"[HF] Erro ao classificar: {e}")
            return {"label": "neutral", "score": 0.5}

    def classify_batch(self, comments: list) -> list:
        """Classifica uma lista inteira de comentários."""
        results = []
        for i, comment in enumerate(comments):
            print(f"[HF] Classificando {i+1}/{len(comments)}")
            sentiment = self.classify(comment["text"])
            results.append({
                "comment_id": comment["comment_id"],
                "text":       comment["text"],
                "label":      sentiment["label"],
                "score":      sentiment["score"],
            })
        return results


# ── PARTE 2: AGREGAÇÃO ────────────────────────────────────────────────────────

def _aggregate(classified: list) -> dict:
    """
    Agrega os resultados do HuggingFace:
    - Calcula percentuais
    - Calcula overall_score (-1 a 1)
    - Separa top comentários positivos e negativos
    """
    total = len(classified) or 1

    positives = [c for c in classified if c["label"] == "positive"]
    negatives = [c for c in classified if c["label"] == "negative"]
    neutrals  = [c for c in classified if c["label"] == "neutral"]

    pos_pct = round(len(positives) / total * 100, 1)
    neg_pct = round(len(negatives) / total * 100, 1)
    neu_pct = round(len(neutrals)  / total * 100, 1)

    score_map = {"positive": 1, "neutral": 0, "negative": -1}
    overall = sum(score_map[c["label"]] * c["score"] for c in classified) / total
    overall = round(overall, 3)

    top_pos = sorted(positives, key=lambda x: x["score"], reverse=True)[:5]
    top_neg = sorted(negatives, key=lambda x: x["score"], reverse=True)[:5]

    return {
        "positive_pct":  pos_pct,
        "negative_pct":  neg_pct,
        "neutral_pct":   neu_pct,
        "overall_score": overall,
        "total":         total,
        "top_positives": [c["text"] for c in top_pos],
        "top_negatives": [c["text"] for c in top_neg],
    }


# ── PARTE 3: CLAUDE ───────────────────────────────────────────────────────────

PROMPT_CLAUDE = """\
Você é um analista de reputação política especializado em redes sociais brasileiras.

Com base nos dados de sentimento abaixo, gere um relatório executivo.
Retorne SOMENTE um JSON válido, sem texto extra, markdown ou explicação.

Estrutura esperada:
{{
  "main_themes":        ["tema1", "tema2", "tema3"],
  "crisis_alert":       false,
  "crisis_reason":      null,
  "top_positive_quote": "comentário positivo mais representativo",
  "top_negative_quote": "comentário negativo mais representativo",
  "narrative":          "Resumo executivo em 2-3 frases para um assessor político."
}}

Regras:
- crisis_alert = true se sentimento negativo > 60% ou houver ataque coordenado
- main_themes: os 3-5 temas mais recorrentes nos comentários
- narrative: em português, tom executivo, para assessoria política

PERFIL: {profile_name}

DADOS DE SENTIMENTO:
- Positivo: {positive_pct}%
- Neutro:   {neutral_pct}%
- Negativo: {negative_pct}%
- Score geral: {overall_score}
- Total de comentários: {total}

COMENTÁRIOS MAIS POSITIVOS:
{top_positives}

COMENTÁRIOS MAIS NEGATIVOS:
{top_negatives}
"""


class NarrativeGenerator:

    def __init__(self, api_key: str):
        self.client = anthropic.Anthropic(api_key=api_key)

    def generate(self, aggregated: dict, profile_name: str) -> dict:
        """Manda os dados agregados pro Claude e recebe o relatório narrativo."""
        prompt = PROMPT_CLAUDE.format(
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
        print(f"[Claude RAW] {raw[:300]}")

        # Remove markdown se vier
        if "```" in raw:
            parts = raw.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                if part.startswith("{"):
                    raw = part
                    break

        # Extrai só o JSON
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start != -1 and end > start:
            raw = raw[start:end]

        return json.loads(repair_json(raw))


# ── PARTE 4: ORQUESTRADOR PRINCIPAL ──────────────────────────────────────────

class SentimentAnalyzer:

    def __init__(self, anthropic_key: str, hf_token: str):
        self.classifier = SentimentClassifier(hf_token)
        self.generator  = NarrativeGenerator(anthropic_key)

    def analyze(self, comments: list, profile_name: str) -> dict:
        """
        Pipeline completo:
        1. HuggingFace classifica cada comentário
        2. Agrega os resultados
        3. Claude gera narrative, temas e crisis_alert
        4. Retorna o relatório completo
        """
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

        # 1. HuggingFace classifica todos
        print(f"[HF] Classificando {len(comments)} comentários...")
        classified = self.classifier.classify_batch(comments)

        # 2. Agrega os resultados
        aggregated = _aggregate(classified)
        print(f"[HF] Positivo: {aggregated['positive_pct']}% | Negativo: {aggregated['negative_pct']}% | Score: {aggregated['overall_score']}")

        # 3. Claude gera o relatório narrativo
        print(f"[Claude] Gerando relatório narrativo...")
        narrative = self.generator.generate(aggregated, profile_name)

        # 4. Monta o retorno final no mesmo formato de antes
        sentiments = [
            {
                "id":        c["comment_id"],
                "sentiment": c["label"],
                "score":     c["score"] if c["label"] == "positive" else -c["score"] if c["label"] == "negative" else 0,
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