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
"""

import logging
import os
import time
from datetime import datetime
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials
from src.scraper_stats import get_stat

log = logging.getLogger(__name__)

CREDENTIALS_FILE = os.getenv("GSHEETS_CREDENTIALS", "credentials.json")
SPREADSHEET_NAME = os.getenv("GSHEETS_SPREADSHEET", "SOCCER_DATA")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

ABA_CONTROLE  = "controle"
ABA_DASHBOARD = "dashboard"
ABA_HISTORICO = "historico"

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

# Neutros
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

# Primários (Azul — Mandante)
C_AZUL_50      = _rgb(239, 246, 255)
C_AZUL_100     = _rgb(219, 234, 254)
C_AZUL_200     = _rgb(191, 219, 254)
C_AZUL_500     = _rgb(59,  130, 246)
C_AZUL_600     = _rgb(37,  99,  235)
C_AZUL_700     = _rgb(29,  78,  216)
C_AZUL_900     = _rgb(30,  58,  138)

# Esmeralda (Visitante)
C_ESMERALDA_50  = _rgb(236, 253, 245)
C_ESMERALDA_100 = _rgb(209, 250, 229)
C_ESMERALDA_200 = _rgb(167, 243, 208)
C_ESMERALDA_500 = _rgb(16,  185, 129)
C_ESMERALDA_600 = _rgb(5,   150, 105)
C_ESMERALDA_700 = _rgb(4,   120, 87)
C_ESMERALDA_900 = _rgb(6,   78,  59)

# Semânticos
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

# ═══════════════════════════════════════════════════════════════════════════
# CLASSE PRINCIPAL
# ═══════════════════════════════════════════════════════════════════════════

class SheetsManager:
    def __init__(self):
        self.planilha: Optional[gspread.Spreadsheet] = None
        self._sheet_ids: dict[str, int] = {}

    def conectar(self) -> "SheetsManager":
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
        gc    = gspread.authorize(creds)
        self.planilha = gc.open(SPREADSHEET_NAME)
        log.info(f"Conectado: {self.planilha.title}")
        return self

    def _aba(self, nome: str) -> gspread.Worksheet:
        try:
            ws = self.planilha.worksheet(nome)
        except gspread.WorksheetNotFound:
            ws = self.planilha.add_worksheet(title=nome, rows=500, cols=26)
        self._sheet_ids[nome] = ws.id
        return ws

    def _remover_aba_se_existir(self, nome: str) -> None:
        try:
            ws = self.planilha.worksheet(nome)
            self.planilha.del_worksheet(ws)
            log.info(f"Aba '{nome}' removida.")
        except gspread.WorksheetNotFound:
            pass

    def _batch(self, reqs: list) -> None:
        if reqs:
            self.planilha.batch_update({"requests": reqs})

    def _sid(self, nome: str) -> int:
        if nome not in self._sheet_ids:
            self._sheet_ids[nome] = self._aba(nome).id
        return self._sheet_ids[nome]

    # ═══════════════════════════════════════════════════════════════════════
    # CONTROLE — LEITURA
    # ═══════════════════════════════════════════════════════════════════════

    def ler_controle(self) -> tuple[str, str]:
        aba = self._aba(ABA_CONTROLE)
        res = aba.batch_get([CELL_CAMPEONATO, CELL_PARTIDA])
        campeonato = (res[0][0][0] if res[0] else "").strip()
        partida    = (res[1][0][0] if res[1] else "").strip()
        return campeonato, partida

    # ═══════════════════════════════════════════════════════════════════════
    # CONTROLE — CONFIGURAÇÃO & FORMATAÇÃO PREMIUM
    # ═══════════════════════════════════════════════════════════════════════

    def configurar_controle(self, campeonatos: list[str]) -> None:
        aba = self._aba(ABA_CONTROLE)
        sid = aba.id
        aba.clear()

        # Layout alinhado às constantes de célula:
        # C3 = índice 2 | C4 = índice 3 | C6 = índice 5 | C7 = índice 6 | C8 = índice 7
        dados = [
            ["", "", "", ""],                                               # 0  (linha 1)
            ["", "⚽  PAINEL DE CONTROLE  —  APWin Analytics", "", ""],        # 1  (linha 2)  B2:D2
            ["", "🏆  Campeonato",  campeonatos[0] if campeonatos else "", ""], # 2  (linha 3)  C3
            ["", "⚔️  Partida",     "", ""],                                   # 3  (linha 4)  C4
            ["", "", "", ""],                                               # 4  (linha 5)  separador
            ["", "📊  Status",       "Aguardando execução...", ""],             # 5  (linha 6)  C6
            ["", "🕐  Última coleta", "—", ""],                                # 6  (linha 7)  C7
            ["", "📈  Última stats",  "—", ""],                                # 7  (linha 8)  C8
            ["", "🗄️  Cache",         "—", ""],                                # 8  (linha 9)  C9
            ["", "", "", ""],                                               # 9  (linha 10)
            ["", "", "", ""],                                               # 10 (linha 11)
            ["", "ℹ️  COMO UTILIZAR", "", ""],                                 # 11 (linha 12) B12:D12
            ["", "1.", "Selecione o campeonato no dropdown acima (C3)", ""],    # 12 (linha 13)
            ["", "2.", "Aguarde a coleta automática das partidas", ""],         # 13 (linha 14)
            ["", "3.", "Escolha a partida no segundo dropdown (C4)", ""],       # 14 (linha 15)
            ["", "4.", "O dashboard atualiza automaticamente em segundos", ""], # 15 (linha 16)
            ["", "", "", ""],                                               # 16 (linha 17)
            ["", "💡 Dica", "Partidas já consultadas ficam em cache local por 1 hora", ""],  # 17 (linha 18)
        ]
        aba.update("A1", dados, value_input_option="USER_ENTERED")

        # Validação do campeonato em C3  →  rowIndex 2, colIndex 2
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

        # Larguras de coluna
        reqs += [
            _col_w(sid, 0, 20),   # A: margem estreita
            _col_w(sid, 1, 40),   # B: ícones/números
            _col_w(sid, 2, 320),  # C: conteúdo principal
            _col_w(sid, 3, 20),   # D: margem estreita
            _col_w(sid, 4, 1),    # E: lista partidas (invisível inicial)
        ]

        # ── Banner Principal (B2:D2) ──
        # rowIndex 1, colIndex 1  →  rowIndex 2, colIndex 4  (B=1, C=2, D=3, E=4 exclusivo)
        reqs += [
            _merge(sid, 1, 1, 2, 4),
            _fmt(sid, 1, 1, 2, 4,
                 backgroundColor=C_AZUL_900,
                 textFormat=_tf(bold=True, size=16, color=C_BRANCO, font_family="Google Sans Display"),
                 **_halign("CENTER"), **_valign("MIDDLE")),
            _row_h(sid, 1, 52),
            # Borda inferior azul no banner (substitui linha decorativa)
            _fmt(sid, 1, 1, 2, 4,
                 **_borders(color=C_AZUL_500, width=3)),
        ]

        # ── Card Campeonato (linha 3, índice 2) ──
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

        # ── Card Partida (linha 4, índice 3) ──
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

        # ── Separador (linha 5, índice 4) ──
        reqs += [_row_h(sid, 4, 10)]

        # ── Status & Metadados (linhas 6-9, índices 5-8) ──
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
        # Destaque visual na linha de cache
        reqs += [
            _fmt(sid, 8, 2, 9, 3,
                 backgroundColor=C_AZUL_50,
                 textFormat=_tf(bold=True, size=10, color=C_AZUL_700),
                 **_halign("LEFT"), **_valign("MIDDLE")),
        ]

        # ── Caixa de Instruções (linha 12, índice 11) ──
        reqs += [
            _merge(sid, 11, 1, 12, 4),
            _fmt(sid, 11, 1, 12, 4,
                 backgroundColor=C_CINZA_800,
                 textFormat=_tf(bold=True, size=11, color=C_BRANCO),
                 **_halign("LEFT"), **_valign("MIDDLE")),
            _row_h(sid, 11, 32),
        ]

        for r in range(12, 16):  # linhas 13-16, índices 12-15
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

        # ── Dica Final (linha 18, índice 17) ──
        reqs += [
            _merge(sid, 17, 1, 18, 4),
            _fmt(sid, 17, 1, 18, 4,
                 backgroundColor=C_AMARELO_BG,
                 textFormat=_tf(size=10, color=C_AMARELO, italic=True),
                 **_halign("CENTER"), **_valign("MIDDLE")),
            _row_h(sid, 17, 28),
        ]

        # ── Bordas sutis em cards ──
        reqs += [
            _fmt(sid, 2, 1, 4, 3, **_borders(color=C_CINZA_200, width=1)),    # card inputs
            _fmt(sid, 5, 1, 9, 3, **_borders(color=C_CINZA_200, width=1)),    # card status (agora 4 linhas)
            _fmt(sid, 11, 1, 16, 3, **_borders(color=C_CINZA_200, width=1)),  # card instruções
        ]

        reqs.append(_hide_col(sid, 4))
        self._batch(reqs)

    def atualizar_lista_partidas(self, partidas: list, campeonato: str,
                                coletado_em: str, n_cache: int = 0) -> None:
        aba = self._aba(ABA_CONTROLE)
        sid = aba.id
        aba.update(f"{COL_LISTA_PARTIDAS}1",
                   [[p] for p in partidas], value_input_option="USER_ENTERED")
        if len(partidas) < 200:
            aba.batch_clear([f"E{len(partidas)+1}:E200"])

        # Validação da partida em C4  →  rowIndex 3, colIndex 2
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

    def atualizar_status(self, mensagem, tipo="info"):
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

    def atualizar_progresso_prefetch(self, atual, total, campeonato):
        msg = f"⏳ Pré-carregando '{campeonato}': {atual}/{total} partidas..."
        self._aba(ABA_CONTROLE).update(CELL_STATUS, [[msg]])

    # ═══════════════════════════════════════════════════════════════════════
    # DASHBOARD — ESCRITA & FORMATAÇÃO PREMIUM
    # ═══════════════════════════════════════════════════════════════════════

    def escrever_dashboard(self, info, forma_m, stats_m, forma_v, stats_v, odds):
        # Recria a aba do zero para eliminar merges/formatações residuais
        self._remover_aba_se_existir(ABA_DASHBOARD)
        aba = self._aba(ABA_DASHBOARD)
        sid = aba.id

        linhas, m = self._montar_dashboard(
            info, forma_m, stats_m, forma_v, stats_v, odds)

        # Escreve dados
        aba.update("A1", linhas, value_input_option="USER_ENTERED")
        time.sleep(0.6)

        # Aplica formatação em aba limpa
        self._formatar_dashboard(sid, info["mandante"], info["visitante"], m)
        log.info(f"Dashboard: {len(linhas)} linhas escritas.")

    def _montar_dashboard(self, info, forma_m, stats_m, forma_v, stats_v, odds):
        mandante  = info["mandante"]
        visitante = info["visitante"]
        linhas: list = []
        m: dict = {}

        def row(*c):
            linhas.append(list(c))
            return len(linhas) - 1

        # ── Banner Principal ──
        row()  # respiro
        m["banner"]   = row("", f"⚽  {mandante}   ×   {visitante}")
        m["info_bar"] = row("", info["campeonato"], "",
                            info["data_hora"], "", info["estadio"])

        # ── KPI — destaques rápidos de cada time ──
        kpi_labels = [
            "", "Vence %", "Gols / jogo", "xG médio",
            "", "xG médio", "Gols / jogo", "Vence %",
        ]
        kpi_values = [
            "",
            get_stat(stats_m, "Vence %"), get_stat(stats_m, "Gols"), get_stat(stats_m, "xG"),
            "✦",
            get_stat(stats_v, "xG"), get_stat(stats_v, "Gols"), get_stat(stats_v, "Vence %"),
        ]
        m["kpi_labels"] = row(*kpi_labels)
        m["kpi_values"] = row(*kpi_values)
        row()  # respiro

        # ── Forma Recente ──
        m["sec_forma"] = row("", "📈  FORMA RECENTE")
        m["cab_forma"] = row("", "Equipe", "Âmbito",
                             "Ú1", "Ú2", "Ú3", "Ú4", "Ú5", "PPJ")
        m["forma_rows"] = []
        for tabela, nome in [(forma_m, mandante), (forma_v, visitante)]:
            for line in tabela:
                if not line or line[0] in ("Forma", ""):
                    continue
                letras = [l for l in (line[1] if len(line) > 1 else "").replace(" ", "")
                          if l.upper() in "VDE"]
                letras += [""] * (5 - len(letras))
                idx = row("", nome, line[0], *letras[:5],
                          line[2] if len(line) > 2 else "")
                m["forma_rows"].append(idx)
        row()  # respiro

        # ── Estatísticas Comparadas ──
        m["sec_stats"]      = row("", "📊  ESTATÍSTICAS COMPARADAS")
        m["cab_stats_time"] = row("", "", mandante, "", "",
                                        visitante, "", "")
        m["cab_stats_sub"]  = row("", "Métrica",
                                  "Geral", "Casa", "Fora",
                                  "Geral", "Casa", "Fora")
        mapa_m = {r[0]: r[1:] for r in stats_m if len(r) >= 2}
        mapa_v = {r[0]: r[1:] for r in stats_v if len(r) >= 2}
        m["stats_rows"] = []
        for i, r in enumerate(stats_m):
            if r[0] in ("Estatísticas", "") or len(r) < 2:
                continue
            vm = (mapa_m.get(r[0], []) + [""] * 3)[:3]
            vv = (mapa_v.get(r[0], []) + [""] * 3)[:3]
            idx = row("", r[0], *vm, *vv)
            m["stats_rows"].append((idx, i % 2 == 0))
        row()  # respiro

        # ── Odds ──
        if odds:
            m["sec_odds"] = row("", "🎯  ODDS  —  1 × X × 2")
            m["cab_odds"] = row("", "Casa de Apostas",
                                f"1 — {mandante}", "X — Empate",
                                f"2 — {visitante}")
            m["odds_rows"] = []
            for line in odds:
                if not line or line[0] == "Casa de Apostas":
                    continue
                if not line[0] and len(line) >= 4:
                    m["odds_rows"].append(row("", "—", line[1], line[2], line[3]))
            row()  # respiro

        row()  # respiro final
        return linhas, m

    def _formatar_dashboard(self, sid, mandante, visitante, m):
        reqs = []

        # Larguras de coluna otimizadas
        widths = [18, 200, 130, 85, 85, 70, 70, 70, 90, 90, 90]
        for i, w in enumerate(widths):
            reqs.append(_col_w(sid, i, w))

        # ── Banner Principal ──
        r = m.get("banner")
        if r is not None:
            reqs += [
                _merge(sid, r, 1, r+1, 8),
                _fmt(sid, r, 1, r+1, 8,
                     backgroundColor=C_PRETO,
                     textFormat=_tf(bold=True, size=20, color=C_BRANCO,
                                    font_family="Google Sans Display"),
                     **_halign("CENTER"), **_valign("MIDDLE")),
                _row_h(sid, r, 58),
            ]

        r = m.get("info_bar")
        if r is not None:
            reqs += [
                _merge(sid, r, 1, r+1, 8),
                _fmt(sid, r, 1, r+1, 8,
                     backgroundColor=C_CINZA_700,
                     textFormat=_tf(size=10, color=C_CINZA_300),
                     **_halign("CENTER"), **_valign("MIDDLE")),
                _row_h(sid, r, 28),
            ]

        # ── KPI Bar — visão rápida dos dois times ──
        r = m.get("kpi_labels")
        if r is not None:
            reqs += [
                # Metade esquerda (mandante): colunas 1-4
                _fmt(sid, r, 1, r+1, 5,
                     backgroundColor=C_AZUL_700,
                     textFormat=_tf(bold=True, size=9, color=C_AZUL_100),
                     **_halign("CENTER"), **_valign("MIDDLE")),
                # Metade direita (visitante): colunas 4-8
                _fmt(sid, r, 4, r+1, 8,
                     backgroundColor=C_ESMERALDA_700,
                     textFormat=_tf(bold=True, size=9, color=C_ESMERALDA_100),
                     **_halign("CENTER"), **_valign("MIDDLE")),
                _row_h(sid, r, 22),
            ]

        r = m.get("kpi_values")
        if r is not None:
            reqs += [
                # Metade esquerda (mandante)
                _fmt(sid, r, 1, r+1, 5,
                     backgroundColor=C_AZUL_100,
                     textFormat=_tf(bold=True, size=14, color=C_AZUL_900),
                     **_halign("CENTER"), **_valign("MIDDLE")),
                # Separador central
                _fmt(sid, r, 4, r+1, 5,
                     backgroundColor=C_CINZA_200,
                     textFormat=_tf(bold=True, size=12, color=C_CINZA_500),
                     **_halign("CENTER"), **_valign("MIDDLE")),
                # Metade direita (visitante)
                _fmt(sid, r, 5, r+1, 8,
                     backgroundColor=C_ESMERALDA_100,
                     textFormat=_tf(bold=True, size=14, color=C_ESMERALDA_900),
                     **_halign("CENTER"), **_valign("MIDDLE")),
                _row_h(sid, r, 36),
            ]
        # ── Forma Recente ──
        if "sec_forma" in m:
            ri = m["sec_forma"]
            reqs += [_merge(sid, ri, 1, ri+1, 9),
                     _fmt(sid, ri, 1, ri+1, 9, backgroundColor=C_CINZA_800,
                          textFormat=_tf(bold=True, size=11, color=C_BRANCO, font_family="Google Sans"),
                          **_halign("LEFT"), **_valign("MIDDLE")),
                     _row_h(sid, ri, 34),
                     _fmt(sid, ri+1, 1, ri+2, 9, backgroundColor=C_AZUL_500),
                     _row_h(sid, ri+1, 4)]
        r = m.get("cab_forma")
        if r is not None:
            reqs += [
                _fmt(sid, r, 1, r+1, 9,
                     backgroundColor=C_CINZA_100,
                     textFormat=_tf(bold=True, size=10, color=C_CINZA_600),
                     **_halign("CENTER"), **_valign("MIDDLE")),
                _row_h(sid, r, 30),
            ]

        for fr in m.get("forma_rows", []):
            reqs += [
                _fmt(sid, fr, 1, fr+1, 9,
                     backgroundColor=C_CINZA_50,
                     **_halign("CENTER"), **_valign("MIDDLE")),
                _fmt(sid, fr, 1, fr+1, 2,
                     textFormat=_tf(bold=True, size=10, color=C_CINZA_800)),
                _fmt(sid, fr, 2, fr+1, 3,
                     textFormat=_tf(italic=True, size=10, color=C_CINZA_500)),
                _row_h(sid, fr, 30),
            ]

        # Badges coloridos V/E/D na forma
        if m.get("forma_rows"):
            ri, rf = m["forma_rows"][0], m["forma_rows"][-1] + 1
            reqs += [_cond_eq(sid, ri, 3, rf, 8, "V", C_VERDE_BG, C_VERDE)]
            reqs += [_cond_eq(sid, ri, 3, rf, 8, "E", C_AMARELO_BG, C_AMARELO)]
            reqs += [_cond_eq(sid, ri, 3, rf, 8, "D", C_VERMELHO_BG, C_VERMELHO)]

        # ── Estatísticas ──
        if "sec_stats" in m:
            ri = m["sec_stats"]
            reqs += [_merge(sid, ri, 1, ri+1, 8),
                     _fmt(sid, ri, 1, ri+1, 8, backgroundColor=C_CINZA_800,
                          textFormat=_tf(bold=True, size=11, color=C_BRANCO, font_family="Google Sans"),
                          **_halign("LEFT"), **_valign("MIDDLE")),
                     _row_h(sid, ri, 34),
                     _fmt(sid, ri+1, 1, ri+2, 8, backgroundColor=C_AZUL_500),
                     _row_h(sid, ri+1, 4)]
        r = m.get("cab_stats_time")
        if r is not None:
            reqs += [
                _fmt(sid, r, 1, r+1, 2,
                     backgroundColor=C_CINZA_100,
                     textFormat=_tf(bold=True, size=10)),
                _merge(sid, r, 2, r+1, 5),
                _fmt(sid, r, 2, r+1, 5,
                     backgroundColor=C_AZUL_600,
                     textFormat=_tf(bold=True, size=12, color=C_BRANCO),
                     **_halign("CENTER"), **_valign("MIDDLE")),
                _merge(sid, r, 5, r+1, 8),
                _fmt(sid, r, 5, r+1, 8,
                     backgroundColor=C_ESMERALDA_600,
                     textFormat=_tf(bold=True, size=12, color=C_BRANCO),
                     **_halign("CENTER"), **_valign("MIDDLE")),
                _row_h(sid, r, 32),
            ]

        r = m.get("cab_stats_sub")
        if r is not None:
            reqs += [
                _fmt(sid, r, 1, r+1, 2,
                     backgroundColor=C_CINZA_200,
                     textFormat=_tf(bold=True, size=10, color=C_CINZA_700),
                     **_halign("CENTER")),
                _fmt(sid, r, 2, r+1, 5,
                     backgroundColor=C_AZUL_100,
                     textFormat=_tf(bold=True, size=10, color=C_AZUL_700),
                     **_halign("CENTER")),
                _fmt(sid, r, 5, r+1, 8,
                     backgroundColor=C_ESMERALDA_100,
                     textFormat=_tf(bold=True, size=10, color=C_ESMERALDA_900),
                     **_halign("CENTER")),
                _row_h(sid, r, 26),
            ]

        for idx, par in m.get("stats_rows", []):
            bg_m = C_AZUL_50 if par else C_BRANCO
            bg_v = C_ESMERALDA_50 if par else C_BRANCO
            reqs += [
                _fmt(sid, idx, 1, idx+1, 2,
                     backgroundColor=C_CINZA_50,
                     textFormat=_tf(bold=True, size=10, color=C_CINZA_700),
                     **_halign("LEFT"), **_valign("MIDDLE")),
                _fmt(sid, idx, 2, idx+1, 5,
                     backgroundColor=bg_m,
                     textFormat=_tf(size=10, color=C_CINZA_800),
                     **_halign("CENTER"), **_valign("MIDDLE")),
                _fmt(sid, idx, 5, idx+1, 8,
                     backgroundColor=bg_v,
                     textFormat=_tf(size=10, color=C_CINZA_800),
                     **_halign("CENTER"), **_valign("MIDDLE")),
                _row_h(sid, idx, 28),
            ]

        # ── Odds ──
        if "sec_odds" in m:
            ri = m["sec_odds"]
            reqs += [_merge(sid, ri, 1, ri+1, 5),
                     _fmt(sid, ri, 1, ri+1, 5, backgroundColor=C_CINZA_800,
                          textFormat=_tf(bold=True, size=11, color=C_BRANCO, font_family="Google Sans"),
                          **_halign("LEFT"), **_valign("MIDDLE")),
                     _row_h(sid, ri, 34),
                     _fmt(sid, ri+1, 1, ri+2, 5, backgroundColor=C_AZUL_500),
                     _row_h(sid, ri+1, 4)]
        r = m.get("cab_odds")
        if r is not None:
            reqs += [
                _fmt(sid, r, 1, r+1, 2,
                     backgroundColor=C_CINZA_200,
                     textFormat=_tf(bold=True, size=10, color=C_CINZA_700),
                     **_halign("CENTER"), **_valign("MIDDLE")),
                _fmt(sid, r, 2, r+1, 3,
                     backgroundColor=C_AZUL_100,
                     textFormat=_tf(bold=True, size=10, color=C_AZUL_700),
                     **_halign("CENTER"), **_valign("MIDDLE")),
                _fmt(sid, r, 3, r+1, 4,
                     backgroundColor=C_CINZA_200,
                     textFormat=_tf(bold=True, size=10, color=C_CINZA_700),
                     **_halign("CENTER"), **_valign("MIDDLE")),
                _fmt(sid, r, 4, r+1, 5,
                     backgroundColor=C_ESMERALDA_100,
                     textFormat=_tf(bold=True, size=10, color=C_ESMERALDA_900),
                     **_halign("CENTER"), **_valign("MIDDLE")),
                _row_h(sid, r, 32),
            ]

        for i, or_ in enumerate(m.get("odds_rows", [])):
            bg = C_LARANJA_BG if i % 2 == 0 else C_BRANCO
            reqs += [
                _fmt(sid, or_, 1, or_+1, 5,
                     backgroundColor=bg,
                     textFormat=_tf(size=10, color=C_CINZA_700),
                     **_halign("CENTER"), **_valign("MIDDLE")),
                _fmt(sid, or_, 1, or_+1, 2,
                     textFormat=_tf(size=10, italic=True, color=C_CINZA_400)),
                _row_h(sid, or_, 30),
            ]

        # Congelar primeiras 3 linhas (banner + info_bar + kpi_labels)
        reqs.append(_freeze(sid, rows=4))
        self._batch(reqs)

    # ═══════════════════════════════════════════════════════════════════════
    # HISTÓRICO — ESCRITA & FORMATAÇÃO PREMIUM
    # ═══════════════════════════════════════════════════════════════════════

    def escrever_historico(self, info, stats_m, stats_v, odds):
        from src.scraper_stats import get_stat
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

    # ═══════════════════════════════════════════════════════════════════════
    # LIMPEZA — REMOÇÃO DE ABA LEGADA
    # ═══════════════════════════════════════════════════════════════════════

    def remover_dados_brutos(self):
        """Remove a aba legada 'dados_brutos' se ainda existir na planilha."""
        self._remover_aba_se_existir("dados_brutos")