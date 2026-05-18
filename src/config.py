"""
config.py
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()   # carrega .env se existir

# ── Google Sheets ──────────────────────────────────────────────────────────
SPREADSHEET_ID: str       = os.environ["SPREADSHEET_ID"]       # ID da planilha
CREDENTIALS_FILE: str     = os.getenv("GOOGLE_CREDENTIALS", "credentials.json")
VALUE_INPUT_OPTION: str   = "USER_ENTERED"   # interpreta fórmulas e datas

# Nome das abas usadas na planilha
ABA_PARTIDAS:  str = "Partidas"    # lista de partidas coletadas
ABA_STATS:     str = "Stats"       # estatísticas detalhadas
ABA_ODDS:      str = "Odds"        # tabela de odds
ABA_CLASS:     str = "Classificação"

# ── Caminhos locais ────────────────────────────────────────────────────────
DIR_DATA:         Path = Path("data")
DIR_MATCHES:      Path = DIR_DATA / "matches"
DIR_STATS_CACHE:  Path = DIR_DATA / "stats_cache"
CAMPEONATOS_JSON: Path = DIR_DATA / "campeonatos.json"
STATUS_FILE:      Path = DIR_DATA / "status.json"

# ── Scraping ───────────────────────────────────────────────────────────────
DELAY_ENTRE_REQUISICOES: float = 2.0      # segundos entre GETs (throttler)
FRESHNESS_PARTIDAS_HORAS: int  = 1        # re-coleta partidas após N horas
LOOP_SLEEP_SEGUNDOS: int       = 300      # pausa entre ciclos completos (5 min)

# ── Logging ────────────────────────────────────────────────────────────────
LOG_LEVEL: str  = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE:  Path = DIR_DATA / "daemon.log"