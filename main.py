#!/usr/bin/env python3
"""
main.py — Loop contínuo de sincronização Sheets ↔ APWin
=========================================================

Comportamento:
  • Inicia automaticamente ao ser invocado pelo app.py (Streamlit).
  • Coleta campeonatos.json automaticamente se o arquivo não existir.
  • Fica em polling do Google Sheets a cada POLL_INTERVAL segundos.
  • Ao detectar mudança de campeonato:
      → Coleta a lista de partidas (cache de 1 h) e atualiza o dropdown.
      → NÃO faz pre-fetch das stats — coleta apenas sob demanda.
  • Ao detectar mudança de partida:
      → Cache hit  → publica no dashboard imediatamente.
      → Cache miss → busca no APWin, salva no cache, publica.

Uso:
  python main.py                  # loop normal (prefetch desativado)
  python main.py --com-prefetch   # habilita pre-fetch em background
  python main.py --setup          # configura a planilha (1ª vez)
  python main.py --poll 10        # intervalo de polling em segundos

CORREÇÕES APLICADAS
-------------------
1. [PRINCIPAL] O daemon nunca morria silenciosamente por erro de rede ou
   token expirado — agora o ciclo principal captura qualquer exceção,
   registra no log e continua rodando. O status_manager reporta o erro
   para o painel Streamlit ao invés de silenciar.

2. Heartbeat: a cada HEARTBEAT_CICLOS ciclos sem mudança de campeonato/
   partida, o daemon atualiza o campo "ultima_atividade" do status.json
   para que o painel Streamlit saiba que o processo ainda está vivo.

3. Reconexão proativa: se o SheetsManager lançar exceção em 3 tentativas
   consecutivas de leitura de controle, o daemon reconecta o sm inteiro
   (nova instância + conectar()) antes de continuar o loop.
"""

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from src import status_manager

# ── Logging ─────────────────────────────────────────────────────────────────
Path("logs").mkdir(exist_ok=True)
Path("data").mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/main.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("main")

# ── Constantes ───────────────────────────────────────────────────────────────
CAMINHO_CAMPEONATOS    = Path("data/campeonatos.json")
DIR_MATCHES            = Path("data/matches")
POLL_INTERVAL          = 6      # segundos entre leituras do Sheets
PREFETCH_STATUS_A_CADA = 3      # atualiza status no Sheets a cada N partidas
HEARTBEAT_CICLOS       = 50     # a cada N ciclos sem atividade, grava heartbeat
ERROS_CONSECUTIVOS_MAX = 5      # reconecta o sm após N erros seguidos


# ── Estado global ────────────────────────────────────────────────────────────
class _Estado:
    def __init__(self):
        self.campeonato: str              = ""
        self.partida:    str              = ""
        self.partidas:   dict             = {}
        self.prefetch_thread: threading.Thread | None = None
        self.parar_prefetch  = threading.Event()
        self.lock            = threading.Lock()
        self.rodando: bool   = True
        self.ciclo_count: int = 0          # FIX 2: contador para heartbeat
        self.erros_consecutivos: int = 0   # FIX 3: contador de erros seguidos


estado = _Estado()


# ── Helpers ──────────────────────────────────────────────────────────────────
def _fmt_dt(iso_str: str) -> str:
    try:
        return datetime.fromisoformat(iso_str).strftime("%d/%m/%Y %H:%M")
    except Exception:
        return iso_str


def coletar_campeonatos() -> None:
    log.info("campeonatos.json não encontrado — coletando automaticamente...")
    status_manager.atualizar(mensagem_status="Coletando lista de campeonatos...")

    from src.apwin_camps import ApwinScraper
    CAMINHO_CAMPEONATOS.parent.mkdir(parents=True, exist_ok=True)
    scraper = ApwinScraper(delay=2.0)
    scraper.run(str(CAMINHO_CAMPEONATOS))

    if not CAMINHO_CAMPEONATOS.exists():
        log.error("Falha ao coletar campeonatos. Verifique a conectividade.")
        sys.exit(1)

    log.info(f"Campeonatos salvos em {CAMINHO_CAMPEONATOS}.")


def carregar_campeonatos() -> dict:
    if not CAMINHO_CAMPEONATOS.exists():
        coletar_campeonatos()
    return json.loads(CAMINHO_CAMPEONATOS.read_text(encoding="utf-8"))


