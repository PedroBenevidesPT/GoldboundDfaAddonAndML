# Calibração das fontes de preço

O peso de cada família de fontes **não** é escolhido: é medido contra as vendas reais da conta.

- Amostra: 177 itens com venda, 419 observações por família.
- Medido em: 2026-10-19T14:28:23Z.
- Fonte: journal_stats (vendas da conta) vs fontes TSM de hoje.

> **Limitação declarada:** as vendas não têm data, logo a comparação é contemporânea (os preditores são os de hoje e as vendas são as do período). É uma limitação do dado, não do método.

| família | observações | cobertura (itens) | erro mediano (log) | razão mediana (previsão/venda) | peso |
|---|---:|---:|---:|---:|---:|
| local_ask | 29 | 16% | 1.350 | 3.858 | 0.425 |
| regional_ask | 195 | 100% | 0.286 | 1.247 | 0.777 |
| regional_sales | 195 | 100% | 0.280 | 0.908 | 0.781 |

## Como ler

- O **erro mediano** é a mediana de |log(previsão / venda)|: quanto mais perto de zero, melhor a família prevê o preço realizado.
- A **razão mediana** mostra o viés: um valor de 3 significa que a f systematicallyamente está 3× acima do preço realizado (é o caso das referências de ask, quando o mercado é inflado).
- O **peso** é 1/(1+erro) escalado pela cobertura: uma família que nunca esteve disponível não recebe peso por ser teoricamente a melhor. A escala é a mesma dos priors do motor (1,0 = o melhor possível), para as duas poderem ser misturadas.
