"""
src/sheets_manager.py
=====================
Gerencia toda a interação com o Google Sheets:
  - Aba 'controle'   → dropdowns interativos de campeonato/partida + status
  - Aba 'dashboard'  → estatísticas com layout visual premium
  - Aba 'historico'  → log acumulado de todas as consultas

Layout da aba controle (1-based para o usuário, 0-based na API):
  Linha 1: (vazio)
  Linha 2: Banner  "⚽ PAINEL DE CONTROLE — APWin Analytics"  [B2:D2]
  Linha 3: Label "🏆 Campeonato" [B3] | Dropdown [C3]
  Linha 4: Label "⚔️ Partida"    [B4] | Dropdown [C4]
  Linha 5: (vazio — separador)
  Linha 6: Label "📊 Status"      [B6] | Valor [C6]
  Linha 7: Label "🕐 Última coleta" [B7] | Valor [C7]
  Linha 8: Label "📈 Última stats"  [B8] | Valor [C8]
  ...
  Coluna E: Lista oculta de partidas para validação do dropdown C4

CORREÇÕES APLICADAS
-------------------
1. [PRINCIPAL] Reconexão automática: tokens OAuth2 do gspread expiram após
   ~1 hora. O método `_executar_com_retry()` detecta TransportError /
   APIError / expiração e reconecta antes de tentar de novo (até 3x com
   backoff exponencial). Antes, qualquer erro de rede ou token expirado
   simplesmente levantava exceção e o daemon parava de escrever na planilha.

2. [PRINCIPAL] `_aba()` com reconexão: a instância de `gspread.Spreadsheet`
   guardada em `self.planilha` fica obsoleta após reconexão — agora ela é
   re-obtida junto com o `gc` na reconexão.

3. Token refresh explícito: as credenciais de Service Account do Google têm
   validade de 3600 s. Adicionamos `google.auth.transport.requests.Request`
   para renovar o access_token antes que expire, sem precisar recriar o
   cliente inteiro toda vez.

4. Backoff + jitter: evita tempestade de requisições após falha temporária
   da API do Google (erro 429 / 503).
"""

import logging
import os
import random
import time
from datetime import datetime
from typing import Optional

import gspread
from google.auth.transport.requests import Request
from google.oauth2.service_account import Credentials
from src.scraper_stats import get_stat

log = logging.getLogger(__name__)

CREDENTIALS_FILE = os.getenv("GSHEETS_CREDENTIALS", "credentials.json")
SPREADSHEET_NAME = os.getenv("GSHEETS_SPREADSHEET", "SOCCER_DATA")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

ABA_CONTROLE   = "controle"
ABA_DASHBOARD  = "dashboard"
ABA_HISTORICO  = "historico"
ABA_DE_FINETTI = "de_finetti"

CELL_CAMPEONATO    = "C3"
CELL_PARTIDA       = "C4"
CELL_STATUS        = "C6"
CELL_ULTIMA_COLETA = "C7"
CELL_ULTIMA_STATS  = "C8"
CELL_CACHE_COUNT   = "C9"
COL_LISTA_PARTIDAS = "E"

CABECALHO_HISTORICO = [
    "Consultado em", "Campeonato", "Mandante", "Visitante", "Data da partida",
    "Vence% M", "Gols M", "xG M",
    "Vence% V", "Gols V", "xG V",
    "Odd 1", "Odd X", "Odd 2",
]

# ═══════════════════════════════════════════════════════════════════════════
# PALETA DE CORES PREMIUM (RGB 0-1)
# ═══════════════════════════════════════════════════════════════════════════

def _rgb(r, g, b):
    return {"red": r/255, "green": g/255, "blue": b/255}

C_PRETO        = _rgb(17,  24,  39)
C_BRANCO       = _rgb(255, 255, 255)
C_CINZA_50     = _rgb(249, 250, 251)
C_CINZA_100    = _rgb(243, 244, 246)
C_CINZA_200    = _rgb(229, 231, 235)
C_CINZA_300    = _rgb(209, 213, 219)
C_CINZA_400    = _rgb(156, 163, 175)
C_CINZA_500    = _rgb(107, 114, 128)
C_CINZA_600    = _rgb(75,  85,  102)
C_CINZA_700    = _rgb(55,  65,  81)
C_CINZA_800    = _rgb(31,  41,  55)

C_AZUL_50      = _rgb(239, 246, 255)
C_AZUL_100     = _rgb(219, 234, 254)
C_AZUL_200     = _rgb(191, 219, 254)
C_AZUL_500     = _rgb(59,  130, 246)
C_AZUL_600     = _rgb(37,  99,  235)
C_AZUL_700     = _rgb(29,  78,  216)
C_AZUL_900     = _rgb(30,  58,  138)

C_ESMERALDA_50  = _rgb(236, 253, 245)
C_ESMERALDA_100 = _rgb(209, 250, 229)
C_ESMERALDA_200 = _rgb(167, 243, 208)
C_ESMERALDA_500 = _rgb(16,  185, 129)
C_ESMERALDA_600 = _rgb(5,   150, 105)
C_ESMERALDA_700 = _rgb(4,   120, 87)
C_ESMERALDA_900 = _rgb(6,   78,  59)

C_VERDE        = _rgb(34,  197, 94)
C_VERDE_BG     = _rgb(220, 252, 231)
C_AMARELO      = _rgb(234, 179, 8)
C_AMARELO_BG   = _rgb(254, 249, 195)
C_VERMELHO     = _rgb(239, 68,  68)
C_VERMELHO_BG  = _rgb(254, 226, 226)
C_LARANJA      = _rgb(249, 115, 22)
C_LARANJA_BG   = _rgb(255, 237, 213)

# ═══════════════════════════════════════════════════════════════════════════
# HELPERS DE API (batchUpdate)
# ═══════════════════════════════════════════════════════════════════════════

def _rng(sid, r1, c1, r2, c2):
    return {"sheetId": sid, "startRowIndex": r1, "endRowIndex": r2,
            "startColumnIndex": c1, "endColumnIndex": c2}

def _fmt(sid, r1, c1, r2, c2, **props):
    return {"repeatCell": {
        "range": _rng(sid, r1, c1, r2, c2),
        "cell": {"userEnteredFormat": props},
        "fields": "userEnteredFormat(" + ",".join(props.keys()) + ")",
    }}

def _merge(sid, r1, c1, r2, c2):
    return {"mergeCells": {"range": _rng(sid, r1, c1, r2, c2), "mergeType": "MERGE_ALL"}}

def _col_w(sid, col, px):
    return {"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "COLUMNS",
                  "startIndex": col, "endIndex": col + 1},
        "properties": {"pixelSize": px}, "fields": "pixelSize"}}

