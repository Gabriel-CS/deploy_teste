"""
src/scraper_campeonatos.py
==========================
Coleta a lista de partidas de um campeonato a partir do APWin.
Armazena em data/matches/partidas_<slug>.json com metadados de coleta.
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

FRESHNESS_HORAS = 1  # re-coleta se o arquivo tiver mais de 1h

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9",
}


def _slugify(texto: str) -> str:
    return texto.lower().replace(" ", "_")


def _caminho_arquivo(campeonato: str, dir_matches: Path) -> Path:
    return dir_matches / f"partidas_{_slugify(campeonato)}.json"


def _esta_fresco(path: Path) -> bool:
    """Retorna True se o arquivo existe e foi coletado há menos de FRESHNESS_HORAS."""
    if not path.exists():
        return False
    try:
        dados = json.loads(path.read_text(encoding="utf-8"))
        meta = dados.get("_meta", {})
        coletado_em = meta.get("coletado_em")
        if not coletado_em:
            return False
        dt = datetime.fromisoformat(coletado_em)
        return datetime.now() - dt < timedelta(hours=FRESHNESS_HORAS)
    except Exception:
        return False


def _parse_partidas(html: str, base_url: str) -> Dict[str, dict]:
    """Extrai o dicionário de partidas do HTML da página do campeonato."""
    soup = BeautifulSoup(html, "html.parser")
    container = soup.find("div", id="jogos") or soup
    partidas: Dict[str, dict] = {}

    for block in container.find_all("div", class_="is-hover"):
        first_link = block.find("a", href=re.compile(r"/jogo/"))
        if not first_link:
            continue

        full_link = urljoin(base_url, first_link["href"].split("#")[0])
        path_parts = [p for p in first_link["href"].split("/") if p]
        raw_slug = path_parts[-1].split("#")[0] if path_parts else ""
        slug = raw_slug or (path_parts[-2] if len(path_parts) >= 2 else "")

        logos = block.select("figure.image.is-24x24 img")
        home = away = ""
        if len(logos) >= 2:
            home = logos[0].get("alt", "").replace(" logo de equipe", "").replace(" Team Logo", "").strip()
            away = logos[1].get("alt", "").replace(" logo de equipe", "").replace(" Team Logo", "").strip()

        if not home or not away:
            p_away = block.find("p", class_="has-text-right")
            p_home = block.find("p", class_=lambda c: c and "has-text-black" in c and "has-text-right" not in c)
            if p_away and not away:
                away = p_away.get_text(strip=True)
            if p_home and not home:
                home = p_home.get_text(strip=True)

        home = home or "Time A"
        away = away or "Time B"

        nome = f"{home} x {away}"
        partidas[nome] = {"link": full_link, "slug": slug, "home": home, "away": away}

    log.info(f"Partidas encontradas: {len(partidas)}")
    return partidas


def obter_partidas(
    campeonato: str,
    champ_url: str,
    dir_matches: Path,
    forcar_atualizacao: bool = False,
) -> Dict[str, dict]:
    """
    Retorna as partidas do campeonato.
    Re-coleta automaticamente se o arquivo não existir ou tiver mais de FRESHNESS_HORAS.

    Returns:
        dict sem a chave '_meta'
    """
    path = _caminho_arquivo(campeonato, dir_matches)

    if not forcar_atualizacao and _esta_fresco(path):
        log.info(f"Usando cache de partidas: {path}")
        dados = json.loads(path.read_text(encoding="utf-8"))
        return {k: v for k, v in dados.items() if k != "_meta"}

    log.info(f"Coletando partidas de '{campeonato}' em {champ_url}")
    from src.throttler import throttler
    throttler.aguardar(motivo=campeonato)

    session = requests.Session()
    session.headers.update(HEADERS)
    time.sleep(1.5)

    resp = session.get(champ_url, timeout=30)
    resp.raise_for_status()

    partidas = _parse_partidas(resp.text, champ_url)

    # Salva com metadados
    payload = {
        "_meta": {
            "campeonato": campeonato,
            "coletado_em": datetime.now().isoformat(),
            "total": len(partidas),
        }
    }
    payload.update(partidas)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"Partidas salvas em {path}")

    return partidas


def meta_coleta(campeonato: str, dir_matches: Path) -> Optional[dict]:
    """Retorna os metadados da última coleta ou None."""
    path = _caminho_arquivo(campeonato, dir_matches)
    if not path.exists():
        return None
    try:
        dados = json.loads(path.read_text(encoding="utf-8"))
        return dados.get("_meta")
    except Exception:
        return None