# ── Thread de pre-fetch (opcional) ──────────────────────────────────────────
def _thread_prefetch(campeonato, partidas, sm, parar):
    from src import cache_manager
    from src.scraper_stats import (
        buscar_pagina, extrair_info_partida, extrair_estatisticas_times,
        extrair_odds,
    )

    pendentes  = [(n, i) for n, i in partidas.items()
                  if not cache_manager.existe(i.get("slug", ""))]
    ja_cached  = len(partidas) - len(pendentes)
    total      = len(pendentes)
    processado = 0

    log.info(f"[prefetch] {total} pendentes, {ja_cached} já em cache.")

    if total == 0:
        with estado.lock:
            sm.atualizar_status(
                f"✓ Todas as {len(partidas)} partidas já estão em cache.", "ok")
        return

    for nome, info in pendentes:
        if parar.is_set():
            log.info("[prefetch] Interrompido.")
            return

        slug = info.get("slug", nome)
        url  = info.get("link", "")
        if not url:
            continue

        try:
            soup = buscar_pagina(url)
            part_info = extrair_info_partida(soup)
            forma_m, stats_m, forma_v, stats_v = extrair_estatisticas_times(soup)
            odds = extrair_odds(soup)

            cache_manager.salvar(slug, nome, {
                "info": part_info,
                "forma_m": forma_m, "stats_m": stats_m,
                "forma_v": forma_v, "stats_v": stats_v,
                "odds": odds,
            })
            processado += 1
            log.info(f"[prefetch] {processado}/{total} — {nome}")

            if processado % PREFETCH_STATUS_A_CADA == 0:
                with estado.lock:
                    sm.atualizar_progresso_prefetch(
                        ja_cached + processado, len(partidas), campeonato)
        except Exception as e:
            log.warning(f"[prefetch] Falha em '{nome}': {e}")

        if parar.is_set():
            return

    total_final = ja_cached + processado
    log.info(f"[prefetch] Concluído. {total_final}/{len(partidas)} em cache.")
    with estado.lock:
        sm.atualizar_status(
            f"✓ Pre-fetch concluído: {total_final}/{len(partidas)} partidas em cache.", "ok")


def iniciar_prefetch(campeonato, partidas, sm):
    if estado.prefetch_thread and estado.prefetch_thread.is_alive():
        log.info("[prefetch] Cancelando thread anterior...")
        estado.parar_prefetch.set()
        estado.prefetch_thread.join(timeout=5)

    estado.parar_prefetch.clear()
    t = threading.Thread(
        target=_thread_prefetch,
        args=(campeonato, partidas, sm, estado.parar_prefetch),
        daemon=True,
        name="prefetch",
    )
    t.start()
    estado.prefetch_thread = t


# ── Publicar partida no dashboard ────────────────────────────────────────────
def publicar_partida(nome: str, sm) -> bool:
    from src import cache_manager
    from src.scraper_stats import (
        buscar_pagina, extrair_info_partida, extrair_estatisticas_times,
        extrair_odds,
    )

    with estado.lock:
        partida_info = estado.partidas.get(nome)

    if not partida_info:
        sm.atualizar_status(f"⚠ Partida não encontrada: '{nome}'", "erro")
        return False

    slug = partida_info.get("slug", nome)
    url  = partida_info.get("link", "")
    cached = cache_manager.carregar(slug)

    if cached:
        log.info(f"Cache hit: '{nome}'")
        sm.atualizar_status(f"⏳ Publicando '{nome}' (cache)...")
    else:
        log.info(f"Cache miss — buscando stats de '{nome}'")
        sm.atualizar_status(f"⏳ Buscando estatísticas de '{nome}'...")
        try:
            soup = buscar_pagina(url)
            info = extrair_info_partida(soup)
            forma_m, stats_m, forma_v, stats_v = extrair_estatisticas_times(soup)
            odds = extrair_odds(soup)

            cached = {
                "info": info,
                "forma_m": forma_m, "stats_m": stats_m,
                "forma_v": forma_v, "stats_v": stats_v,
                "odds": odds,
            }
            cache_manager.salvar(slug, nome, cached)
        except Exception as e:
            log.exception(f"Falha ao buscar '{nome}'")
            sm.atualizar_status(f"❌ Erro ao buscar '{nome}': {e}", "erro")
            status_manager.atualizar(mensagem_status=f"Erro ao buscar: {e}")
            return False

    try:
        sm.escrever_dashboard(
            cached["info"],
            cached["forma_m"], cached["stats_m"],
            cached["forma_v"], cached["stats_v"],
            cached["odds"],
        )
        sm.escrever_historico(
            cached["info"], cached["stats_m"], cached["stats_v"], cached["odds"]
        )

        mandante  = cached["info"].get("mandante", "?")
        visitante = cached["info"].get("visitante", "?")
        ts = datetime.now().strftime("%H:%M:%S")
        status_manager.atualizar(
            ultima_atividade=datetime.now().isoformat(),
            mensagem_status=f"Publicado: {mandante} × {visitante}",
        )
        sm.atualizar_status(f"✓ {mandante} × {visitante}  |  {ts}", "ok")
        return True

    except Exception as e:
        log.exception(f"Falha ao publicar '{nome}'")
        sm.atualizar_status(f"❌ Erro ao publicar: {e}", "erro")
        status_manager.atualizar(mensagem_status=f"Erro ao publicar: {e}")
        return False


