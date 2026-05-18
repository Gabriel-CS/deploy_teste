"""
src/throttler.py
================
Controle centralizado e thread-safe de taxa de requisições ao APWin.

Todas as funções que fazem requests HTTP devem chamar `throttler.aguardar()`
antes de executar a requisição. O throttler garante que nunca haverá duas
requisições separadas por menos de MIN_DELAY segundos, independente de
quantas threads estejam rodando.

Uso:
    from src.throttler import throttler

    throttler.aguardar()          # bloqueia se necessário
    resp = session.get(url)
"""

import logging
import random
import threading
import time

log = logging.getLogger(__name__)


class Throttler:
    """
    Garante um intervalo mínimo entre requisições HTTP, com jitter aleatório
    para evitar padrões previsíveis de acesso.

    Args:
        min_delay:  mínimo de segundos entre requisições (padrão: 20)
        max_delay:  máximo de segundos entre requisições (padrão: 30)
                    O delay real de cada ciclo é sorteado nesse intervalo.
    """

    def __init__(self, min_delay: float = 20.0, max_delay: float = 30.0):
        self._min   = min_delay
        self._max   = max_delay
        self._lock  = threading.Lock()     # só uma thread acessa por vez
        self._ultimo: float = 0.0          # timestamp da última requisição

    # -----------------------------------------------------------------------
    # API pública
    # -----------------------------------------------------------------------

    def aguardar(self, motivo: str = "") -> None:
        """
        Bloqueia a thread chamante até que o intervalo mínimo desde a última
        requisição tenha sido cumprido. Thread-safe: se duas threads chamarem
        ao mesmo tempo, elas se enfileiram.
        """
        with self._lock:
            agora    = time.monotonic()
            decorrido = agora - self._ultimo
            delay    = random.uniform(self._min, self._max)

            espera = delay - decorrido
            if espera > 0:
                label = f" [{motivo}]" if motivo else ""
                log.info(
                    f"Throttler{label}: aguardando {espera:.1f}s "
                    f"(intervalo alvo: {delay:.1f}s)"
                )
                self._exibir_contagem(espera)

            self._ultimo = time.monotonic()

    def resetar(self) -> None:
        """Zera o timestamp — próxima requisição não precisa esperar."""
        with self._lock:
            self._ultimo = 0.0

    def tempo_ate_proxima(self) -> float:
        """Retorna quantos segundos faltam para a próxima requisição liberada."""
        agora     = time.monotonic()
        decorrido = agora - self._ultimo
        return max(0.0, self._min - decorrido)

    # -----------------------------------------------------------------------
    # Interno
    # -----------------------------------------------------------------------

    def _exibir_contagem(self, segundos: float) -> None:
        """Dorme em pequenos intervalos para não travar o processo."""
        fim = time.monotonic() + segundos
        while True:
            restante = fim - time.monotonic()
            if restante <= 0:
                break
            time.sleep(min(1.0, restante))


# ---------------------------------------------------------------------------
# Instância global — importar e usar em todos os módulos
# ---------------------------------------------------------------------------

throttler = Throttler(min_delay=20.0, max_delay=30.0)