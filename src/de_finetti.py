"""
src/de_finetti.py
=================
Cálculo da Distância de De Finetti para partidas de futebol.

Fluxo:
  xG (mandante e visitante)
    └─► distribuição Poisson P(k gols), k ∈ {0,...,MAX_GOLS}
          └─► matriz de placares  (produto cartesiano das distribuições)
                └─► P(vitória M), P(empate), P(vitória V)
                      └─► Distância de De Finetti para cada resultado

A distância de De Finetti mede o "custo de surpresa" de um vetor de
probabilidades p = (p1, p2, p3) em relação ao vértice do simplex
correspondente a cada desfecho possível.

  d(resultado=i) = Σ_j (p_j - e_ij)²

onde e_ij é o vetor indicador do resultado i (1 se j==i, 0 caso contrário).

Referência: de Finetti, B. (1962). Does it make sense to speak of 'good
probability appraisers'? In I. J. Good (Ed.), The Scientist Speculates.
"""

from __future__ import annotations

import logging
import math
from typing import NamedTuple

log = logging.getLogger(__name__)

MAX_GOLS = 5          # k ∈ {0, 1, 2, 3, 4, 5}
_N = MAX_GOLS + 1     # tamanho do vetor de probabilidades (6)


# ──────────────────────────────────────────────────────────────────────────────
# Tipos de retorno
# ──────────────────────────────────────────────────────────────────────────────

class ProbabilidadesResultado(NamedTuple):
    mandante: float   # P(time da casa vencer)
    empate:   float   # P(empate)
    visitante: float  # P(time visitante vencer)


class DistanciaFinetti(NamedTuple):
    se_mandante_ganhar:  float  # d se o evento "vitória mandante" ocorrer
    se_empate:           float  # d se o evento "empate" ocorrer
    se_visitante_ganhar: float  # d se o evento "vitória visitante" ocorrer


# ──────────────────────────────────────────────────────────────────────────────
# 1. Distribuição de Poisson
# ──────────────────────────────────────────────────────────────────────────────

def distribuicao_poisson(xg: float, max_gols: int = MAX_GOLS) -> list[float]:
    """
    Retorna P(X = k) para k = 0, 1, …, max_gols, dado X ~ Poisson(λ=xg).

    Args:
        xg:       Gols esperados (λ). Deve ser > 0; valores ≤ 0 são
                  tratados como 0.01 para evitar colapso da distribuição.
        max_gols: Limite superior de gols considerados (padrão 5).

    Returns:
        Lista de floats com comprimento max_gols + 1.
    """
    lam = max(xg, 0.01)
    probs: list[float] = []
    e_neg_lam = math.exp(-lam)
    fatorial_k = 1
    lam_k = 1.0

    for k in range(max_gols + 1):
        if k > 0:
            fatorial_k *= k
            lam_k *= lam
        probs.append(e_neg_lam * lam_k / fatorial_k)

    # Normaliza para garantir que a soma seja exatamente 1 (truncamento em 5)
    total = sum(probs)
    return [p / total for p in probs]


# ──────────────────────────────────────────────────────────────────────────────
# 2. Matriz de placares
# ──────────────────────────────────────────────────────────────────────────────

def matriz_placares(
    probs_m: list[float],
    probs_v: list[float],
) -> list[list[float]]:
    """
    Monta a matriz de probabilidades de placares (MAX_GOLS+1 × MAX_GOLS+1).

    matriz[i][j] = P(mandante marca i gols) × P(visitante marca j gols)

    Linhas → gols do mandante (0–5)
    Colunas → gols do visitante (0–5)
    """
    n = len(probs_m)
    return [[probs_m[i] * probs_v[j] for j in range(n)] for i in range(n)]


# ──────────────────────────────────────────────────────────────────────────────
# 3. Probabilidades dos três desfechos
# ──────────────────────────────────────────────────────────────────────────────

