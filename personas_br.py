"""
Biblioteca de personas políticas brasileiras.
Esse é o diferencial — sem isso, a simulação é genérica e inútil pro mercado BR.
"""

import random
from typing import List, Dict

ESPECTROS = {
    "esquerda_dura": {
        "weight": 0.12,
        "traits": ["anti-Bolsonaro radical", "apoia Lula sem ressalvas", "crítico ao mercado", "pauta identitária forte"],
        "linguagem": "informal, gírias militantes, uso frequente de 'companheiro', 'genocida'",
        "midias": ["Twitter/X", "Instagram"],
    },
    "esquerda_moderada": {
        "weight": 0.15,
        "traits": ["lulista crítico", "valoriza democracia e instituições", "preocupado com economia"],
        "linguagem": "ponderada, busca diálogo, evita extremos",
        "midias": ["Instagram", "YouTube", "Facebook"],
    },
    "centro": {
        "weight": 0.18,
        "traits": ["voto útil", "rejeita extremos", "decide por economia e segurança", "pragmático"],
        "linguagem": "neutra, analítica, baseada em fatos",
        "midias": ["YouTube", "Facebook", "WhatsApp"],
    },
    "direita_moderada": {
        "weight": 0.20,
        "traits": ["liberal econômico", "conservador moderado nos costumes", "anti-PT mas não bolsonarista"],
        "linguagem": "formal, foca economia, mercado, gestão",
        "midias": ["YouTube", "Twitter/X", "LinkedIn"],
    },
    "direita_dura": {
        "weight": 0.18,
        "traits": ["bolsonarista convicto", "anti-esquerda radical", "pauta de costumes forte", "desconfia de mídia"],
        "linguagem": "informal, gírias bolsonaristas, 'mito', 'comunista', 'imprensa lixo'",
        "midias": ["WhatsApp", "Telegram", "YouTube", "Instagram"],
    },
    "abstencionista": {
        "weight": 0.10,
        "traits": ["descrente da política", "voto branco/nulo recorrente", "cínico"],
        "linguagem": "irônica, sarcástica, 'todos iguais'",
        "midias": ["TikTok", "Twitter/X"],
    },
    "indeciso": {
        "weight": 0.07,
        "traits": ["sem posição firme", "absorve narrativa dominante", "decide tarde"],
        "linguagem": "questionadora, pede dados",
        "midias": ["Instagram", "YouTube"],
    },
}

REGIOES = {
    "sudeste": {"weight": 0.42, "context": "urbano, pauta segurança e economia"},
    "nordeste": {"weight": 0.27, "context": "lulismo histórico, pauta social forte"},
    "sul": {"weight": 0.14, "context": "mais conservador, agronegócio e indústria"},
    "centro_oeste": {"weight": 0.08, "context": "agro forte, direita dominante"},
    "norte": {"weight": 0.09, "context": "pauta ambiental polarizada, evangélicos crescendo"},
}

CLASSES = {
    "A_B": {"weight": 0.20, "context": "alta escolaridade, mais liberal econômico"},
    "C": {"weight": 0.50, "context": "classe média baixa, foco em emprego e custo de vida"},
    "D_E": {"weight": 0.30, "context": "baixa renda, pauta social central, beneficiários de programas"},
}

FAIXAS_ETARIAS = {
    "16_24": {"weight": 0.15, "context": "redes sociais nativas, pauta climática e identitária"},
    "25_39": {"weight": 0.30, "context": "trabalho, família, economia"},
    "40_59": {"weight": 0.35, "context": "estabilidade, segurança, saúde"},
    "60_plus": {"weight": 0.20, "context": "tradição, religião, previdência"},
}

RELIGIAO = {
    "catolico": {"weight": 0.50},
    "evangelico": {"weight": 0.31, "context": "pauta de costumes muito forte, anti-aborto, pró-família"},
    "sem_religiao": {"weight": 0.10},
    "outras": {"weight": 0.09},
}


def _weighted_choice(options: Dict) -> str:
    keys = list(options.keys())
    weights = [options[k]["weight"] for k in keys]
    return random.choices(keys, weights=weights, k=1)[0]


def gerar_persona() -> Dict:
    """Gera uma persona BR realista com base em distribuições demográficas."""
    espectro = _weighted_choice(ESPECTROS)
    regiao = _weighted_choice(REGIOES)
    classe = _weighted_choice(CLASSES)
    idade = _weighted_choice(FAIXAS_ETARIAS)
    religiao = _weighted_choice(RELIGIAO)

    return {
        "espectro": espectro,
        "regiao": regiao,
        "classe": classe,
        "idade": idade,
        "religiao": religiao,
        "traits": ESPECTROS[espectro]["traits"],
        "linguagem": ESPECTROS[espectro]["linguagem"],
        "midias_preferidas": ESPECTROS[espectro]["midias"],
        "contexto_demografico": (
            f"{REGIOES[regiao]['context']}; "
            f"{CLASSES[classe]['context']}; "
            f"{FAIXAS_ETARIAS[idade]['context']}"
        ),
    }


def gerar_lote(n: int, filtros: Dict = None) -> List[Dict]:
    """
    Gera N personas. Filtros opcionais permitem recorte específico.
    Ex: filtros={'regiao': 'nordeste'} → simula só eleitorado NE.
    """
    personas = []
    tentativas = 0
    while len(personas) < n and tentativas < n * 10:
        p = gerar_persona()
        if filtros:
            if all(p.get(k) == v for k, v in filtros.items()):
                personas.append(p)
        else:
            personas.append(p)
        tentativas += 1
    return personas