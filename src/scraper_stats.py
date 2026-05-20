import json
import logging
from datetime import datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9",
}
TIMEOUT = 20


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def buscar_pagina(url: str) -> BeautifulSoup:
    """
    Faz o GET da URL e retorna o BeautifulSoup.
    Sempre passa pelo throttler antes de executar a requisição.
    """
    from src.throttler import throttler
    throttler.aguardar(motivo=url.split("/")[-2] or url)
    log.info(f"GET {url}")
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def extrair_info_partida(soup: BeautifulSoup) -> dict:
    """Extrai metadados do SportsEvent no JSON-LD da página."""
    info = {
        "mandante": "N/A",
        "visitante": "N/A",
        "data_hora": "N/A",
        "estadio": "N/A",
        "campeonato": "N/A",
    }
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            dados = json.loads(tag.string or "")
            for item in dados.get("@graph", []):
                if item.get("@type") == "SportsEvent":
                    info["mandante"] = item.get("homeTeam", {}).get("name", "N/A")
                    info["visitante"] = item.get("awayTeam", {}).get("name", "N/A")
                    info["estadio"] = item.get("location", {}).get("name", "N/A")
                    raw_dt = item.get("startDate", "")
                    if raw_dt:
                        try:
                            dt = datetime.fromisoformat(raw_dt)
                            info["data_hora"] = dt.strftime("%d/%m/%Y %H:%M")
                        except ValueError:
                            info["data_hora"] = raw_dt
                if item.get("@type") == "BreadcrumbList":
                    items_bc = item.get("itemListElement", [])
                    if len(items_bc) >= 3:
                        info["campeonato"] = items_bc[2].get("name", "N/A")
        except (json.JSONDecodeError, AttributeError):
            continue
    return info


def _extrair_tabela(soup: BeautifulSoup, indice: int) -> list[list[str]]:
    tables = soup.find_all("table")
    if indice >= len(tables):
        return []
    return [
        [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
        for tr in tables[indice].find_all("tr")
        if any(td.get_text(strip=True) for td in tr.find_all(["td", "th"]))
    ]


def extrair_estatisticas_times(soup: BeautifulSoup) -> tuple[list, list, list, list]:
    """Retorna: forma_mandante, stats_mandante, forma_visitante, stats_visitante."""
    return (
        _extrair_tabela(soup, 0),
        _extrair_tabela(soup, 1),
        _extrair_tabela(soup, 2),
        _extrair_tabela(soup, 3),
    )


def extrair_odds(soup: BeautifulSoup) -> list[list[str]]:
    linhas = _extrair_tabela(soup, 4)
    return [linha[:-1] if len(linha) > 4 else linha for linha in linhas]


def extrair_classificacao(soup: BeautifulSoup) -> tuple[list, list, list]:
    """Retorna: class_casa, class_fora, class_geral."""
    return (
        _extrair_tabela(soup, 5),
        _extrair_tabela(soup, 6),
        _extrair_tabela(soup, 7),
    )


# ---------------------------------------------------------------------------
# Utilitário
# ---------------------------------------------------------------------------

def get_stat(tabela: list[list[str]], nome: str, coluna: int = 1) -> str:
    """
    Busca o valor de uma métrica em uma tabela de stats.
    """
    for row in tabela:
        if row and row[0] == nome:
            return row[coluna] if len(row) > coluna else ""
    return ""


def melhor_odd(odds: list[list[str]], col: int) -> str:
    """Retorna o maior valor numérico em uma coluna das odds."""
    valores = []
    for row in odds:
        if row and not row[0] and len(row) > col:
            try:
                valores.append(float(row[col]))
            except ValueError:
                pass
    return f"{max(valores):.2f}" if valores else "N/A"