def _row_h(sid, row, px):
    return {"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "ROWS",
                  "startIndex": row, "endIndex": row + 1},
        "properties": {"pixelSize": px}, "fields": "pixelSize"}}

def _tf(bold=False, size=10, color=None, italic=False, font_family="Google Sans"):
    f = {"bold": bold, "fontSize": size, "italic": italic}
    if font_family:
        f["fontFamily"] = font_family
    if color:
        f["foregroundColor"] = color
    return f

def _borders(color=None, style="SOLID", width=1):
    b = {"style": style, "width": width}
    if color:
        b["color"] = color
    return {"borders": {"top": b, "bottom": b, "left": b, "right": b}}

def _halign(a):  return {"horizontalAlignment": a}
def _valign(a):  return {"verticalAlignment": a}

def _freeze(sid, rows=0):
    return {"updateSheetProperties": {
        "properties": {"sheetId": sid,
                       "gridProperties": {"frozenRowCount": rows}},
        "fields": "gridProperties.frozenRowCount"}}

def _hide_col(sid, col):
    return {"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "COLUMNS",
                  "startIndex": col, "endIndex": col + 1},
        "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}}

def _cond_eq(sid, r1, c1, r2, c2, val, bg, fg=None):
    fmt = {"backgroundColor": bg}
    if fg:
        fmt["textFormat"] = {"foregroundColor": fg, "bold": True}
    return {"addConditionalFormatRule": {"rule": {
        "ranges": [_rng(sid, r1, c1, r2, c2)],
        "booleanRule": {
            "condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": val}]},
            "format": fmt,
        }}, "index": 0}}

def _cond_contains(sid, r1, c1, r2, c2, val, bg, fg=None):
    fmt = {"backgroundColor": bg}
    if fg:
        fmt["textFormat"] = {"foregroundColor": fg, "bold": True}
    return {"addConditionalFormatRule": {"rule": {
        "ranges": [_rng(sid, r1, c1, r2, c2)],
        "booleanRule": {
            "condition": {"type": "TEXT_CONTAINS", "values": [{"userEnteredValue": val}]},
            "format": fmt,
        }}, "index": 0}}

def _barra(prob: float, comprimento: int = 20) -> str:
    """Gera uma barra visual de texto proporcional à probabilidade."""
    filled = round(prob * comprimento)
    return "█" * filled + "░" * (comprimento - filled) + f"  {prob:.1%}"


# ═══════════════════════════════════════════════════════════════════════════
# CLASSE PRINCIPAL
# ═══════════════════════════════════════════════════════════════════════════

