# farm_ranking_report.py
import sys
sys.path.insert(0, r'C:\FarmAnalysis')
from woweconomy.analytics import farm_ranking
from woweconomy.analytics.analytics import load_latest_market

try:
    market = load_latest_market()
    result = farm_ranking.top_mats(drops=[], market=market, top_n=10)
    with open('fazenda_report.txt', 'w', encoding='utf-8') as f:
        f.write("=== Farm Ranking Report ===\n")
        for i, item in enumerate(result, 1):
            f.write(f"{i}. {item}\n")
    print(f"Report gerado: fazenda_report.txt ({len(result)} itens)")
except Exception as e:
    with open('fazenda_report.txt', 'w', encoding='utf-8') as f:
        f.write(f"Error: {e}")
    print(f"Erro: {e}")