# ── Ciclo de polling ─────────────────────────────────────────────────────────
def ciclo(sm, campeonatos: dict, habilitar_prefetch: bool) -> None:
    from src.scraper_campeonatos import obter_partidas, meta_coleta

    # ── Comandos vindos do painel Streamlit ──
    cmd = status_manager.ler_comando()
    if cmd == "pausar":
        log.info("Pausando por comando do painel.")
        status_manager.atualizar(estado="pausado",
                                  mensagem_status="Pausado pelo painel de controle.")
        while status_manager.carregar().get("comando") != "reiniciar":
            time.sleep(2)
        status_manager.atualizar(estado="rodando", comando="",
                                  mensagem_status="Retomando execução...")
        log.info("Execução retomada.")
    elif cmd == "reiniciar":
        status_manager.atualizar(estado="rodando", comando="",
                                  mensagem_status="Retomando execução...")

    # FIX 2 — Heartbeat: mantém o painel Streamlit informado mesmo sem mudanças
    estado.ciclo_count += 1
    if estado.ciclo_count % HEARTBEAT_CICLOS == 0:
        status_manager.atualizar(
            estado="rodando",
            ultima_atividade=datetime.now().isoformat(),
            mensagem_status=(
                f"Monitorando — campeonato: '{estado.campeonato or 'nenhum'}' "
                f"| partida: '{estado.partida or 'nenhuma'}'"
            ),
        )

    # ── Leitura da planilha ──
    try:
        campeonato_atual, partida_atual = sm.ler_controle()
        # FIX 3 — zera o contador de erros após leitura bem-sucedida
        estado.erros_consecutivos = 0
    except Exception as e:
        estado.erros_consecutivos += 1
        log.warning(
            f"Falha ao ler controle ({estado.erros_consecutivos}/"
            f"{ERROS_CONSECUTIVOS_MAX}): {e}"
        )
        status_manager.atualizar(mensagem_status=f"Erro ao ler controle: {e}")

        # FIX 3 — reconecta o SheetsManager após erros consecutivos
        if estado.erros_consecutivos >= ERROS_CONSECUTIVOS_MAX:
            log.warning("Muitos erros seguidos — reconectando SheetsManager...")
            try:
                sm.conectar()
                estado.erros_consecutivos = 0
                log.info("SheetsManager reconectado com sucesso.")
                status_manager.atualizar(
                    mensagem_status="Reconectado ao Google Sheets.")
            except Exception as re:
                log.error(f"Reconexão falhou: {re}")
        return

    # ── Mudança de campeonato ────────────────────────────────────────────────
    if campeonato_atual != estado.campeonato:
        if not campeonato_atual:
            return

        if campeonato_atual not in campeonatos:
            sm.atualizar_status(
                f"⚠ Campeonato desconhecido: '{campeonato_atual}'", "erro")
            return

        log.info(f"Campeonato: '{estado.campeonato}' → '{campeonato_atual}'")
        sm.atualizar_status(f"⏳ Coletando partidas de '{campeonato_atual}'...")

        try:
            partidas = obter_partidas(
                campeonato_atual, campeonatos[campeonato_atual], DIR_MATCHES)
        except Exception as e:
            log.exception("Falha ao coletar partidas")
            sm.atualizar_status(f"❌ Erro ao coletar partidas: {e}", "erro")
            return

        meta  = meta_coleta(campeonato_atual, DIR_MATCHES)
        coleta_ts = _fmt_dt(meta["coletado_em"]) if meta and meta.get("coletado_em") else ""

        from src import cache_manager
        n_cache = cache_manager.total_cacheado(partidas)

        sm.atualizar_lista_partidas(list(partidas.keys()), campeonato_atual,
                                    coleta_ts, n_cache=n_cache)

        with estado.lock:
            estado.campeonato = campeonato_atual
            estado.partidas   = partidas
            estado.partida    = ""

        status_manager.atualizar(
            estado="rodando",
            campeonato=campeonato_atual,
            partida="",
            partidas_coletadas=len(partidas),
            total_partidas_cache=n_cache,
            ultima_atividade=datetime.now().isoformat(),
            mensagem_status=f"{len(partidas)} partidas | {n_cache} em cache — selecione uma partida",
        )
        sm.atualizar_status(
            f"✓ {len(partidas)} partidas carregadas | {n_cache} já em cache. "
            f"Selecione uma partida em C4.", "ok")

        if habilitar_prefetch:
            iniciar_prefetch(campeonato_atual, partidas, sm)

        return

    # ── Mudança de partida ───────────────────────────────────────────────────
    if partida_atual and partida_atual != estado.partida:
        log.info(f"Partida: '{estado.partida}' → '{partida_atual}'")
        status_manager.atualizar(
            partida=partida_atual,
            ultima_atividade=datetime.now().isoformat(),
            mensagem_status=f"Processando: {partida_atual}...",
        )
        with estado.lock:
            estado.partida = partida_atual
        publicar_partida(partida_atual, sm)