class SheetsManager:
    def __init__(self):
        self.planilha: Optional[gspread.Spreadsheet] = None
        self._sheet_ids: dict[str, int] = {}
        self._creds: Optional[Credentials] = None   # FIX: guarda creds para refresh

    # ───────────────────────────────────────────────────────────────────────
    # FIX 1 — Conexão com refresh de token
    # ───────────────────────────────────────────────────────────────────────

    def conectar(self) -> "SheetsManager":
        try:
            import streamlit as st
            # Verifica se o bloco de credenciais existe no st.secrets
            usar_secrets = "gcp_service_account" in st.secrets
        except ImportError:
            # Se der erro de importação, significa que estamos rodando o main.py localmente
            usar_secrets = False

        if usar_secrets:
            # NUVEM: Lê o dicionário de credenciais diretamente da memória
            info = dict(st.secrets["gcp_service_account"])
            creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        else:
            # LOCAL: Lê do arquivo JSON físico
            creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)

        gc = gspread.authorize(creds)
        self.planilha = gc.open(SPREADSHEET_NAME)
        log.info(f"Conectado: {self.planilha.title}")
        return self

    def _refresh_token_se_necessario(self) -> None:
        """
        FIX 2 — Renova o access_token se estiver expirado ou prestes a
        expirar (< 300 s de validade). Evita o erro 401 silencioso que faz
        o daemon parar de escrever na planilha após ~1 hora.
        """
        if self._creds is None:
            self.conectar()
            return

        if not self._creds.valid or (
            self._creds.expiry and
            (self._creds.expiry - datetime.utcnow()).total_seconds() < 300
        ):
            log.info("Token Google expirando — renovando...")
            try:
                self._creds.refresh(Request())
                gc = gspread.authorize(self._creds)
                self.planilha = gc.open(SPREADSHEET_NAME)
                self._sheet_ids.clear()
                log.info("Token renovado com sucesso.")
            except Exception as e:
                log.warning(f"Falha no refresh de token, reconectando: {e}")
                self.conectar()

    def _executar_com_retry(self, fn, *args, max_tentativas: int = 3, **kwargs):
        """
        FIX 3 — Wrapper de retry com backoff exponencial + jitter.

        Captura os erros mais comuns da API do Google:
          - APIError 429/503  → espera e tenta novamente
          - TransportError    → reconecta e tenta novamente
          - Qualquer exceção  → reconecta na 2ª tentativa
        """
        ultimo_erro = None
        for tentativa in range(1, max_tentativas + 1):
            try:
                self._refresh_token_se_necessario()
                return fn(*args, **kwargs)
            except gspread.exceptions.APIError as e:
                codigo = getattr(e, "response", None)
                codigo = codigo.status_code if codigo else 0
                ultimo_erro = e
                if codigo in (429, 500, 503):
                    espera = (2 ** tentativa) + random.uniform(0, 1)
                    log.warning(
                        f"APIError {codigo} (tentativa {tentativa}/{max_tentativas})"
                        f" — aguardando {espera:.1f}s antes de repetir..."
                    )
                    time.sleep(espera)
                elif codigo == 401:
                    log.warning("Token inválido (401) — reconectando...")
                    self.conectar()
                else:
                    raise
            except Exception as e:
                ultimo_erro = e
                espera = (2 ** tentativa) + random.uniform(0, 1)
                log.warning(
                    f"Erro na API Google (tentativa {tentativa}/{max_tentativas}): "
                    f"{type(e).__name__}: {e} — reconectando em {espera:.1f}s..."
                )
                time.sleep(espera)
                try:
                    self.conectar()
                except Exception as re:
                    log.error(f"Reconexão falhou: {re}")

        raise RuntimeError(
            f"Falha após {max_tentativas} tentativas. Último erro: {ultimo_erro}"
        )

    def _aba(self, nome: str) -> gspread.Worksheet:
        """FIX 4 — _aba() agora usa _executar_com_retry para tolerar falhas."""
        def _abrir():
            try:
                ws = self.planilha.worksheet(nome)
            except gspread.WorksheetNotFound:
                ws = self.planilha.add_worksheet(title=nome, rows=500, cols=26)
            self._sheet_ids[nome] = ws.id
            return ws
        return self._executar_com_retry(_abrir)

    def _remover_aba_se_existir(self, nome: str) -> None:
        try:
            ws = self.planilha.worksheet(nome)
            self.planilha.del_worksheet(ws)
            log.info(f"Aba '{nome}' removida.")
        except gspread.WorksheetNotFound:
            pass

    def _batch(self, reqs: list) -> None:
        if reqs:
            self._executar_com_retry(
                self.planilha.batch_update, {"requests": reqs})

    def _sid(self, nome: str) -> int:
        if nome not in self._sheet_ids:
            self._sheet_ids[nome] = self._aba(nome).id
        return self._sheet_ids[nome]

    # ═══════════════════════════════════════════════════════════════════════
    # CONTROLE — LEITURA
    # ═══════════════════════════════════════════════════════════════════════

    def ler_controle(self) -> tuple[str, str]:
        def _ler():
            aba = self._aba(ABA_CONTROLE)
            res = aba.batch_get([CELL_CAMPEONATO, CELL_PARTIDA])
            campeonato = (res[0][0][0] if res[0] else "").strip()
            partida    = (res[1][0][0] if res[1] else "").strip()
            return campeonato, partida
        return self._executar_com_retry(_ler)

    # ═══════════════════════════════════════════════════════════════════════
    # CONTROLE — CONFIGURAÇÃO & FORMATAÇÃO PREMIUM
    # ═══════════════════════════════════════════════════════════════════════

    def configurar_controle(self, campeonatos: list[str]) -> None:
        aba = self._aba(ABA_CONTROLE)
        sid = aba.id
        aba.clear()

        dados = [
            ["", "", "", ""],
            ["", "⚽  PAINEL DE CONTROLE  —  APWin Analytics", "", ""],
            ["", "🏆  Campeonato",  campeonatos[0] if campeonatos else "", ""],
            ["", "⚔️  Partida",     "", ""],
            ["", "", "", ""],
            ["", "📊  Status",       "Aguardando execução...", ""],
            ["", "🕐  Última coleta", "—", ""],
            ["", "📈  Última stats",  "—", ""],
            ["", "🗄️  Cache",         "—", ""],
            ["", "", "", ""],
            ["", "", "", ""],
            ["", "ℹ️  COMO UTILIZAR", "", ""],
            ["", "1.", "Selecione o campeonato no dropdown acima (C3)", ""],
            ["", "2.", "Aguarde a coleta automática das partidas", ""],
            ["", "3.", "Escolha a partida no segundo dropdown (C4)", ""],
            ["", "4.", "O dashboard atualiza automaticamente em segundos", ""],
            ["", "", "", ""],
            ["", "💡 Dica", "Partidas já consultadas ficam em cache local por 1 hora", ""],
        ]
        self._executar_com_retry(
            aba.update, "A1", dados, value_input_option="USER_ENTERED")

        self._batch([{"setDataValidation": {
            "range": _rng(sid, 2, 2, 3, 3),
            "rule": {
                "condition": {"type": "ONE_OF_LIST",
                              "values": [{"userEnteredValue": c} for c in campeonatos]},
                "showCustomUi": True, "strict": True,
            },
        }}])
        self._formatar_controle(aba, sid)
        log.info("Aba 'controle' configurada com design premium.")

    def _formatar_controle(self, aba, sid):
        reqs = []

        reqs += [
            _col_w(sid, 0, 20),
            _col_w(sid, 1, 40),
            _col_w(sid, 2, 320),
            _col_w(sid, 3, 20),
            _col_w(sid, 4, 1),
        ]

        reqs += [
            _merge(sid, 1, 1, 2, 4),
            _fmt(sid, 1, 1, 2, 4,
                 backgroundColor=C_AZUL_900,
                 textFormat=_tf(bold=True, size=16, color=C_BRANCO, font_family="Google Sans Display"),
                 **_halign("CENTER"), **_valign("MIDDLE")),
            _row_h(sid, 1, 52),
            _fmt(sid, 1, 1, 2, 4,
                 **_borders(color=C_AZUL_500, width=3)),
        ]

        reqs += [
            _fmt(sid, 2, 1, 3, 2,
                 backgroundColor=C_AZUL_100,
                 textFormat=_tf(bold=True, size=11, color=C_AZUL_700),
                 **_halign("RIGHT"), **_valign("MIDDLE")),
            _fmt(sid, 2, 2, 3, 3,
                 backgroundColor=C_BRANCO,
                 textFormat=_tf(bold=True, size=12, color=C_AZUL_900),
                 **_borders(color=C_AZUL_200, width=2),
                 **_halign("LEFT"), **_valign("MIDDLE")),
            _row_h(sid, 2, 42),
        ]

        reqs += [
            _fmt(sid, 3, 1, 4, 2,
                 backgroundColor=C_ESMERALDA_100,
                 textFormat=_tf(bold=True, size=11, color=C_ESMERALDA_900),
                 **_halign("RIGHT"), **_valign("MIDDLE")),
            _fmt(sid, 3, 2, 4, 3,
                 backgroundColor=C_BRANCO,
                 textFormat=_tf(bold=True, size=12, color=C_ESMERALDA_900),
                 **_borders(color=C_ESMERALDA_200, width=2),
                 **_halign("LEFT"), **_valign("MIDDLE")),
            _row_h(sid, 3, 42),
        ]

        reqs += [_row_h(sid, 4, 10)]

        for r in (5, 6, 7, 8):
            reqs += [
                _fmt(sid, r, 1, r+1, 2,
                     backgroundColor=C_CINZA_100,
                     textFormat=_tf(bold=True, size=10, color=C_CINZA_600),
                     **_halign("RIGHT"), **_valign("MIDDLE")),
                _fmt(sid, r, 2, r+1, 3,
                     backgroundColor=C_CINZA_50,
                     textFormat=_tf(size=10, color=C_CINZA_700),
                     **_halign("LEFT"), **_valign("MIDDLE")),
                _row_h(sid, r, 32),
            ]
        reqs += [
            _fmt(sid, 8, 2, 9, 3,
                 backgroundColor=C_AZUL_50,
                 textFormat=_tf(bold=True, size=10, color=C_AZUL_700),
                 **_halign("LEFT"), **_valign("MIDDLE")),
        ]

        reqs += [
            _merge(sid, 11, 1, 12, 4),
            _fmt(sid, 11, 1, 12, 4,
                 backgroundColor=C_CINZA_800,
                 textFormat=_tf(bold=True, size=11, color=C_BRANCO),
                 **_halign("LEFT"), **_valign("MIDDLE")),
            _row_h(sid, 11, 32),
        ]

        for r in range(12, 16):
            reqs += [
                _fmt(sid, r, 1, r+1, 2,
                     backgroundColor=C_CINZA_100,
                     textFormat=_tf(bold=True, size=10, color=C_CINZA_500),
                     **_halign("CENTER"), **_valign("MIDDLE")),
                _fmt(sid, r, 2, r+1, 3,
                     backgroundColor=C_CINZA_50,
                     textFormat=_tf(size=10, color=C_CINZA_600),
                     **_halign("LEFT"), **_valign("MIDDLE")),
                _row_h(sid, r, 28),
            ]

        reqs += [
            _merge(sid, 17, 1, 18, 4),
            _fmt(sid, 17, 1, 18, 4,
                 backgroundColor=C_AMARELO_BG,
                 textFormat=_tf(size=10, color=C_AMARELO, italic=True),
                 **_halign("CENTER"), **_valign("MIDDLE")),
            _row_h(sid, 17, 28),
        ]

        reqs += [
            _fmt(sid, 2, 1, 4, 3, **_borders(color=C_CINZA_200, width=1)),
            _fmt(sid, 5, 1, 9, 3, **_borders(color=C_CINZA_200, width=1)),
            _fmt(sid, 11, 1, 16, 3, **_borders(color=C_CINZA_200, width=1)),
        ]

        reqs.append(_hide_col(sid, 4))
        self._batch(reqs)

    def atualizar_lista_partidas(self, partidas: list, campeonato: str,
                                coletado_em: str, n_cache: int = 0) -> None:
        def _atualizar():
            aba = self._aba(ABA_CONTROLE)
            sid = aba.id
            aba.update(f"{COL_LISTA_PARTIDAS}1",
                       [[p] for p in partidas], value_input_option="USER_ENTERED")
            if len(partidas) < 200:
                aba.batch_clear([f"E{len(partidas)+1}:E200"])

            self._batch([{"setDataValidation": {
                "range": _rng(sid, 3, 2, 4, 3),
                "rule": {
                    "condition": {
                        "type": "ONE_OF_RANGE",
                        "values": [{"userEnteredValue":
                                    f"={ABA_CONTROLE}!$E$1:$E${len(partidas)}"}],
                    },
                    "showCustomUi": True, "strict": False,
                },
            }}])
            aba.update(CELL_ULTIMA_COLETA, [[coletado_em]])
            cache_label = f"{n_cache} / {len(partidas)} partidas em cache"
            aba.update(CELL_CACHE_COUNT, [[cache_label]])
            log.info(f"{len(partidas)} partidas no dropdown | cache: {n_cache}.")

        self._executar_com_retry(_atualizar)

    def atualizar_status(self, mensagem, tipo="info"):
        def _atualizar():
            aba = self._aba(ABA_CONTROLE)
            sid = aba.id
            aba.update(CELL_STATUS, [[mensagem]])
            aba.update(CELL_ULTIMA_STATS,
                       [[datetime.now().strftime("%d/%m/%Y %H:%M:%S")]])

            cores_bg = {"ok": C_VERDE_BG, "erro": C_VERMELHO_BG, "info": C_AZUL_100, "warn": C_AMARELO_BG}
            cores_fg = {"ok": C_VERDE, "erro": C_VERMELHO, "info": C_AZUL_600, "warn": C_AMARELO}
            bg = cores_bg.get(tipo, C_CINZA_100)
            fg = cores_fg.get(tipo, C_CINZA_600)

            self._batch([
                _fmt(sid, 5, 2, 6, 3,
                     backgroundColor=bg,
                     textFormat=_tf(bold=True, size=11, color=fg),
                     **_halign("LEFT"), **_valign("MIDDLE"))
            ])

        self._executar_com_retry(_atualizar)

    def atualizar_progresso_prefetch(self, atual, total, campeonato):
        msg = f"⏳ Pré-carregando '{campeonato}': {atual}/{total} partidas..."
        self._executar_com_retry(
            self._aba(ABA_CONTROLE).update, CELL_STATUS, [[msg]])

    # ═══════════════════════════════════════════════════════════════════════
    # DASHBOARD — ESCRITA & FORMATAÇÃO PREMIUM
    # ═══════════════════════════════════════════════════════════════════════

    def escrever_dashboard(self, info, forma_m, stats_m, forma_v, stats_v, odds):
        def _escrever():
            self._remover_aba_se_existir(ABA_DASHBOARD)
            aba = self._aba(ABA_DASHBOARD)
            sid = aba.id

            linhas, m = self._montar_dashboard(
                info, forma_m, stats_m, forma_v, stats_v, odds)

            aba.update("A1", linhas, value_input_option="USER_ENTERED")
            time.sleep(0.6)
            self._formatar_dashboard(sid, info["mandante"], info["visitante"], m)
            log.info(f"Dashboard: {len(linhas)} linhas escritas.")

        self._executar_com_retry(_escrever)

    def _montar_dashboard(self, info, forma_m, stats_m, forma_v, stats_v, odds):
        mandante  = info["mandante"]
        visitante = info["visitante"]
        linhas: list = []
        m: dict = {}

        def row(*c):
            linhas.append(list(c))
            return len(linhas) - 1

        row()
        m["banner"]   = row("", f"⚽  {mandante}   ×   {visitante}")
        m["info_bar"] = row("", info["campeonato"], "",
                            info["data_hora"], "", info["estadio"])

        kpi_labels = [
            "", "Vence %", "Gols / jogo", "xG médio",
            "", "xG médio", "Gols / jogo", "Vence %",
        ]
        kpi_values = [
            "",
            get_stat(stats_m, "Vence %"),
            get_stat(stats_m, "Gols"),
            get_stat(stats_m, "xG"),
            "",
            get_stat(stats_v, "xG"),
            get_stat(stats_v, "Gols"),
            get_stat(stats_v, "Vence %"),
        ]
        m["kpi_labels"] = row(*kpi_labels)
        m["kpi_values"] = row(*kpi_values)
        row()

        def _bloco_time(lado, forma, stats):
            prefix = "m" if lado == "mandante" else "v"
            nome   = mandante if lado == "mandante" else visitante

            m[f"sec_forma_{prefix}"] = row("", f"📋  FORMA RECENTE — {nome.upper()}")
            row()

            cab = forma[0] if forma else []
            m[f"forma_cab_{prefix}"] = row("", *cab)
            m[f"forma_rows_{prefix}"] = []
            for data_row in forma[1:]:
                idx = row("", *data_row)
                m[f"forma_rows_{prefix}"].append(idx)

            row()
            m[f"sec_stats_{prefix}"] = row("", f"📊  ESTATÍSTICAS — {nome.upper()}")
            row()

            cab2 = stats[0] if stats else []
            m[f"stats_cab_{prefix}"] = row("", *cab2)
            
            # [CORREÇÃO]: Agora rastreamos as linhas das estatísticas 
            # para aplicar o batchUpdate posteriormente.
            m[f"stats_rows_{prefix}"] = []
            for data_row in stats[1:]:
                idx = row("", *data_row)
                m[f"stats_rows_{prefix}"].append(idx)

            row()

        _bloco_time("mandante",  forma_m, stats_m)
        _bloco_time("visitante", forma_v, stats_v)

        m["sec_odds"] = row("", "💰  ODDS DO MERCADO")
        row()
        cab_odds = odds[0] if odds else []
        m["cab_odds"] = row("", *cab_odds)
        m["odds_rows"] = []
        for data_row in odds[1:]:
            idx = row("", *data_row)
            m["odds_rows"].append(idx)

        return linhas, m

    def _formatar_dashboard(self, sid, mandante, visitante, m):
        reqs = []

        # 1. LARGURAS DE COLUNA (Grid baseado na aba De Finetti)
        reqs += [
            _col_w(sid, 0, 16),   # A: Margem esquerda (limpa)
            _col_w(sid, 1, 160),  # B: Rótulos principais
            _col_w(sid, 2, 85),   # C: Valores numéricos
            _col_w(sid, 3, 85),   # D: Valores numéricos
            _col_w(sid, 4, 85),   # E: Divisor central / Valores
            _col_w(sid, 5, 85),   # F: Valores numéricos
            _col_w(sid, 6, 85),   # G: Valores numéricos
            _col_w(sid, 7, 85),   # H: Valores numéricos
            _col_w(sid, 8, 16),   # I: Margem direita (limpa)
        ]

        # 2. BANNER & INFO (Tipografia e espaçamentos do De Finetti)
        if "banner" in m:
            ri = m["banner"]
            reqs += [
                _merge(sid, ri, 1, ri+1, 8),
                _fmt(sid, ri, 1, ri+1, 8,
                     backgroundColor=C_AZUL_900,
                     textFormat=_tf(bold=True, size=16, color=C_BRANCO, font_family="Google Sans Display"),
                     **_halign("CENTER"), **_valign("MIDDLE")),
                _row_h(sid, ri, 48), # Altura idêntica ao De Finetti
            ]

        if "info_bar" in m:
            ri = m["info_bar"]
            reqs += [
                _fmt(sid, ri, 1, ri+1, 8,
                     backgroundColor=C_CINZA_800,
                     textFormat=_tf(size=10, color=C_CINZA_300),
                     **_halign("CENTER"), **_valign("MIDDLE")),
                _row_h(sid, ri, 26),
            ]

        # 3. KPIs GLOBAIS (Mantido o padrão de cores dos times)
        if "kpi_labels" in m and "kpi_values" in m:
            rl = m["kpi_labels"]
            rv = m["kpi_values"]
            reqs += [
                _fmt(sid, rl, 1, rl+1, 8,
                     backgroundColor=C_CINZA_100, textFormat=_tf(size=9, color=C_CINZA_500), **_halign("CENTER")),
                _row_h(sid, rl, 22),
                _fmt(sid, rv, 1, rv+1, 5,
                     backgroundColor=C_AZUL_50, textFormat=_tf(bold=True, size=14, color=C_AZUL_700),
                     **_halign("CENTER"), **_valign("MIDDLE")),
                _fmt(sid, rv, 4, rv+1, 8,
                     backgroundColor=C_ESMERALDA_50, textFormat=_tf(bold=True, size=14, color=C_ESMERALDA_700),
                     **_halign("CENTER"), **_valign("MIDDLE")),
                _row_h(sid, rv, 40),
            ]

        # 4. FORMA E ESTATÍSTICAS (Lógica de Color Coding e Zebra)
        for lado, cor_sec, cor_cab, cor_bg_claro in [
            ("m", C_AZUL_900, C_AZUL_600, C_AZUL_50),          # Paleta Mandante
            ("v", C_ESMERALDA_900, C_ESMERALDA_600, C_ESMERALDA_50), # Paleta Visitante
        ]:
            # --- Seção: Forma Recente ---
            if f"sec_forma_{lado}" in m:
                ri = m[f"sec_forma_{lado}"]
                reqs += [
                    _merge(sid, ri, 1, ri+1, 8),
                    _fmt(sid, ri, 1, ri+1, 8, backgroundColor=cor_sec,
                         textFormat=_tf(bold=True, size=12, color=C_BRANCO, font_family="Google Sans"),
                         **_halign("LEFT"), **_valign("MIDDLE")),
                    _row_h(sid, ri, 36),
                ]

            if f"forma_cab_{lado}" in m:
                ri = m[f"forma_cab_{lado}"]
                reqs += [
                    _fmt(sid, ri, 1, ri+1, 8, backgroundColor=cor_cab, textFormat=_tf(bold=True, size=10, color=C_BRANCO),
                         **_halign("CENTER"), **_valign("MIDDLE")),
                    _row_h(sid, ri, 28),
                ]

            # [INSIGHT] Alternância matemática (i % 2) para Efeito Zebra
            for i, fr in enumerate(m.get(f"forma_rows_{lado}", [])):
                bg = cor_bg_claro if i % 2 == 0 else C_BRANCO
                reqs += [
                    _fmt(sid, fr, 1, fr+1, 8, backgroundColor=bg, **_halign("CENTER"), **_valign("MIDDLE")),
                    _row_h(sid, fr, 28),
                ]

            # --- Seção: Estatísticas ---
            if f"sec_stats_{lado}" in m:
                ri = m[f"sec_stats_{lado}"]
                reqs += [
                    _merge(sid, ri, 1, ri+1, 8),
                    _fmt(sid, ri, 1, ri+1, 8, backgroundColor=cor_sec,
                         textFormat=_tf(bold=True, size=12, color=C_BRANCO, font_family="Google Sans"),
                         **_halign("LEFT"), **_valign("MIDDLE")),
                    _row_h(sid, ri, 36),
                ]

            if f"stats_cab_{lado}" in m:
                ri = m[f"stats_cab_{lado}"]
                reqs += [
                    _fmt(sid, ri, 1, ri+1, 8, backgroundColor=cor_cab, textFormat=_tf(bold=True, size=10, color=C_BRANCO),
                         **_halign("CENTER"), **_valign("MIDDLE")),
                    _row_h(sid, ri, 28),
                ]

            for i, sr in enumerate(m.get(f"stats_rows_{lado}", [])):
                bg = cor_bg_claro if i % 2 == 0 else C_BRANCO
                reqs += [
                    _fmt(sid, sr, 1, sr+1, 8, backgroundColor=bg, **_halign("CENTER"), **_valign("MIDDLE")),
                    # Destaque à esquerda apenas para a primeira coluna (Rótulos)
                    _fmt(sid, sr, 1, sr+1, 2, textFormat=_tf(bold=True, size=10, color=C_CINZA_700), **_halign("LEFT")),
                    _row_h(sid, sr, 28),
                ]

        # 5. ODDS DO MERCADO (Estilo da "Matriz" do De Finetti)
        if "sec_odds" in m:
            ri = m["sec_odds"]
            reqs += [
                _merge(sid, ri, 1, ri+1, 8),
                _fmt(sid, ri, 1, ri+1, 8, backgroundColor=C_CINZA_800,
                     textFormat=_tf(bold=True, size=12, color=C_BRANCO, font_family="Google Sans"),
                     **_halign("LEFT"), **_valign("MIDDLE")),
                _row_h(sid, ri, 36),
            ]

        if "cab_odds" in m:
            ri = m["cab_odds"]
            reqs += [
                _fmt(sid, ri, 1, ri+1, 8, backgroundColor=C_CINZA_700, textFormat=_tf(bold=True, size=10, color=C_BRANCO),
                     **_halign("CENTER"), **_valign("MIDDLE")),
                _row_h(sid, ri, 28),
            ]

        for i, odds_r in enumerate(m.get("odds_rows", [])):
            bg = C_AMARELO_BG if i % 2 == 0 else C_BRANCO
            reqs += [
                _fmt(sid, odds_r, 1, odds_r+1, 8, backgroundColor=bg, textFormat=_tf(bold=True, size=11, color=C_CINZA_800),
                     **_halign("CENTER"), **_valign("MIDDLE")),
                _row_h(sid, odds_r, 30),
            ]

        # Congelar as 4 primeiras linhas (mantém banner, info e KPIs fixos no topo no scroll)
        reqs.append(_freeze(sid, rows=4))
        self._batch(reqs)

    # ═══════════════════════════════════════════════════════════════════════
    # HISTÓRICO — ESCRITA & FORMATAÇÃO PREMIUM
    # ═══════════════════════════════════════════════════════════════════════

    def escrever_historico(self, info, stats_m, stats_v, odds):
        def _escrever():
            aba = self._aba(ABA_HISTORICO)
            sid = aba.id
            todos = aba.get_all_values()

            if not todos or todos[0] != CABECALHO_HISTORICO:
                aba.clear()
                aba.insert_row(CABECALHO_HISTORICO, index=1)
                n = len(CABECALHO_HISTORICO)
                widths = [150, 170, 150, 150, 120, 75, 65, 65, 75, 65, 65, 65, 65, 65]

                reqs = [
                    _fmt(sid, 0, 0, 1, n,
                         backgroundColor=C_PRETO,
                         textFormat=_tf(bold=True, size=10, color=C_BRANCO,
                                        font_family="Google Sans"),
                         **_halign("CENTER"), **_valign("MIDDLE")),
                    _freeze(sid, rows=1),
                    _row_h(sid, 0, 32),
                ]
                reqs += [_col_w(sid, i, w) for i, w in enumerate(widths[:n])]
                self._batch(reqs)
                todos = [CABECALHO_HISTORICO]

            odds_vals = ["", "", ""]
            for r in odds:
                if r and not r[0] and len(r) >= 4:
                    odds_vals = r[1:4]; break

            nova = [
                datetime.now().strftime("%d/%m/%Y %H:%M"),
                info["campeonato"], info["mandante"], info["visitante"],
                info["data_hora"],
                get_stat(stats_m, "Vence %"), get_stat(stats_m, "Gols"),
                get_stat(stats_m, "xG"),
                get_stat(stats_v, "Vence %"), get_stat(stats_v, "Gols"),
                get_stat(stats_v, "xG"), *odds_vals,
            ]
            nova_linha = len(todos) + 1
            aba.insert_row(nova, index=nova_linha)
            par = nova_linha % 2 == 0

            reqs = [
                _fmt(sid, nova_linha-1, 0, nova_linha, len(CABECALHO_HISTORICO),
                     backgroundColor=C_CINZA_50 if par else C_BRANCO,
                     **_halign("CENTER"), **_valign("MIDDLE")),
                _fmt(sid, nova_linha-1, 0, nova_linha, 1,
                     textFormat=_tf(size=9, color=C_CINZA_400)),
                _fmt(sid, nova_linha-1, 1, nova_linha, 4,
                     textFormat=_tf(bold=True, size=10, color=C_CINZA_800)),
                _fmt(sid, nova_linha-1, 4, nova_linha, len(CABECALHO_HISTORICO),
                     textFormat=_tf(size=10, color=C_CINZA_600)),
                _row_h(sid, nova_linha-1, 26),
            ]
            self._batch(reqs)
            log.info(f"Histórico: linha {nova_linha} inserida.")

        self._executar_com_retry(_escrever)

    # ═══════════════════════════════════════════════════════════════════════
    # DE FINETTI — ESCRITA & FORMATAÇÃO
    # ═══════════════════════════════════════════════════════════════════════

    def escrever_de_finetti(self, info: dict, xg_m: float, xg_v: float) -> None:
        """
        Calcula e publica a análise de De Finetti na aba 'de_finetti'.

        Args:
            info:  dicionário com 'mandante', 'visitante', 'campeonato',
                   'data_hora' (mesmo formato de escrever_dashboard)
            xg_m:  xG do mandante (float > 0)
            xg_v:  xG do visitante (float > 0)
        """
        from src.de_finetti import calcular, MAX_GOLS

        def _escrever():
            self._remover_aba_se_existir(ABA_DE_FINETTI)
            aba = self._aba(ABA_DE_FINETTI)
            sid = aba.id

            mandante  = info.get("mandante",  "Mandante")
            visitante = info.get("visitante", "Visitante")

            res  = calcular(xg_m, xg_v)
            gols = list(range(MAX_GOLS + 1))

            linhas: list[list] = []
            m: dict = {}

            def row(*c):
                linhas.append(list(c))
                return len(linhas) - 1

            # ── Banner ───────────────────────────────────────────────────
            row()
            m["banner"] = row(
                "", f"⚽  {mandante}   ×   {visitante}   —   Análise de De Finetti"
            )
            m["info_bar"] = row(
                "", info.get("campeonato", ""), "",
                info.get("data_hora", ""),
                "", f"xG: {xg_m:.2f} × {xg_v:.2f}"
            )
            row()

            # ── Seção 1: Distribuição de Poisson ─────────────────────────
            m["sec_poisson"] = row("", "📐  DISTRIBUIÇÃO DE POISSON")
            row()
            m["pois_cab"] = row(
                "", "Gols esperados (xG)", *[str(k) for k in gols]
            )
            m["pois_m"] = row(
                "", f"🏠  {mandante}  (xG = {xg_m:.2f})",
                *[f"{p:.2%}" for p in res.probs_poisson_m]
            )
            m["pois_v"] = row(
                "", f"✈️  {visitante}  (xG = {xg_v:.2f})",
                *[f"{p:.2%}" for p in res.probs_poisson_v]
            )
            row()

            # ── Seção 2: Matriz de placares ───────────────────────────────
            m["sec_matriz"] = row(
                "", "⚽  MATRIZ DE PLACARES  (P[gols_casa = i, gols_fora = j])"
            )
            row()
            m["mat_cab"] = row("", "Casa ↓  Fora →", *[str(k) for k in gols])
            m["mat_rows"]  = []
            m["mat_diag"]  = []
            m["mat_lower"] = []
            m["mat_upper"] = []

            for i, linha_mat in enumerate(res.matriz):
                ri = row("", str(i), *[f"{v:.2%}" for v in linha_mat])
                m["mat_rows"].append(ri)
                for j in range(len(linha_mat)):
                    col_offset = 2 + j
                    if i == j:
                        m["mat_diag"].append((ri, col_offset))
                    elif i > j:
                        m["mat_lower"].append((ri, col_offset))
                    else:
                        m["mat_upper"].append((ri, col_offset))
            row()

            # ── Seção 3: Probabilidades do resultado ──────────────────────
            m["sec_prob"] = row("", "📊  PROBABILIDADES DO RESULTADO")
            row()
            m["prob_cab"] = row("", "Resultado", "Probabilidade", "Barra visual")
            m["prob_m"]   = row(
                "", f"🏠  {mandante} vence",
                f"{res.resultado.mandante:.1%}",
                _barra(res.resultado.mandante),
            )
            m["prob_emp"] = row(
                "", "🤝  Empate",
                f"{res.resultado.empate:.1%}",
                _barra(res.resultado.empate),
            )
            m["prob_v"]   = row(
                "", f"✈️  {visitante} vence",
                f"{res.resultado.visitante:.1%}",
                _barra(res.resultado.visitante),
            )
            row()

            # ── Seção 4: Distância de De Finetti ──────────────────────────
            m["sec_finetti"] = row("", "🎯  DISTÂNCIA DE DE FINETTI")
            row()
            m["fin_cab"] = row("", "Evento", "Distância", "Interpretação")

            dist    = res.distancia
            min_d   = min(
                dist.se_mandante_ganhar,
                dist.se_empate,
                dist.se_visitante_ganhar,
            )

            def _fav_label(d_val):
                return (
                    "★  FAVORITO  (menor distância)"
                    if abs(d_val - min_d) < 1e-9 else ""
                )

            m["fin_m"]   = row(
                "", f"🏠  {mandante} vence",
                round(dist.se_mandante_ganhar, 6),
                _fav_label(dist.se_mandante_ganhar),
            )
            m["fin_emp"] = row(
                "", "🤝  Empate",
                round(dist.se_empate, 6),
                _fav_label(dist.se_empate),
            )
            m["fin_v"]   = row(
                "", f"✈️  {visitante} vence",
                round(dist.se_visitante_ganhar, 6),
                _fav_label(dist.se_visitante_ganhar),
            )
            row()
            m["rodape"] = row(
                "",
                "ℹ️ Quanto menor a distância de De Finetti, maior a "
                "'confiança' do modelo naquele desfecho.",
            )

            aba.update("A1", linhas, value_input_option="USER_ENTERED")
            time.sleep(0.5)
            self._formatar_de_finetti(sid, m, res)
            log.info(
                f"De Finetti: {len(linhas)} linhas escritas "
                f"para {mandante} × {visitante}."
            )

        self._executar_com_retry(_escrever)

    def _formatar_de_finetti(self, sid, m, res) -> None:
        """Aplica formatação premium à aba de_finetti via batchUpdate."""
        from src.de_finetti import MAX_GOLS

        N          = MAX_GOLS + 1   # 6 colunas de gols (0–5)
        TOTAL_COLS = N + 2          # espaçador A + label B + 6 valores

        reqs = []

        # ── Larguras de coluna ─────────────────────────────────────────
        reqs += [
            _col_w(sid, 0, 16),
            _col_w(sid, 1, 210),
            _col_w(sid, 2, 90),
            _col_w(sid, 3, 90),
            _col_w(sid, 4, 90),
            _col_w(sid, 5, 90),
            _col_w(sid, 6, 90),
            _col_w(sid, 7, 90),
            _col_w(sid, 8, 270),
        ]

        # ── Banner ────────────────────────────────────────────────────
        if "banner" in m:
            ri = m["banner"]
            reqs += [
                _merge(sid, ri, 1, ri+1, TOTAL_COLS + 2),
                _fmt(sid, ri, 1, ri+1, TOTAL_COLS + 2,
                     backgroundColor=C_AZUL_900,
                     textFormat=_tf(bold=True, size=16, color=C_BRANCO,
                                    font_family="Google Sans Display"),
                     **_halign("CENTER"), **_valign("MIDDLE")),
                _row_h(sid, ri, 48),
            ]

        if "info_bar" in m:
            ri = m["info_bar"]
            reqs += [
                _fmt(sid, ri, 1, ri+1, TOTAL_COLS + 2,
                     backgroundColor=C_CINZA_800,
                     textFormat=_tf(size=10, color=C_CINZA_300),
                     **_halign("CENTER"), **_valign("MIDDLE")),
                _row_h(sid, ri, 26),
            ]

        # ── Cabeçalhos de seção ───────────────────────────────────────
        for sec_key, cor in [
            ("sec_poisson", C_AZUL_900),
            ("sec_matriz",  C_CINZA_800),
            ("sec_prob",    C_ESMERALDA_900),
            ("sec_finetti", C_AZUL_900),
        ]:
            if sec_key in m:
                ri = m[sec_key]
                reqs += [
                    _merge(sid, ri, 1, ri+1, TOTAL_COLS + 2),
                    _fmt(sid, ri, 1, ri+1, TOTAL_COLS + 2,
                         backgroundColor=cor,
                         textFormat=_tf(bold=True, size=12, color=C_BRANCO,
                                        font_family="Google Sans"),
                         **_halign("LEFT"), **_valign("MIDDLE")),
                    _row_h(sid, ri, 36),
                ]

        # ── Poisson ───────────────────────────────────────────────────
        if "pois_cab" in m:
            ri = m["pois_cab"]
            reqs += [
                _fmt(sid, ri, 1, ri+1, TOTAL_COLS,
                     backgroundColor=C_AZUL_600,
                     textFormat=_tf(bold=True, size=10, color=C_BRANCO),
                     **_halign("CENTER"), **_valign("MIDDLE")),
                _row_h(sid, ri, 28),
            ]
        for row_key, bg in [("pois_m", C_AZUL_50), ("pois_v", C_ESMERALDA_50)]:
            if row_key in m:
                ri = m[row_key]
                reqs += [
                    _fmt(sid, ri, 1, ri+1, TOTAL_COLS,
                         backgroundColor=bg,
                         **_halign("CENTER"), **_valign("MIDDLE")),
                    _row_h(sid, ri, 30),
                ]

        # ── Matriz de placares ────────────────────────────────────────
        if "mat_cab" in m:
            ri = m["mat_cab"]
            reqs += [
                _fmt(sid, ri, 1, ri+1, TOTAL_COLS,
                     backgroundColor=C_CINZA_700,
                     textFormat=_tf(bold=True, size=10, color=C_BRANCO),
                     **_halign("CENTER"), **_valign("MIDDLE")),
                _row_h(sid, ri, 28),
            ]
        for ri in m.get("mat_rows", []):
            reqs += [
                _fmt(sid, ri, 1, ri+1, TOTAL_COLS,
                     backgroundColor=C_CINZA_50,
                     **_halign("CENTER"), **_valign("MIDDLE")),
                _fmt(sid, ri, 1, ri+1, 2,
                     backgroundColor=C_CINZA_200,
                     textFormat=_tf(bold=True, size=10, color=C_CINZA_700),
                     **_halign("CENTER")),
                _row_h(sid, ri, 28),
            ]
        for (ri, ci) in m.get("mat_diag", []):
            reqs.append(_fmt(sid, ri, ci, ri+1, ci+1,
                             backgroundColor=C_AMARELO_BG,
                             textFormat=_tf(bold=True, size=9, color=C_AMARELO)))
        for (ri, ci) in m.get("mat_lower", []):
            reqs.append(_fmt(sid, ri, ci, ri+1, ci+1,
                             backgroundColor=C_AZUL_100,
                             textFormat=_tf(size=9, color=C_AZUL_700)))
        for (ri, ci) in m.get("mat_upper", []):
            reqs.append(_fmt(sid, ri, ci, ri+1, ci+1,
                             backgroundColor=C_ESMERALDA_100,
                             textFormat=_tf(size=9, color=C_ESMERALDA_700)))

        # ── Probabilidades do resultado ───────────────────────────────
        if "prob_cab" in m:
            ri = m["prob_cab"]
            reqs += [
                _fmt(sid, ri, 1, ri+1, 5,
                     backgroundColor=C_ESMERALDA_600,
                     textFormat=_tf(bold=True, size=10, color=C_BRANCO),
                     **_halign("CENTER"), **_valign("MIDDLE")),
                _row_h(sid, ri, 28),
            ]
        prob_rows = [
            ("prob_m",   res.resultado.mandante,   C_AZUL_50,      C_AZUL_700),
            ("prob_emp", res.resultado.empate,     C_AMARELO_BG,   C_AMARELO),
            ("prob_v",   res.resultado.visitante,  C_ESMERALDA_50, C_ESMERALDA_700),
        ]
        for (key, prob, bg, fg) in prob_rows:
            if key in m:
                ri = m[key]
                reqs += [
                    _fmt(sid, ri, 1, ri+1, 5,
                         backgroundColor=bg,
                         **_halign("CENTER"), **_valign("MIDDLE")),
                    _fmt(sid, ri, 2, ri+1, 3,
                         textFormat=_tf(bold=True, size=14, color=fg),
                         **_halign("CENTER")),
                    _row_h(sid, ri, 36),
                ]

        # ── Distância de De Finetti ───────────────────────────────────
        if "fin_cab" in m:
            ri = m["fin_cab"]
            reqs += [
                _fmt(sid, ri, 1, ri+1, 5,
                     backgroundColor=C_AZUL_600,
                     textFormat=_tf(bold=True, size=10, color=C_BRANCO),
                     **_halign("CENTER"), **_valign("MIDDLE")),
                _row_h(sid, ri, 28),
            ]
        dist  = res.distancia
        min_d = min(
            dist.se_mandante_ganhar,
            dist.se_empate,
            dist.se_visitante_ganhar,
        )
        for (key, d_val) in [
            ("fin_m",   dist.se_mandante_ganhar),
            ("fin_emp", dist.se_empate),
            ("fin_v",   dist.se_visitante_ganhar),
        ]:
            if key in m:
                ri     = m[key]
                is_fav = abs(d_val - min_d) < 1e-9
                bg     = C_VERDE_BG  if is_fav else C_CINZA_50
                fg_val = C_VERDE     if is_fav else C_CINZA_700
                reqs += [
                    _fmt(sid, ri, 1, ri+1, 5,
                         backgroundColor=bg,
                         **_halign("CENTER"), **_valign("MIDDLE")),
                    _fmt(sid, ri, 2, ri+1, 3,
                         textFormat=_tf(bold=is_fav, size=12, color=fg_val),
                         **_halign("CENTER")),
                    _fmt(sid, ri, 3, ri+1, 5,
                         textFormat=_tf(bold=is_fav, size=10, color=fg_val),
                         **_halign("LEFT")),
                    _row_h(sid, ri, 34),
                ]

        # ── Rodapé ────────────────────────────────────────────────────
        if "rodape" in m:
            ri = m["rodape"]
            reqs += [
                _merge(sid, ri, 1, ri+1, TOTAL_COLS + 2),
                _fmt(sid, ri, 1, ri+1, TOTAL_COLS + 2,
                     backgroundColor=C_CINZA_100,
                     textFormat=_tf(size=9, color=C_CINZA_500, italic=True),
                     **_halign("CENTER"), **_valign("MIDDLE")),
                _row_h(sid, ri, 28),
            ]

        reqs.append(_freeze(sid, rows=3))
        self._batch(reqs)

    # ═══════════════════════════════════════════════════════════════════════
    # LIMPEZA — REMOÇÃO DE ABA LEGADA
    # ═══════════════════════════════════════════════════════════════════════

    def remover_dados_brutos(self):
        self._remover_aba_se_existir("dados_brutos")