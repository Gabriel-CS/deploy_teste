# app.py (Modificado)
import streamlit as st
import time
from datetime import datetime
from src.sheets_manager import SheetsManager
from main import ciclo, carregar_campeonatos # Importamos a lógica diretamente

st.set_page_config(page_title="DATA Monitor", page_icon="⚽", layout="centered")

# --- 1. Inicialização de Recursos Persistentes ---
# O @st.cache_resource garante que conectamos ao Google Sheets apenas 1 vez,
# não a cada refresh da página.
@st.cache_resource
def get_sheets_manager():
    sm = SheetsManager()
    sm.conectar()
    return sm

@st.cache_resource
def get_campeonatos():
    return carregar_campeonatos()

sm = get_sheets_manager()
campeonatos = get_campeonatos()

st.title("⚽ DATA Analytics")
st.caption("Rodando nativamente no Streamlit (sem subprocessos)")

# --- 2. Interface (UI) ---
col_r, col_s = st.columns([1, 3])
with col_r:
    if st.button("🔄 Forçar Execução"):
        st.rerun()

with col_s:
    intervalo = st.slider("Auto-refresh (s)", 10, 90, 60)

# --- 3. Execução da Lógica Core ---
st.info("Buscando atualizações na planilha...")

try:
    # Executamos o ciclo diretamente aqui! 
    # O prefetch pode ser passado como False para não travar a UI
    ciclo(sm, campeonatos, habilitar_prefetch=False)
except Exception as e:
    st.error(f"Erro na execução: {e}")

# Lemos o status para mostrar na tela
from src import status_manager
status = status_manager.carregar()
st.success(f"Status atual: {status.get('mensagem_status', 'OK')}")

st.caption(f"Próxima verificação em {intervalo} segundos...")

# --- 4. Loop ---
time.sleep(intervalo)
st.rerun()