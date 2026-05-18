"""
src/cache_manager.py
====================
Gerencia o cache local das estatísticas de partidas.
Cada partida é salva em data/stats_cache/<slug>.json
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

DIR_CACHE = Path("data/stats_cache")


def _slug_para_path(slug: str) -> Path:
    slug_safe = re.sub(r"[^\w\-]", "_", slug)
    return DIR_CACHE / f"{slug_safe}.json"


def salvar(slug: str, partida_nome: str, payload: dict) -> None:
    """Salva as estatísticas de uma partida no cache local."""
    DIR_CACHE.mkdir(parents=True, exist_ok=True)
    dados = {
        "_meta": {
            "slug": slug,
            "partida": partida_nome,
            "coletado_em": datetime.now().isoformat(),
        },
        **payload,
    }
    path = _slug_para_path(slug)
    path.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    log.debug(f"Cache salvo: {path.name}")


def carregar(slug: str) -> Optional[dict]:
    """
    Carrega as estatísticas do cache. Retorna None se não existir.
    O payload retornado não contém '_meta'.
    """
    path = _slug_para_path(slug)
    if not path.exists():
        return None
    try:
        dados = json.loads(path.read_text(encoding="utf-8"))
        return {k: v for k, v in dados.items() if k != "_meta"}
    except Exception as e:
        log.warning(f"Erro ao ler cache {path.name}: {e}")
        return None


def existe(slug: str) -> bool:
    return _slug_para_path(slug).exists()


def meta(slug: str) -> Optional[dict]:
    path = _slug_para_path(slug)
    if not path.exists():
        return None
    try:
        dados = json.loads(path.read_text(encoding="utf-8"))
        return dados.get("_meta")
    except Exception:
        return None


def listar_slugs_cacheados() -> set[str]:
    if not DIR_CACHE.exists():
        return set()
    return {p.stem for p in DIR_CACHE.glob("*.json")}


def total_cacheado(partidas: dict) -> int:
    """Conta quantas partidas do dict já têm cache."""
    return sum(1 for info in partidas.values() if existe(info.get("slug", "")))