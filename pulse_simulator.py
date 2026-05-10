"""
Engine de simulação multi-agente do Pulse.
Inspirado em MiroFish, simplificado pra rodar com Claude Haiku.
"""

import json
import asyncio
import os
from anthropic import AsyncAnthropic
from typing import List, Dict
from collections import Counter
from personas_br import gerar_lote

client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

PROMPT_REACAO = """Você é um eleitor brasileiro com o seguinte perfil:

ESPECTRO POLÍTICO: {espectro}
REGIÃO: {regiao}
CLASSE SOCIAL: {classe}
IDADE: {idade}
RELIGIÃO: {religiao}
CARACTERÍSTICAS: {traits}
LINGUAGEM TÍPICA: {linguagem}
CONTEXTO: {contexto_demografico}

Você acabou de ver o seguinte conteúdo político nas redes sociais:

---
{conteudo}
---

CONTEXTO ADICIONAL: {contexto_cenario}

Reaja como esse eleitor reagiria de verdade. Responda APENAS com JSON válido, sem texto antes ou depois:

{{
  "sentimento": "positivo" | "negativo" | "neutro" | "raiva" | "indignacao" | "apoio" | "deboche",
  "intensidade": 1-10,
  "vai_compartilhar": true | false,
  "vai_comentar": true | false,
  "comentario_provavel": "como esse eleitor escreveria nos comments (use a linguagem típica dele)",
  "muda_voto": "sim" | "nao" | "reforça_voto_atual" | "afasta",
  "tema_central_percebido": "qual tema o eleitor entendeu como principal",
  "risco_viralizacao": 1-10
}}"""


async def _simular_agente(persona: Dict, conteudo: str, contexto_cenario: str) -> Dict:
    """Roda um agente individual via Claude Haiku."""
    prompt = PROMPT_REACAO.format(
        espectro=persona["espectro"],
        regiao=persona["regiao"],
        classe=persona["classe"],
        idade=persona["idade"],
        religiao=persona["religiao"],
        traits=", ".join(persona["traits"]),
        linguagem=persona["linguagem"],
        contexto_demografico=persona["contexto_demografico"],
        conteudo=conteudo,
        contexto_cenario=contexto_cenario or "Nenhum contexto adicional.",
    )

    try:
        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        # Limpa code fences se vier
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        reacao = json.loads(raw.strip())
        reacao["persona"] = persona
        return reacao
    except Exception as e:
        return {"erro": str(e), "persona": persona}


async def _executar_simulacao(personas: List[Dict], conteudo: str, contexto: str) -> List[Dict]:
    """Roda todos os agentes em paralelo."""
    tasks = [_simular_agente(p, conteudo, contexto) for p in personas]
    return await asyncio.gather(*tasks)


def _agregar_resultados(reacoes: List[Dict]) -> Dict:
    """Transforma N reações individuais em forecast agregado."""
    validas = [r for r in reacoes if "erro" not in r]
    total = len(validas)

    if total == 0:
        return {"erro": "Nenhuma reação válida foi gerada."}

    # Distribuição de sentimento
    sentimentos = Counter(r["sentimento"] for r in validas)
    sentimento_pct = {k: round(v / total * 100, 1) for k, v in sentimentos.items()}

    # Engajamento
    vai_compartilhar = sum(1 for r in validas if r.get("vai_compartilhar"))
    vai_comentar = sum(1 for r in validas if r.get("vai_comentar"))

    # Impacto eleitoral
    impacto_voto = Counter(r["muda_voto"] for r in validas)

    # Viralização
    riscos = [r.get("risco_viralizacao", 0) for r in validas]
    risco_viral_medio = round(sum(riscos) / len(riscos), 1) if riscos else 0

    # Clusters por espectro
    clusters = {}
    for r in validas:
        esp = r["persona"]["espectro"]
        if esp not in clusters:
            clusters[esp] = {"sentimentos": [], "intensidade_media": 0, "comentarios": []}
        clusters[esp]["sentimentos"].append(r["sentimento"])
        clusters[esp]["intensidade_media"] += r.get("intensidade", 0)
        if r.get("comentario_provavel"):
            clusters[esp]["comentarios"].append(r["comentario_provavel"])

    for esp, data in clusters.items():
        n = len(data["sentimentos"])
        data["intensidade_media"] = round(data["intensidade_media"] / n, 1) if n else 0
        data["sentimento_dominante"] = Counter(data["sentimentos"]).most_common(1)[0][0]
        data["amostra_comentarios"] = data["comentarios"][:3]
        del data["comentarios"]
        del data["sentimentos"]

    # Temas percebidos
    temas = Counter(r.get("tema_central_percebido", "indefinido") for r in validas)

    # Crisis Score: 0-100
    negativos = sentimento_pct.get("negativo", 0) + sentimento_pct.get("raiva", 0) + sentimento_pct.get("indignacao", 0)
    crisis_score = round(min(100, negativos * 0.7 + risco_viral_medio * 3), 1)

    return {
        "total_agentes": total,
        "sentimento_distribuicao": sentimento_pct,
        "engajamento": {
            "compartilhamento_pct": round(vai_compartilhar / total * 100, 1),
            "comentario_pct": round(vai_comentar / total * 100, 1),
        },
        "impacto_voto": dict(impacto_voto),
        "risco_viralizacao_medio": risco_viral_medio,
        "crisis_score": crisis_score,
        "clusters_por_espectro": clusters,
        "temas_percebidos": dict(temas.most_common(5)),
        "amostra_comentarios": [r.get("comentario_provavel") for r in validas[:10] if r.get("comentario_provavel")],
    }


def simular(conteudo: str, n_agentes: int = 100, filtros: Dict = None, contexto: str = "") -> Dict:
    """
    Função pública. Simula reação a um conteúdo político.

    Args:
        conteudo: post, fala, peça publicitária a ser testado
        n_agentes: quantos eleitores simular (50-500 recomendado)
        filtros: recorte demográfico, ex: {'regiao': 'nordeste'}
        contexto: contexto adicional do cenário (ex: "candidato em campanha pra prefeitura")

    Returns:
        forecast agregado
    """
    personas = gerar_lote(n_agentes, filtros)
    if not personas:
        return {"erro": "Nenhuma persona gerada com esses filtros."}

    reacoes = asyncio.run(_executar_simulacao(personas, conteudo, contexto))
    forecast = _agregar_resultados(reacoes)
    forecast["filtros_aplicados"] = filtros or "nenhum (amostra nacional)"
    forecast["conteudo_testado"] = conteudo
    return forecast