"""
app.py — Painel de Monitoramento Streamlit
==========================================
Inicia o daemon main.py automaticamente e exibe status mínimo de execução.
Nenhum dado sensível da planilha é exibido.

Uso:
    streamlit run app.py
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

# ── Configuração da página ──────────────────────────────────────────────────
st.set_page_config(
    page_title="DATA Monitor",
    page_icon="⚽",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
    .block-container { padding-top: 2rem; padding-bottom: 2rem; }
    .status-card {
        border-radius: 12px;
        padding: 20px 24px;
        text-align: center;
        margin-bottom: 20px;
    }
    .status-online  { background: linear-gradient(135deg,#dcfce7,#bbf7d0); border:1px solid #22c55e; }
    .status-paused  { background: linear-gradient(135deg,#fef9c3,#fde047); border:1px solid #eab308; }
    .status-offline { background: linear-gradient(135deg,#fee2e2,#fecaca); border:1px solid #ef4444; }
    .metric-box {
        background: #f8fafc;
        border-radius: 8px;
        padding: 14px 16px;
        border: 1px solid #e2e8f0;
        margin-bottom: 4px;
    }
    .metric-label { font-size: 0.72rem; color: #64748b; margin-bottom: 2px; }
    .metric-value { font-size: 1rem; font-weight: 600; color: #1e293b; }
</style>
""", unsafe_allow_html=True)


# ── Caminhos ────────────────────────────────────────────────────────────────
STATUS_FILE = Path("data/status.json")
LOG_DIR     = Path("logs")


# ── Helpers ─────────────────────────────────────────────────────────────────

def ler_status() -> dict:
    if not STATUS_FILE.exists():
        return {
            "estado": "parado",
            "campeonato": "—",
            "partida": "—",
            "ultima_atividade": "",
            "mensagem_status": "Aguardando início do daemon...",
            "total_partidas_cache": 0,
            "pid": None,
        }
    try:
        with STATUS_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"estado": "erro", "mensagem_status": "Arquivo de status corrompido.", "pid": None}


def pid_ativo(pid) -> bool:
    """Verifica se um processo com o PID informado ainda está rodando."""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)   # sinal 0 = apenas testa existência
        return True
    except (OSError, ProcessLookupError, ValueError):
        return False


def iniciar_daemon() -> None:
    """
    Lança main.py como subprocesso se o daemon não estiver ativo.
    Prefetch desativado por padrão — coleta só mediante seleção de campeonato.
    """
    status = ler_status()
    if status.get("estado") == "rodando" and pid_ativo(status.get("pid")):
        return  # já está rodando, nada a fazer

    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / "daemon_stdout.log"
    log_file = open(log_path, "a", encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, "main.py"],   # prefetch off é o padrão agora
        stdout=log_file,
        stderr=subprocess.STDOUT,
        # Garante que o subprocesso sobreviva se o Streamlit reiniciar
        start_new_session=True,
    )
    # Persiste o PID na session_state para não perder a referência
    st.session_state["daemon_pid"] = proc.pid


def tempo_desde(iso_str: str) -> str:
    if not iso_str:
        return "—"
    try:
        delta = datetime.now() - datetime.fromisoformat(iso_str)
        if delta.days > 0:
            return f"{delta.days}d atrás"
        if delta.seconds > 3600:
            return f"{delta.seconds // 3600}h atrás"
        if delta.seconds > 60:
            return f"{delta.seconds // 60}min atrás"
        return "agora"
    except Exception:
        return "—"


# ── Auto-start (executado uma vez por sessão Streamlit) ─────────────────────
if "daemon_iniciado" not in st.session_state:
    st.session_state["daemon_iniciado"] = True
    iniciar_daemon()


# ── Leitura de status ───────────────────────────────────────────────────────
status = ler_status()
estado = status.get("estado", "parado")


# ── Cabeçalho ───────────────────────────────────────────────────────────────
st.title("⚽  DATA Analytics")
st.caption("Monitor do daemon de sincronização — nenhum dado sensível é exibido aqui.")


# ── Card de estado ──────────────────────────────────────────────────────────
if estado == "rodando":
    st.markdown(
        '<div class="status-card status-online">'
        '<h2 style="color:#15803d;margin:0">🟢  ONLINE</h2>'
        '<p style="color:#166534;margin:4px 0 0">Daemon ativo — aguardando seleção na planilha</p>'
        '</div>', unsafe_allow_html=True,
    )
elif estado == "pausado":
    st.markdown(
        '<div class="status-card status-paused">'
        '<h2 style="color:#854d0e;margin:0">🟡  PAUSADO</h2>'
        '<p style="color:#713f12;margin:4px 0 0">Execução suspensa</p>'
        '</div>', unsafe_allow_html=True,
    )
else:
    st.markdown(
        '<div class="status-card status-offline">'
        '<h2 style="color:#991b1b;margin:0">🔴  INICIANDO…</h2>'
        '<p style="color:#7f1d1d;margin:4px 0 0">O daemon será iniciado em instantes</p>'
        '</div>', unsafe_allow_html=True,
    )

# ── Métricas mínimas ────────────────────────────────────────────────────────
#c1, c2, c3 = st.columns(3)
#
#def metric_box(col, label, value):
#    col.markdown(
#        f'<div class="metric-box">'
#        f'<div class="metric-label">{label}</div>'
#        f'<div class="metric-value">{value or "—"}</div>'
#        f'</div>',
#        unsafe_allow_html=True,
#    )
#
#metric_box(c1, "🏆 Campeonato",  status.get("campeonato") or "—")
#metric_box(c2, "⚔️ Partida",     status.get("partida")    or "—")
#metric_box(c3, "🕐 Última ativ.", tempo_desde(status.get("ultima_atividade", "")))

# ── Último log ──────────────────────────────────────────────────────────────
#msg = status.get("mensagem_status", "")
#if msg:
#    st.info(f"**Status:** {msg}")
#
st.divider()

# ── Controle mínimo ─────────────────────────────────────────────────────────
col_r, col_s = st.columns([1, 3])

with col_r:
    if st.button("🔄 Atualizar", use_container_width=True):
        st.rerun()

with col_s:
    intervalo = st.slider("Auto-refresh (s)", 10, 90, 60, label_visibility="collapsed")

st.caption(f"Próxima atualização em {intervalo}s · PID daemon: {status.get('pid') or 'N/A'}")

# ── Auto-refresh ────────────────────────────────────────────────────────────
time.sleep(intervalo)
st.rerun()