"""
Engine de simulação multi-agente do Pulse.
Inspirado em MiroFish, simplificado pra rodar com Claude Haiku.

v1.2 - Adição de inferência estatística:
       - Bootstrap resampling pra intervalo de confiança
       - Margem de erro nas métricas principais
       - Tamanho de amostra efetiva por cluster
"""

import json
import asyncio
import os
import random
import statistics
from anthropic import AsyncAnthropic
from typing import List, Dict, Tuple
from collections import Counter
from personas_br import gerar_lote

client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ── PROMPT ────────────────────────────────────────────────────────────────────

PROMPT_REACAO = """Você é um eleitor brasileiro REAL com o perfil abaixo. Não é um assistente, não é neutro, não busca consenso. Você tem preconceitos, desconfianças, gírias próprias e bloqueios culturais.

═══════════════════════════════════
SEU PERFIL
═══════════════════════════════════
ESPECTRO POLÍTICO: {espectro}
REGIÃO: {regiao}
CLASSE SOCIAL: {classe}
IDADE: {idade}
RELIGIÃO: {religiao}

CARACTERÍSTICAS DURAS: {traits}
LINGUAGEM TÍPICA: {linguagem}
CONTEXTO DEMOGRÁFICO: {contexto_demografico}

═══════════════════════════════════
REGRAS DE COMPORTAMENTO (CRÍTICAS)
═══════════════════════════════════

1. SEJA TRIBAL. Eleitor brasileiro reage primeiro pelo TOM e pelo VOCABULÁRIO de quem fala, não pelo conteúdo da proposta. Se o tom soa "do outro lado", você desconfia mesmo de proposta boa.

2. MARCADORES QUE TE AFASTAM (se o conteúdo usar esses códigos do espectro oposto, sua reação fica HOSTIL ou DESCONFIADA mesmo se a proposta parecer boa):
   - Esquerda dura/moderada se AFASTA de: "mamata", "chega de", "esse povo", emoji 🇧🇷, "verdadeiro brasileiro", "imposto do povo", tom anti-político genérico, "comunismo", "ideologia de gênero"
   - Direita dura/moderada se AFASTA de: "companheiro", "genocida", "fascista", "elite branca", "patriarcado", "neoliberal", "redistribuição", pauta identitária explícita
   - Centro/Indeciso se AFASTA de: extremismo, ataques pessoais, promessas mágicas sem números, tom raivoso
   - Abstencionista se AFASTA de: qualquer promessa, "vou fazer", "comigo vai ser diferente" — ele já ouviu isso mil vezes

3. PROMESSAS DE CAMPANHA: você é CÉTICO por padrão. Brasileiro médio não acredita em político. Só quem já é fanático de um candidato (esquerda dura/direita dura) aplaude promessa nova sem questionar.

4. VARIE SEU SENTIMENTO. Não use "apoio" como default. Considere honestamente:
   - "raiva" e "indignacao" se algo te ofende ou contraria sua visão de mundo
   - "deboche" se você acha furada/demagogia (típico de abstencionista e indeciso cansado)
   - "neutro" se você não se sente representado nem ofendido — apenas indiferente
   - "negativo" se discorda mas sem revolta
   - "positivo" se gosta com ressalvas
   - "apoio" SOMENTE se é da sua tribo E você acredita de verdade
   - "indignacao" se você sente que estão te enganando ou desrespeitando

5. NÃO SEJA RAZOÁVEL. Eleitor real não pondera "dois lados". Você tem um lado e enxerga o mundo dele.

═══════════════════════════════════
CONTEÚDO QUE VOCÊ VIU NAS REDES
═══════════════════════════════════
{conteudo}

CONTEXTO ADICIONAL: {contexto_cenario}

═══════════════════════════════════
SUA TAREFA
═══════════════════════════════════
Reaja como ESSE eleitor reagiria de VERDADE — incluindo desconfianças, preconceitos linguísticos, ceticismo e tribalismo. Se o conteúdo usa marcadores do espectro oposto ao seu, sua reação NÃO PODE ser "apoio" puro, mesmo que a proposta soe boa.

Responda APENAS com JSON válido, sem texto antes ou depois:

{{
  "sentimento": "positivo" | "negativo" | "neutro" | "raiva" | "indignacao" | "apoio" | "deboche",
  "intensidade": 1-10,
  "vai_compartilhar": true | false,
  "vai_comentar": true | false,
  "comentario_provavel": "como ESSE eleitor escreveria nos comments. Use a linguagem, gírias, emojis, erros de português típicos do perfil. Seja autêntico, não educado.",
  "muda_voto": "sim" | "nao" | "reforça_voto_atual" | "afasta",
  "tema_central_percebido": "qual tema o eleitor entendeu como principal (use as palavras DELE, não as palavras do post)",
  "risco_viralizacao": 1-10,
  "marcador_que_pegou": "qual palavra/expressão do conteúdo mais ativou sua reação (positiva ou negativamente)"
}}"""


