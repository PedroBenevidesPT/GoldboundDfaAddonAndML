# Avaliação dos estimadores de preço de referência

Gerado por `python scripts/reference_backtest.py`. É uma avaliação de **nível** e uma **calibração** com as vendas da conta — não um backtest de ouro realizado, porque a base de dados não tem vendas datadas.

## 1. Walk-forward: prever o mercado local do fetch seguinte

Os fetches por reino são reproduzidos por ordem (o log de alterações é reconstruído fetch a fetch) e cada estimador é avaliado contra o mercado local do fetch seguinte.

### local disperso (97,600 observações)

| estimador | p25 | p50 | p75 | média |
|---|---:|---:|---:|---:|
| aritmética 0.5/0.5 | 0.238 | 0.646 | 2.200 | 3.883 |
| aritmética 0.8/0.2 | 0.114 | 0.331 | 0.954 | 2.423 |
| conservadora: min | 0.028 | 0.072 | 0.277 | 0.272 |
| geométrica 0.5/0.5 | 0.242 | 0.567 | 1.368 | 1.283 |
| geométrica 0.65/0.35 | 0.178 | 0.411 | 0.879 | 0.940 |
| geométrica 0.8/0.2 | 0.113 | 0.259 | 0.514 | 0.862 |
| local (primeiro) | 0.022 | 0.053 | 0.152 | 1.495 |
| piso: max | 0.249 | 1.222 | 4.315 | 7.577 |
| regional | 0.449 | 1.175 | 4.154 | 6.347 |

### parede viva (279,121 observações)

| estimador | p25 | p50 | p75 | média |
|---|---:|---:|---:|---:|
| aritmética 0.5/0.5 | 0.128 | 0.450 | 2.252 | 60.832 |
| aritmética 0.8/0.2 | 0.054 | 0.189 | 0.912 | 24.373 |
| conservadora: min | 0.001 | 0.013 | 0.117 | 0.135 |
| geométrica 0.5/0.5 | 0.130 | 0.486 | 1.355 | 1.098 |
| geométrica 0.65/0.35 | 0.092 | 0.339 | 0.832 | 0.615 |
| geométrica 0.8/0.2 | 0.055 | 0.195 | 0.439 | 0.317 |
| local (primeiro) | 0.000 | 0.004 | 0.018 | 0.077 |
| piso: max | 0.016 | 0.713 | 4.492 | 121.542 |
| regional | 0.254 | 0.885 | 4.475 | 121.598 |

### sem parede (77,862 observações)

| estimador | p25 | p50 | p75 | média |
|---|---:|---:|---:|---:|
| aritmética 0.5/0.5 | 0.085 | 0.308 | 0.595 | 12275.563 |
| aritmética 0.8/0.2 | 0.036 | 0.133 | 0.299 | 4910.953 |
| conservadora: min | 0.000 | 0.089 | 0.516 | 1.222 |
| geométrica 0.5/0.5 | 0.086 | 0.338 | 0.728 | 6.241 |
| geométrica 0.65/0.35 | 0.061 | 0.247 | 0.554 | 2.661 |
| geométrica 0.8/0.2 | 0.037 | 0.152 | 0.345 | 1.758 |
| local (primeiro) | 0.000 | 0.001 | 0.024 | 1.229 |
| piso: max | 0.000 | 0.047 | 1.112 | 24549.935 |
| regional | 0.167 | 0.599 | 1.082 | 24330.044 |

**Leitura:** o mercado local é, em todos os subconjuntos, uma previsão muito melhor do que a referência regional (erro mediano na ordem de 0,5 % contra >100 %). A referência regional é uma média de 92 reinos e por isso erra muito mais. Onde é preciso misturar, a média **geométrica** bate a aritmética com os mesmos pesos.

## 2. Preços realizados da conta (calibração, não backtest)

n = 25 vendas de transmog com preço local e regional. As vendas **não são datadas**, logo a comparação é contra a referência de hoje, e não contra a do momento da venda: o número é uma indicação, não uma prova.

| estimador | erro mediano | p25 | p75 |
|---|---:|---:|---:|
| aritmética 0.5/0.5 | 4.62 | 1.36 | 11.87 |
| aritmética 0.8/0.2 | 3.89 | 0.96 | 11.64 |
| conservadora: min | 1.05 | 0.32 | 4.24 |
| geométrica 0.5/0.5 | 3.93 | 1.15 | 8.13 |
| geométrica 0.65/0.35 | 3.53 | 0.68 | 9.14 |
| geométrica 0.8/0.2 | 3.16 | 0.51 | 11.15 |
| local (primeiro) | 3.18 | 0.32 | 9.86 |
| piso: max | 5.85 | 2.44 | 19.53 |
| regional | 4.19 | 1.66 | 18.04 |

O erro é grande para todos: as vendas da conta realizaram-se a uma fracção do ask médio, e houve meses entre a venda e a referência de hoje. A ordem relativa é que se pode ler; o nível é trabalho do coeficiente de banda, não da referência.

## 3. O que fica por provar

- **Não há backtest de ouro realizado.** Para o haver seria preciso o journal com datas (a data da venda e a da publicação) e a parede local nessa data. A máquina existe (`reference_price.estimate_e2`), o dado não.
- **A comparação com as vendas é fraca** (n = 25, sem datas).
- **O alvo do walk-forward é o nível local**, não o preço de venda: o referente e o alvo partilham o mesmo mecanismo de média, o que favorece a referência local. O teste confirma a *estabilidade* e a *relevância* da referência local, não que ela seja o valor justo.
- **Os pesos são uma política**, informed by the measurements: a profundidade, a frescura e a dispersão são medidas; a forma da curva é decisão e está em `reference_price.ReferenceConfig`.