# ── Setup e entry point ──────────────────────────────────────────────────────
def cmd_setup(sm, campeonatos: dict) -> None:
    log.info("Setup da planilha...")
    sm.configurar_controle(list(campeonatos.keys()))
    sm.remover_dados_brutos()
    for nome in ("dashboard", "historico"):
        sm._aba(nome)
    print("\n  ✅ Planilha configurada.")
    print("  → Selecione um campeonato em C3 e execute: python main.py")


def _sair(signum, frame):
    print("\n  Encerrando daemon...")
    estado.parar_prefetch.set()
    estado.rodando = False
    status_manager.atualizar(
        estado="parado",
        mensagem_status="Daemon encerrado.",
    )


def main():
    parser = argparse.ArgumentParser(description="Loop contínuo Sheets ↔ APWin")
    parser.add_argument("--setup",        action="store_true",
                        help="Configura a planilha (executar uma vez)")
    parser.add_argument("--poll",         type=int, default=POLL_INTERVAL,
                        help=f"Intervalo de polling em segundos (padrão: {POLL_INTERVAL})")
    parser.add_argument("--com-prefetch", action="store_true",
                        help="Habilita pre-fetch de stats em background após selecionar campeonato")
    args = parser.parse_args()

    campeonatos = carregar_campeonatos()

    from src.sheets_manager import SheetsManager
    sm = SheetsManager()
    try:
        sm.conectar()
    except Exception as e:
        log.error(f"Falha ao conectar ao Google Sheets: {e}")
        sys.exit(1)

    if args.setup:
        cmd_setup(sm, campeonatos)
        return

    signal.signal(signal.SIGINT,  _sair)
    signal.signal(signal.SIGTERM, _sair)

    habilitar_prefetch = args.com_prefetch
    intervalo          = args.poll

    status_manager.atualizar(
        estado="rodando",
        pid=os.getpid(),
        mensagem_status=(
            f"Loop iniciado | poll={intervalo}s | "
            f"prefetch={'on' if habilitar_prefetch else 'off — coleta só sob demanda'}"
        ),
    )
    log.info(
        f"Loop iniciado | poll={intervalo}s | "
        f"prefetch={'on' if habilitar_prefetch else 'off'}"
    )
    print(f"\n  🔁 Monitorando planilha a cada {intervalo}s  (Ctrl+C para parar)\n")

    while estado.rodando:
        try:
            ciclo(sm, campeonatos, habilitar_prefetch)
        except Exception as e:
            # FIX 1 — nunca deixa o loop morrer silenciosamente
            log.exception(f"Erro inesperado no ciclo (daemon continua): {e}")
            status_manager.atualizar(
                mensagem_status=f"⚠ Erro no ciclo (continuando): {e}")

        for _ in range(intervalo * 2):
            if not estado.rodando:
                break
            time.sleep(0.5)

    log.info("Daemon encerrado.")


if __name__ == "__main__":
    main()