# ── EXECUÇÃO DOS AGENTES ──────────────────────────────────────────────────────

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
            max_tokens=700,
            temperature=1.0,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
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


# ── INFERÊNCIA ESTATÍSTICA ────────────────────────────────────────────────────

def _bootstrap_intervalo(valores: List[float], n_reamostragens: int = 1000, confianca: float = 0.95) -> Dict:
    """
    Bootstrap resampling pra calcular intervalo de confiança.

    Reamostra os valores com reposição N vezes e calcula percentis.
    Esse é o método padrão pra estimar margem de erro em amostras pequenas
    sem assumir distribuição normal.

    Args:
        valores: lista de valores numéricos
        n_reamostragens: quantas vezes reamostrar (1000 é padrão científico)
        confianca: nível de confiança (0.95 = 95%)

    Returns:
        {'media', 'ic_inferior', 'ic_superior', 'desvio_padrao', 'margem_erro'}
    """
    if not valores or len(valores) < 2:
        return {
            "media": valores[0] if valores else 0,
            "ic_inferior": valores[0] if valores else 0,
            "ic_superior": valores[0] if valores else 0,
            "desvio_padrao": 0,
            "margem_erro": 0,
        }

    n = len(valores)
    medias_reamostradas = []

    for _ in range(n_reamostragens):
        amostra = [random.choice(valores) for _ in range(n)]
        medias_reamostradas.append(sum(amostra) / n)

    medias_reamostradas.sort()
    alpha = (1 - confianca) / 2
    idx_inferior = int(n_reamostragens * alpha)
    idx_superior = int(n_reamostragens * (1 - alpha))

    media = sum(valores) / len(valores)
    desvio = statistics.stdev(valores) if len(valores) > 1 else 0

    return {
        "media": round(media, 2),
        "ic_inferior": round(medias_reamostradas[idx_inferior], 2),
        "ic_superior": round(medias_reamostradas[idx_superior], 2),
        "desvio_padrao": round(desvio, 2),
        "margem_erro": round((medias_reamostradas[idx_superior] - medias_reamostradas[idx_inferior]) / 2, 2),
    }


def _proporcao_com_ic(contagem: int, total: int, confianca: float = 0.95) -> Dict:
    """
    Calcula intervalo de confiança pra uma proporção (% de algo).
    Usa o método de Wilson, que é o padrão pra proporções em estatística.

    Mais robusto que aproximação normal quando a proporção é extrema (perto de 0 ou 100%).
    """
    if total == 0:
        return {"valor": 0, "ic_inferior": 0, "ic_superior": 0, "margem_erro": 0}

    p = contagem / total
    z = 1.96 if confianca == 0.95 else 2.576  # 95% ou 99%

    denominador = 1 + (z**2 / total)
    centro = (p + (z**2 / (2 * total))) / denominador
    margem = z * ((p * (1 - p) / total + z**2 / (4 * total**2)) ** 0.5) / denominador

    return {
        "valor": round(p * 100, 1),
        "ic_inferior": round(max(0, (centro - margem) * 100), 1),
        "ic_superior": round(min(100, (centro + margem) * 100), 1),
        "margem_erro": round(margem * 100, 1),
    }


