import requests
from bs4 import BeautifulSoup
import json
import time
import logging
from typing import Dict

# Configuração de logging para acompanhar a execução
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class ApwinScraper:
    """Scraper para extrair campeonatos de futebol do site APWIN."""
    def __init__(self, delay: float = 1.0):
        self.base_url = "https://www.apwin.com/br/ligas/"
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7',
        })

    def fetch_page(self, url: str) -> str:
        """
        Realiza a requisição HTTP e retorna o conteúdo HTML da página.
        """
        logging.info(f"Aguardando {self.delay} segundos antes da requisição...")
        time.sleep(self.delay)

        logging.info(f"Requisitando: {url}")
        response = self.session.get(url, timeout=30)
        response.raise_for_status()  # Levanta exceção para status HTTP 4xx/5xx
        response.encoding = 'utf-8'
        return response.text

    def extract_championships(self, html: str) -> Dict[str, str]:
        """
        Extrai os campeonatos (nome e link) a partir do HTML da página de ligas.
        """
        soup = BeautifulSoup(html, 'html.parser')
        championships = {}

        # Localiza todas as divs com a classe "apw-accordion-content"
        accordion_divs = soup.find_all('div', class_='apw-accordion-content')

        if not accordion_divs:
            logging.warning("Nenhuma div 'apw-accordion-content' encontrada. Verifique a estrutura da página.")

        for div in accordion_divs:
            # Dentro de cada div, busca todos os links <a>
            links = div.find_all('a', href=True)
            for link in links:
                title = link.get('title')
                href = link['href']

                # Garante que o link seja absoluto
                if href.startswith('/'):
                    href = f"https://www.apwin.com{href}"

                if title and href:
                    championships[title.strip()] = href

        logging.info(f"Total de campeonatos extraídos: {len(championships)}")
        return championships

    def save_to_json(self, data: Dict[str, str], filename: str = "campeonatos.json"):
        """
        Salva o dicionário de campeonatos em um arquivo JSON.
        """
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        logging.info(f"Dados salvos em '{filename}'")

    def run(self, output_file: str = "campeonatos.json"):
        """
        Executa o fluxo completo: requisição, extração e salvamento.
        """
        try:
            html = self.fetch_page(self.base_url)
            championships = self.extract_championships(html)
            if championships:
                self.save_to_json(championships, output_file)
            else:
                logging.warning("Nenhum campeonato foi encontrado. O JSON não será criado.")
        except requests.RequestException as e:
            logging.error(f"Erro durante a requisição: {e}")
        except Exception as e:
            logging.error(f"Erro inesperado: {e}")


if __name__ == "__main__":
    # Instancia o scraper com um intervalo de 2 segundos (ajuste conforme orientação do dono do site)
    scraper = ApwinScraper(delay=2.0)
    scraper.run("campeonatos.json")