def probabilidades_resultado(mat: list[list[float]]) -> ProbabilidadesResultado:
    """
    A partir da matriz de placares, calcula P(mandante), P(empate), P(visitante).

    - Diagonal principal (i == j)        → empate
    - Triângulo inferior (i > j)         → mandante vence
    - Triângulo superior (i < j)         → visitante vence
    """
    n = len(mat)
    p_m = p_e = p_v = 0.0

    for i in range(n):
        for j in range(n):
            v = mat[i][j]
            if i == j:
                p_e += v
            elif i > j:
                p_m += v
            else:
                p_v += v

    # Normalização defensiva (truncamento em max_gols pode deixar soma < 1)
    total = p_m + p_e + p_v
    if total > 0:
        p_m /= total
        p_e /= total
        p_v /= total

    return ProbabilidadesResultado(mandante=p_m, empate=p_e, visitante=p_v)


# ──────────────────────────────────────────────────────────────────────────────
# 4. Distância de De Finetti
# ──────────────────────────────────────────────────────────────────────────────

def distancia_finetti(probs: ProbabilidadesResultado) -> DistanciaFinetti:
    """
    Calcula a distância de De Finetti para cada possível desfecho.

    Para um vetor p = (p1, p2, p3) e o vértice e_i do simplex:
        d(e_i) = Σ_j (p_j - e_ij)²

    Onde e_ij = 1 se j == i, else 0.

    Interpretação: quanto menor d(e_i), mais o forecaster "apostou" naquele
    resultado; quanto maior, mais "surpreendido" ficaria se ele ocorresse.
    """
    p1, p2, p3 = probs.mandante, probs.empate, probs.visitante

    d_mandante  = (p1 - 1)**2 + (p2 - 0)**2 + (p3 - 0)**2
    d_empate    = (p1 - 0)**2 + (p2 - 1)**2 + (p3 - 0)**2
    d_visitante = (p1 - 0)**2 + (p2 - 0)**2 + (p3 - 1)**2

    return DistanciaFinetti(
        se_mandante_ganhar=d_mandante,
        se_empate=d_empate,
        se_visitante_ganhar=d_visitante,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 5. Função de conveniência: pipeline completo
# ──────────────────────────────────────────────────────────────────────────────

class ResultadoFinetti(NamedTuple):
    xg_m:          float
    xg_v:          float
    probs_poisson_m: list[float]   # P(k gols), k=0..MAX_GOLS
    probs_poisson_v: list[float]
    matriz:          list[list[float]]
    resultado:       ProbabilidadesResultado
    distancia:       DistanciaFinetti
    favorito:        str            # "Mandante" | "Empate" | "Visitante"


def calcular(xg_m: float, xg_v: float) -> ResultadoFinetti:
    """
    Pipeline completo: xG → Poisson → matriz → probs → distância.

    Args:
        xg_m: xG do mandante (time da casa)
        xg_v: xG do visitante (time de fora)

    Returns:
        ResultadoFinetti com todos os dados intermediários e finais.
    """
    probs_m = distribuicao_poisson(xg_m)
    probs_v = distribuicao_poisson(xg_v)
    mat     = matriz_placares(probs_m, probs_v)
    probs   = probabilidades_resultado(mat)
    dist    = distancia_finetti(probs)

    # O favorito é o resultado com menor distância de Finetti
    valores = {
        "Mandante":  dist.se_mandante_ganhar,
        "Empate":    dist.se_empate,
        "Visitante": dist.se_visitante_ganhar,
    }
    favorito = min(valores, key=valores.get)  # type: ignore[arg-type]

    log.debug(
        f"De Finetti | xG: {xg_m:.2f}×{xg_v:.2f} | "
        f"P(M/E/V): {probs.mandante:.1%}/{probs.empate:.1%}/{probs.visitante:.1%} | "
        f"Favorito: {favorito}"
    )

    return ResultadoFinetti(
        xg_m=xg_m, xg_v=xg_v,
        probs_poisson_m=probs_m, probs_poisson_v=probs_v,
        matriz=mat, resultado=probs, distancia=dist,
        favorito=favorito,
    )


def parse_xg(valor: str) -> float | None:
    """
    Converte a string de xG vinda do scraper (ex: '1.45', '1,45', '--')
    para float. Retorna None se não for possível converter.
    """
    if not valor or valor in ("--", "N/A", "-", ""):
        return None
    try:
        return float(valor.replace(",", "."))
    except ValueError:
        return None