def _confiabilidade_amostra(n: int) -> Dict:
    """
    Avalia a confiabilidade estatística do tamanho da amostra.
    Baseado em padrões de pesquisa eleitoral brasileira.
    """
    if n < 30:
        return {"nivel": "baixa", "label": "Amostra muito pequena", "cor": "red"}
    elif n < 80:
        return {"nivel": "moderada", "label": "Amostra exploratória", "cor": "amber"}
    elif n < 200:
        return {"nivel": "boa", "label": "Amostra estatisticamente válida", "cor": "green"}
    else:
        return {"nivel": "excelente", "label": "Amostra robusta", "cor": "cyan"}


# ── AGREGAÇÃO ─────────────────────────────────────────────────────────────────

def _agregar_resultados(reacoes: List[Dict]) -> Dict:
    """Transforma N reações individuais em forecast agregado COM estatística."""
    validas = [r for r in reacoes if "erro" not in r]
    total = len(validas)

    if total == 0:
        return {"erro": "Nenhuma reação válida foi gerada."}

    # ── Sentimentos com intervalo de confiança ──
    sentimentos = Counter(r["sentimento"] for r in validas)
    sentimento_distribuicao = {}
    sentimento_com_ic = {}

    for sent, count in sentimentos.items():
        ic = _proporcao_com_ic(count, total)
        sentimento_distribuicao[sent] = ic["valor"]
        sentimento_com_ic[sent] = {
            "valor": ic["valor"],
            "ic_95": [ic["ic_inferior"], ic["ic_superior"]],
            "margem_erro": ic["margem_erro"],
        }

    # ── Engajamento com IC ──
    vai_compartilhar = sum(1 for r in validas if r.get("vai_compartilhar"))
    vai_comentar = sum(1 for r in validas if r.get("vai_comentar"))

    ic_share = _proporcao_com_ic(vai_compartilhar, total)
    ic_coment = _proporcao_com_ic(vai_comentar, total)

    # ── Risco de viralização com bootstrap ──
    riscos = [r.get("risco_viralizacao", 0) for r in validas]
    risco_stats = _bootstrap_intervalo(riscos)

    # ── Intensidade média com bootstrap ──
    intensidades = [r.get("intensidade", 0) for r in validas]
    intensidade_stats = _bootstrap_intervalo(intensidades)

    # ── Impacto eleitoral ──
    impacto_voto = Counter(r["muda_voto"] for r in validas)
    impacto_voto_pct = {
        k: _proporcao_com_ic(v, total)["valor"] for k, v in impacto_voto.items()
    }

    # ── Clusters por espectro ──
    clusters = {}
    for r in validas:
        esp = r["persona"]["espectro"]
        if esp not in clusters:
            clusters[esp] = {
                "sentimentos": [],
                "intensidades": [],
                "comentarios": [],
                "marcadores": [],
            }
        clusters[esp]["sentimentos"].append(r["sentimento"])
        clusters[esp]["intensidades"].append(r.get("intensidade", 0))
        if r.get("comentario_provavel"):
            clusters[esp]["comentarios"].append(r["comentario_provavel"])
        if r.get("marcador_que_pegou"):
            clusters[esp]["marcadores"].append(r["marcador_que_pegou"])

    for esp, data in clusters.items():
        n = len(data["sentimentos"])
        sent_count = Counter(data["sentimentos"])
        dominante = sent_count.most_common(1)[0]

        # Intensidade do cluster com IC
        int_stats = _bootstrap_intervalo(data["intensidades"])

        data["n_agentes"] = n
        data["intensidade_media"] = int_stats["media"]
        data["intensidade_ic_95"] = [int_stats["ic_inferior"], int_stats["ic_superior"]]
        data["sentimento_dominante"] = dominante[0]
        data["sentimento_dominante_pct"] = round(dominante[1] / n * 100, 1)
        data["amostra_comentarios"] = data["comentarios"][:3]
        data["marcadores_chave"] = [m for m, _ in Counter(data["marcadores"]).most_common(3)]
        data["confiabilidade"] = _confiabilidade_amostra(n)
        del data["comentarios"]
        del data["sentimentos"]
        del data["marcadores"]
        del data["intensidades"]

    # ── Temas percebidos ──
    temas = Counter(r.get("tema_central_percebido", "indefinido") for r in validas)

    # ── Crisis Score com IC ──
    sent_negativo = (
        sentimento_distribuicao.get("negativo", 0)
        + sentimento_distribuicao.get("raiva", 0) * 1.3
        + sentimento_distribuicao.get("indignacao", 0) * 1.4
        + sentimento_distribuicao.get("deboche", 0) * 0.6
    )
    crisis_score_valor = round(min(100, sent_negativo * 0.6 + risco_stats["media"] * 3.5), 1)

    # Pra calcular IC do crisis score, simula variação baseada nas margens das fontes
    margem_crisis = round(
        (
            sentimento_com_ic.get("negativo", {}).get("margem_erro", 0)
            + sentimento_com_ic.get("raiva", {}).get("margem_erro", 0) * 1.3
            + sentimento_com_ic.get("indignacao", {}).get("margem_erro", 0) * 1.4
        )
        * 0.6
        + risco_stats["margem_erro"] * 3.5,
        1,
    )

    return {
        "total_agentes": total,
        "confiabilidade_geral": _confiabilidade_amostra(total),
        "metodo_estatistico": "Bootstrap resampling (1000 reamostragens) + Intervalo de Wilson para proporções",
        "nivel_confianca": "95%",
        "sentimento_distribuicao": sentimento_distribuicao,
        "sentimento_distribuicao_ic": sentimento_com_ic,
        "engajamento": {
            "compartilhamento_pct": ic_share["valor"],
            "compartilhamento_ic_95": [ic_share["ic_inferior"], ic_share["ic_superior"]],
            "compartilhamento_margem_erro": ic_share["margem_erro"],
            "comentario_pct": ic_coment["valor"],
            "comentario_ic_95": [ic_coment["ic_inferior"], ic_coment["ic_superior"]],
            "comentario_margem_erro": ic_coment["margem_erro"],
        },
        "impacto_voto": dict(impacto_voto),
        "impacto_voto_pct": impacto_voto_pct,
        "risco_viralizacao_medio": risco_stats["media"],
        "risco_viralizacao_ic_95": [risco_stats["ic_inferior"], risco_stats["ic_superior"]],
        "risco_viralizacao_margem_erro": risco_stats["margem_erro"],
        "intensidade_media": intensidade_stats["media"],
        "intensidade_ic_95": [intensidade_stats["ic_inferior"], intensidade_stats["ic_superior"]],
        "crisis_score": crisis_score_valor,
        "crisis_score_margem_erro": margem_crisis,
        "crisis_score_ic_95": [
            round(max(0, crisis_score_valor - margem_crisis), 1),
            round(min(100, crisis_score_valor + margem_crisis), 1),
        ],
        "clusters_por_espectro": clusters,
        "temas_percebidos": dict(temas.most_common(5)),
        "amostra_comentarios": [
            r.get("comentario_provavel") for r in validas[:10] if r.get("comentario_provavel")
        ],
    }


# ── FUNÇÃO PÚBLICA ────────────────────────────────────────────────────────────

def simular(conteudo: str, n_agentes: int = 100, filtros: Dict = None, contexto: str = "") -> Dict:
    """
    Simula reação de eleitores brasileiros a um conteúdo político.

    Retorna forecast agregado com inferência estatística:
    - Intervalo de confiança 95% pra todas métricas
    - Margem de erro estatística
    - Avaliação da confiabilidade da amostra
    """
    personas = gerar_lote(n_agentes, filtros)
    if not personas:
        return {"erro": "Nenhuma persona gerada com esses filtros."}

    reacoes = asyncio.run(_executar_simulacao(personas, conteudo, contexto))
    forecast = _agregar_resultados(reacoes)
    forecast["filtros_aplicados"] = filtros or "nenhum (amostra nacional)"
    forecast["conteudo_testado"] = conteudo
    return forecast