"""
src/status_manager.py
=====================
Gerencia o arquivo de status compartilhado entre main.py (daemon) e app.py (Streamlit).
O arquivo data/status.json funciona como canal de comunicação unidirecional.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

STATUS_FILE = Path("data/status.json")

DEFAULT_STATUS = {
    "estado": "parado",       # "rodando" | "parado" | "pausado"
    "campeonato": "",
    "partida": "",
    "ultima_atividade": "",
    "mensagem_status": "Aguardando início...",
    "total_partidas_cache": 0,
    "partidas_coletadas": 0,
    "comando": "",            # "pausar" | "reiniciar" | ""
    "pid": None,
}


def _ensure_dir():
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)


def carregar() -> dict:
    """Lê o status atual do arquivo."""
    _ensure_dir()
    if not STATUS_FILE.exists():
        salvar(DEFAULT_STATUS)
        return dict(DEFAULT_STATUS)
    try:
        with STATUS_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"Erro ao ler status: {e}. Usando padrão.")
        return dict(DEFAULT_STATUS)


def salvar(payload: dict) -> None:
    """Salva o status no arquivo."""
    _ensure_dir()
    payload["_atualizado_em"] = datetime.now().isoformat()
    with STATUS_FILE.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def atualizar(**kwargs) -> dict:
    """Atualiza campos específicos do status."""
    status = carregar()
    status.update(kwargs)
    salvar(status)
    return status


def ler_comando() -> str:
    """Retorna e limpa o comando pendente do Streamlit."""
    status = carregar()
    cmd = status.get("comando", "")
    if cmd:
        status["comando"] = ""
        salvar(status)
    return cmd


def definir_comando(cmd: str) -> None:
    """Define um comando a ser executado pelo daemon."""
    atualizar(comando=cmd)