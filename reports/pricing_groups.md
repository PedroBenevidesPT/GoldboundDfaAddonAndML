# Grupos de preço do inventário de transmog

Gerado por `python scripts/pricing_groups.py`. É uma **análise**: nada foi escrito na base de dados nem no TSM, e a string de import proposta nunca foi importada.

## 1. O que foi medido, e onde

| dado | fonte | janela | cobertura |
|---|---|---|---|
| clearance E1 (venda regional ÷ parede local) | `tsm_region_price.avg_sale_price` ÷ `tsm_realm_price.min_buyout` | estado actual | todos os pares (reino, item) com parede |
| clearance E2 (venda própria ÷ parede **da data da venda**) | `journal_stats` ÷ histórico de paredes | — | **indisponível**: o journal não guarda datas |
| comportamento do ask em 78 dias | `market_snapshots` (27/07 → 13/10) | 78 dias | 30,892 itens |
| procura potencial | `dfa.db :: missing` | amostra DfA | 20,485 contas amostradas |
| concorrência | `tsm_realm_price.min_buyout`, `tsm_market.num_auctions` | actual | 92 reinos |
| vendas e expirações próprias | `journal_stats` (Journalator) | histórico | só os itens da conta |

**E1 é um limite inferior**, não o preço justo: a média de venda regional inclui snipes (vendas a 1 % do ask) e a parede é o *anúncio* mais barato, não o preço transacionado. **E2 seria a medida honesta**, mas este banco de dados não tem vendas datadas, logo a parede na data da venda não pode ser reconstruída — e a parede actual não é usada em seu lugar.

**E2 foi tentado:** 195 venda(s) sem data no journal: a parede na data da venda não pode ser reconstruída.

## 2. Bandas de preço: a medição

As nove bandas são **candidatas**. As que não têm amostra suficiente são fundidas na seguinte e assinaladas.

| banda | itens | E1 n | E1 p25 | E1 p50 | E1 p75 | ask 78d | parede/ask | sold/dia | procura | confiança |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| <100 | 7,309 | 15,760 | 0.045 | 0.182 | 0.624 | 0.702 | 0.930 | 18.5745 | 0.126 | alta |
| 100-500 | 4,509 | 136,539 | 0.067 | 0.202 | 0.574 | 0.889 | 1.000 | 0.0260 | 0.263 | alta |
| 500-2k | 5,316 | 165,996 | 0.039 | 0.119 | 0.431 | 0.903 | 0.988 | 0.0130 | 0.329 | alta |
| 2k-5k | 3,063 | 101,995 | 0.033 | 0.104 | 0.399 | 0.945 | 0.983 | 0.0110 | 0.411 | alta |
| 5k-15k | 3,195 | 134,741 | 0.031 | 0.106 | 0.375 | 0.959 | 0.980 | 0.0080 | 0.457 | alta |
| 15k-50k | 4,053 | 130,711 | 0.024 | 0.080 | 0.285 | 0.995 | 0.999 | 0.0060 | 0.539 | alta |
| 50k-150k | 1,987 | 63,432 | 0.018 | 0.063 | 0.212 | 1.000 | 1.000 | 0.0060 | 0.658 | alta |
| 150k-500k | 1,059 | 19,211 | 0.017 | 0.056 | 0.238 | 1.000 | 1.000 | 0.0060 | 0.672 | alta |
| >500k | 664 | 4,964 | 0.001 | 0.020 | 0.233 | 1.052 | 1.003 | 0.0070 | 0.680 | alta |

## 3. Bandas de preço: a política (separada da medição)

Os coeficientes **normais** não são lidos das razoes de venda. O que opera hoje é o modelo do projecto (secção D de `reports/mog_groups.md`); a medição fica ao lado, com a sua incerteza, para se ver onde as duas coisas discordam. As bandas com mais de 1 000 observações de E1 são as únicas em que a evidência manda; nas outras, a proposta é fraca.

| banda | normal (opera) | min (opera) | max (opera) | fonte | proposta da evidência (normal/min/max) | dispersão | confiança |
|---|---:|---:|---:|---|---:|---|---|
| <100 | 0.45 | 0.25 | 0.90 | modelo actual (mog_operations.BAND_MULTIPLIERS) | 0.182 / 0.080 / 0.624 | p25 0.045 / p50 0.182 / p75 0.624 (n=15760) | alta |
| 100-500 | 0.55 | 0.30 | 1.00 | modelo actual (mog_operations.BAND_MULTIPLIERS) | 0.202 / 0.080 / 0.574 | p25 0.067 / p50 0.202 / p75 0.574 (n=136539) | alta |
| 500-2k | 0.62 | 0.35 | 1.15 | modelo actual (mog_operations.BAND_MULTIPLIERS) | 0.150 / 0.080 / 0.431 | p25 0.039 / p50 0.119 / p75 0.431 (n=165996) | alta |
| 2k-5k | 0.68 | 0.40 | 1.30 | modelo actual (mog_operations.BAND_MULTIPLIERS) | 0.150 / 0.080 / 0.399 | p25 0.033 / p50 0.104 / p75 0.399 (n=101995) | alta |
| 5k-15k | 0.78 | 0.45 | 1.45 | modelo actual (mog_operations.BAND_MULTIPLIERS) | 0.150 / 0.080 / 0.375 | p25 0.031 / p50 0.106 / p75 0.375 (n=134741) | alta |
| 15k-50k | 0.85 | 0.50 | 1.60 | modelo actual (mog_operations.BAND_MULTIPLIERS) | 0.150 / 0.080 / 0.285 | p25 0.024 / p50 0.080 / p75 0.285 (n=130711) | alta |
| 50k-150k | 0.85 | 0.50 | 1.60 | modelo actual (mog_operations.BAND_MULTIPLIERS) | 0.150 / 0.080 / 0.212 | p25 0.018 / p50 0.063 / p75 0.212 (n=63432) | alta |
| 150k-500k | 0.85 | 0.50 | 1.60 | modelo actual (mog_operations.BAND_MULTIPLIERS) | 0.150 / 0.080 / 0.238 | p25 0.017 / p50 0.056 / p75 0.238 (n=19211) | alta |
| >500k | 0.85 | 0.50 | 1.60 | modelo actual (mog_operations.BAND_MULTIPLIERS) | 0.150 / 0.080 / 0.233 | p25 0.001 / p50 0.020 / p75 0.233 (n=4964) | alta |

> Os pisos da política (`normal ≥ 0,15`, `min ≥ 0,08`) são **decisões**, não estatística: sem eles, uma banda com clearance de 0,03 levaria a postar a 3 % do ask, o que a amostra não sustenta. Estão em `pricing_groups.BandPolicy` e são configuráveis.

## 4. Os grupos

Os grupos são **comportamento**, e os eixos são medições:

- **procura potencial** — a amostra DfA ainda não tem o look (limiar: 5% das contas amostradas). É procura potencial: não é intenção de compra nem probabilidade de venda.
- **comportamento do ask** — caiu mais de 90% em 78 dias (o preço está a descer) ou não.

| grupo | para que serve | inclui | exclui | undercut | duração | retenção |
|---|---|---|---|---:|---:|---:|
| `demand_falling` | a amostra mostra compradores em falta e o ask esta a cair: o preco tem de acompanhar | procura medida acima do limiar E o ask caiu mais de 10% em 78 dias | qualquer um dos dois criterios | 15%–25% | 12 h | 14 d |
| `demand_stable` | a amostra mostra compradores em falta e o ask segura: e o grupo que sustenta o preco | procura medida acima do limiar E o ask caiu 10% ou menos em 78 dias | procura nao medida, ou o ask a descer | 5%–15% | 48 h | 30 d |
| `no_demand_falling` | a amostra nao mostra compradores e o ask esta a cair: o preco nao se segura | procura medida abaixo do limiar E o ask caiu mais de 10% | procura acima do limiar, ou procura desconhecida | 20%–30% | 12 h | 7 d |
| `no_demand_stable` | o ask segura mas a amostra nao mostra compradores: o preco e plausivel, a venda nao | procura medida abaixo do limiar E o ask caiu 10% ou menos | procura acima do limiar, ou procura desconhecida | 10%–20% | 24 h | 14 d |
| `unknown_falling` | a amostra nao cobre o item e o ask esta a cair: so a velocidade decide | o item nao esta na amostra DfA E o ask caiu mais de 10% | procura medida, de qualquer valor | 15%–25% | 12 h | 7 d |
| `unknown_stable` | a amostra nao cobre o item e o ask segura: o preco segue a banda | o item nao esta na amostra DfA E o ask caiu 10% ou menos | procura medida, de qualquer valor | 10%–20% | 24 h | 14 d |
| `commodity` | mercado unico de commodities: o reino nao muda nada | o item tem preco de commodity na base | tudo o resto | 0%–5% | 24 h | 14 d |
| `no_reference` | sem preco nenhum: nao ha o que postar, ha o que liquidar de outra forma | sem preco de mercado, sem parede local e sem preco de venda | qualquer referencia de preco | 0%–0% | 24 h | 7 d |

### 4.1 Os grupos medem-se diferente? (a velocidade, não o preço)

A clearance (E1) **não** separa os grupos de forma limpa: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor não significa que o mercado pague menos. A estatística que separa sem esse problema é a **velocidade** (vendas/dia e taxa de venda).

| grupo | banda | itens | com movimento | vendas/dia (mediana) | taxa de venda | procura (mediana) | ask 78d | E1 p50 (diagnóstico) |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| commodity | 100-500 | 5 | 5 | 154.9760 | 0.0770 | — | 0.787 | — |
| commodity | 2k-5k | 2 | 0 | — | — | — | 0.269 | — |
| commodity | <100 | 129 | 120 | 1188.3920 | 0.2015 | — | 0.801 | — |
| demand_falling | 100-500 | 979 | 690 | 0.0130 | 0.0100 | 0.255 | 0.692 | 0.225 |
| demand_falling | 150k-500k | 165 | 95 | 0.0060 | 0.0060 | 0.698 | 0.656 | 0.041 |
| demand_falling | 15k-50k | 791 | 625 | 0.0060 | 0.0060 | 0.617 | 0.738 | 0.076 |
| demand_falling | 2k-5k | 728 | 604 | 0.0090 | 0.0080 | 0.528 | 0.686 | 0.097 |
| demand_falling | 500-2k | 1,396 | 1,080 | 0.0100 | 0.0090 | 0.404 | 0.667 | 0.140 |
| demand_falling | 50k-150k | 439 | 325 | 0.0060 | 0.0060 | 0.681 | 0.737 | 0.051 |
| demand_falling | 5k-15k | 749 | 630 | 0.0070 | 0.0070 | 0.514 | 0.740 | 0.087 |
| demand_falling | <100 | 102 | 89 | 0.0240 | 0.0160 | 0.144 | 0.606 | 0.129 |
| demand_falling | >500k | 84 | 25 | 0.0070 | 0.0070 | 0.633 | 0.649 | 0.043 |
| demand_stable | 100-500 | 1,133 | 911 | 0.0120 | 0.0110 | 0.411 | 1.161 | 0.218 |
| demand_stable | 150k-500k | 357 | 221 | 0.0060 | 0.0050 | 0.698 | 1.174 | 0.042 |
| demand_stable | 15k-50k | 1,389 | 1,041 | 0.0060 | 0.0050 | 0.574 | 1.040 | 0.061 |
| demand_stable | 2k-5k | 733 | 650 | 0.0090 | 0.0080 | 0.567 | 1.064 | 0.071 |
| demand_stable | 500-2k | 1,250 | 1,052 | 0.0110 | 0.0100 | 0.501 | 1.084 | 0.100 |
| demand_stable | 50k-150k | 850 | 599 | 0.0050 | 0.0050 | 0.708 | 1.080 | 0.052 |
| demand_stable | 5k-15k | 1,266 | 1,055 | 0.0060 | 0.0060 | 0.606 | 1.033 | 0.073 |
| demand_stable | <100 | 35 | 29 | 0.0170 | 0.0160 | 0.126 | 1.007 | 0.125 |
| demand_stable | >500k | 256 | 69 | 0.0070 | 0.0070 | 0.712 | 1.208 | 0.019 |
| no_demand_falling | 100-500 | 163 | 89 | 0.0070 | 0.0070 | 0.032 | 0.700 | 0.122 |
| no_demand_falling | 150k-500k | 7 | 4 | 0.0060 | 0.0060 | 0.040 | 0.715 | 0.055 |
| no_demand_falling | 15k-50k | 70 | 45 | 0.0050 | 0.0040 | 0.033 | 0.729 | 0.019 |
| no_demand_falling | 2k-5k | 115 | 73 | 0.0060 | 0.0050 | 0.032 | 0.655 | 0.049 |
| no_demand_falling | 500-2k | 276 | 131 | 0.0050 | 0.0050 | 0.031 | 0.646 | 0.055 |
| no_demand_falling | 50k-150k | 40 | 19 | 0.0060 | 0.0060 | 0.030 | 0.767 | 0.038 |
| no_demand_falling | 5k-15k | 93 | 60 | 0.0060 | 0.0060 | 0.033 | 0.659 | 0.040 |
| no_demand_falling | <100 | 9 | 8 | 0.0110 | 0.0110 | 0.040 | 0.751 | 0.175 |
| no_demand_falling | >500k | 2 | 1 | 0.0030 | 0.0050 | 0.047 | 0.441 | 0.005 |
| no_demand_stable | 100-500 | 166 | 87 | 0.0080 | 0.0060 | 0.031 | 1.123 | 0.131 |
| no_demand_stable | 150k-500k | 21 | 10 | 0.0050 | 0.0045 | 0.033 | 1.438 | 0.001 |
| no_demand_stable | 15k-50k | 141 | 92 | 0.0040 | 0.0035 | 0.034 | 1.240 | 0.008 |
| no_demand_stable | 2k-5k | 209 | 111 | 0.0040 | 0.0040 | 0.026 | 1.323 | 0.037 |
| no_demand_stable | 500-2k | 342 | 140 | 0.0050 | 0.0050 | 0.028 | 1.256 | 0.056 |
| no_demand_stable | 50k-150k | 71 | 49 | 0.0040 | 0.0040 | 0.033 | 1.286 | 0.005 |
| no_demand_stable | 5k-15k | 217 | 137 | 0.0040 | 0.0040 | 0.032 | 1.153 | 0.031 |
| no_demand_stable | <100 | 6 | 4 | 0.0090 | 0.0110 | 0.029 | 0.976 | 0.080 |
| no_demand_stable | >500k | 14 | 5 | 0.0100 | 0.0060 | 0.037 | 1.291 | 0.000 |
| unknown_falling | 100-500 | 1,160 | 776 | 1.8725 | 0.0320 | — | 0.499 | 0.200 |
| unknown_falling | 150k-500k | 79 | 28 | 0.0235 | 0.0115 | — | 0.661 | 0.419 |
| unknown_falling | 15k-50k | 367 | 177 | 0.0160 | 0.0100 | — | 0.707 | 0.200 |
| unknown_falling | 2k-5k | 513 | 283 | 0.1330 | 0.0200 | — | 0.600 | 0.380 |
| unknown_falling | 500-2k | 958 | 565 | 0.3400 | 0.0250 | — | 0.559 | 0.312 |
| unknown_falling | 50k-150k | 170 | 70 | 0.0160 | 0.0100 | — | 0.660 | 0.178 |
| unknown_falling | 5k-15k | 443 | 310 | 0.0390 | 0.0110 | — | 0.654 | 0.329 |
| unknown_falling | <100 | 4,307 | 3,325 | 15.4530 | 0.0700 | — | 0.390 | 0.213 |
| unknown_falling | >500k | 55 | 4 | 0.0130 | 0.0190 | — | 0.620 | 0.370 |
| unknown_stable | 100-500 | 903 | 610 | 2.4370 | 0.0365 | — | 1.430 | 0.117 |
| unknown_stable | 150k-500k | 430 | 64 | 0.0095 | 0.0085 | — | 1.000 | 0.118 |
| unknown_stable | 15k-50k | 1,295 | 361 | 0.0140 | 0.0110 | — | 1.011 | 0.160 |
| unknown_stable | 2k-5k | 763 | 228 | 0.0395 | 0.0160 | — | 1.213 | 0.137 |
| unknown_stable | 500-2k | 1,094 | 511 | 0.2550 | 0.0210 | — | 1.296 | 0.101 |
| unknown_stable | 50k-150k | 417 | 176 | 0.0140 | 0.0115 | — | 1.167 | 0.122 |
| unknown_stable | 5k-15k | 427 | 266 | 0.0180 | 0.0090 | — | 1.126 | 0.208 |
| unknown_stable | <100 | 2,721 | 2,271 | 24.8400 | 0.0880 | — | 1.410 | 0.471 |
| unknown_stable | >500k | 253 | 28 | 0.0080 | 0.0080 | — | 1.285 | 0.008 |

**Teste de distinção** (velocidade com diferença ≥ 20% e amostra ≥ 100):

- **43** pares **distinguem-se** pela velocidade → merecem estratégias diferentes;
- **8** pares **não se distinguem** → candidatas a fundir (o mesmo grupo, com o mesmo undercut e a mesma janela);
- **84** pares **inconclusivos** (amostra insuficiente): não se fundem por falta de prova.

<details><summary>detalhe</summary>

- `demand_falling` e `demand_stable` na banda 100-500: **fundir** — velocidade quase igual (0.013 vs 0.012 vendas/dia, diferença 8%)
- `demand_falling` e `no_demand_falling` na banda 100-500: inconclusivo — amostra de turnover insuficiente (690 / 89)
- `demand_falling` e `no_demand_stable` na banda 100-500: inconclusivo — amostra de turnover insuficiente (690 / 87)
- `demand_falling` e `unknown_falling` na banda 100-500: distintos — velocidade diferente (0.013 vs 1.873 vendas/dia, diferença 99%)
- `demand_falling` e `unknown_stable` na banda 100-500: distintos — velocidade diferente (0.013 vs 2.437 vendas/dia, diferença 99%)
- `demand_falling` e `demand_stable` na banda 150k-500k: inconclusivo — amostra de turnover insuficiente (95 / 221)
- `demand_falling` e `no_demand_falling` na banda 150k-500k: inconclusivo — amostra de turnover insuficiente (95 / 4)
- `demand_falling` e `no_demand_stable` na banda 150k-500k: inconclusivo — amostra de turnover insuficiente (95 / 10)
- `demand_falling` e `unknown_falling` na banda 150k-500k: inconclusivo — amostra de turnover insuficiente (95 / 28)
- `demand_falling` e `unknown_stable` na banda 150k-500k: inconclusivo — amostra de turnover insuficiente (95 / 64)
- `demand_falling` e `demand_stable` na banda 15k-50k: **fundir** — velocidade quase igual (0.006 vs 0.006 vendas/dia, diferença 0%)
- `demand_falling` e `no_demand_falling` na banda 15k-50k: inconclusivo — amostra de turnover insuficiente (625 / 45)
- `demand_falling` e `no_demand_stable` na banda 15k-50k: inconclusivo — amostra de turnover insuficiente (625 / 92)
- `demand_falling` e `unknown_falling` na banda 15k-50k: distintos — velocidade diferente (0.006 vs 0.016 vendas/dia, diferença 62%)
- `demand_falling` e `unknown_stable` na banda 15k-50k: distintos — velocidade diferente (0.006 vs 0.014 vendas/dia, diferença 57%)
- `demand_falling` e `demand_stable` na banda 2k-5k: **fundir** — velocidade quase igual (0.009 vs 0.009 vendas/dia, diferença 0%)
- `demand_falling` e `no_demand_falling` na banda 2k-5k: inconclusivo — amostra de turnover insuficiente (604 / 73)
- `demand_falling` e `no_demand_stable` na banda 2k-5k: distintos — velocidade diferente (0.009 vs 0.004 vendas/dia, diferença 56%)
- `demand_falling` e `unknown_falling` na banda 2k-5k: distintos — velocidade diferente (0.009 vs 0.133 vendas/dia, diferença 93%)
- `demand_falling` e `unknown_stable` na banda 2k-5k: distintos — velocidade diferente (0.009 vs 0.040 vendas/dia, diferença 77%)
- `demand_falling` e `demand_stable` na banda 500-2k: **fundir** — velocidade quase igual (0.010 vs 0.011 vendas/dia, diferença 9%)
- `demand_falling` e `no_demand_falling` na banda 500-2k: distintos — velocidade diferente (0.010 vs 0.005 vendas/dia, diferença 50%)
- `demand_falling` e `no_demand_stable` na banda 500-2k: distintos — velocidade diferente (0.010 vs 0.005 vendas/dia, diferença 50%)
- `demand_falling` e `unknown_falling` na banda 500-2k: distintos — velocidade diferente (0.010 vs 0.340 vendas/dia, diferença 97%)
- `demand_falling` e `unknown_stable` na banda 500-2k: distintos — velocidade diferente (0.010 vs 0.255 vendas/dia, diferença 96%)
- `demand_falling` e `demand_stable` na banda 50k-150k: **fundir** — velocidade quase igual (0.006 vs 0.005 vendas/dia, diferença 17%)
- `demand_falling` e `no_demand_falling` na banda 50k-150k: inconclusivo — amostra de turnover insuficiente (325 / 19)
- `demand_falling` e `no_demand_stable` na banda 50k-150k: inconclusivo — amostra de turnover insuficiente (325 / 49)
- `demand_falling` e `unknown_falling` na banda 50k-150k: inconclusivo — amostra de turnover insuficiente (325 / 70)
- `demand_falling` e `unknown_stable` na banda 50k-150k: distintos — velocidade diferente (0.006 vs 0.014 vendas/dia, diferença 57%)
- `demand_falling` e `demand_stable` na banda 5k-15k: **fundir** — velocidade quase igual (0.007 vs 0.006 vendas/dia, diferença 14%)
- `demand_falling` e `no_demand_falling` na banda 5k-15k: inconclusivo — amostra de turnover insuficiente (630 / 60)
- `demand_falling` e `no_demand_stable` na banda 5k-15k: distintos — velocidade diferente (0.007 vs 0.004 vendas/dia, diferença 43%)
- `demand_falling` e `unknown_falling` na banda 5k-15k: distintos — velocidade diferente (0.007 vs 0.039 vendas/dia, diferença 82%)
- `demand_falling` e `unknown_stable` na banda 5k-15k: distintos — velocidade diferente (0.007 vs 0.018 vendas/dia, diferença 61%)
- `demand_falling` e `demand_stable` na banda <100: inconclusivo — amostra de turnover insuficiente (89 / 29)
- `demand_falling` e `no_demand_falling` na banda <100: inconclusivo — amostra de turnover insuficiente (89 / 8)
- `demand_falling` e `no_demand_stable` na banda <100: inconclusivo — amostra de turnover insuficiente (89 / 4)
- `demand_falling` e `unknown_falling` na banda <100: inconclusivo — amostra de turnover insuficiente (89 / 3325)
- `demand_falling` e `unknown_stable` na banda <100: inconclusivo — amostra de turnover insuficiente (89 / 2271)
- `demand_falling` e `demand_stable` na banda >500k: inconclusivo — amostra de turnover insuficiente (25 / 69)
- `demand_falling` e `no_demand_falling` na banda >500k: inconclusivo — amostra de turnover insuficiente (25 / 1)
- `demand_falling` e `no_demand_stable` na banda >500k: inconclusivo — amostra de turnover insuficiente (25 / 5)
- `demand_falling` e `unknown_falling` na banda >500k: inconclusivo — amostra de turnover insuficiente (25 / 4)
- `demand_falling` e `unknown_stable` na banda >500k: inconclusivo — amostra de turnover insuficiente (25 / 28)
- `demand_stable` e `no_demand_falling` na banda 100-500: inconclusivo — amostra de turnover insuficiente (911 / 89)
- `demand_stable` e `no_demand_stable` na banda 100-500: inconclusivo — amostra de turnover insuficiente (911 / 87)
- `demand_stable` e `unknown_falling` na banda 100-500: distintos — velocidade diferente (0.012 vs 1.873 vendas/dia, diferença 99%)
- `demand_stable` e `unknown_stable` na banda 100-500: distintos — velocidade diferente (0.012 vs 2.437 vendas/dia, diferença 100%)
- `demand_stable` e `no_demand_falling` na banda 150k-500k: inconclusivo — amostra de turnover insuficiente (221 / 4)
- `demand_stable` e `no_demand_stable` na banda 150k-500k: inconclusivo — amostra de turnover insuficiente (221 / 10)
- `demand_stable` e `unknown_falling` na banda 150k-500k: inconclusivo — amostra de turnover insuficiente (221 / 28)
- `demand_stable` e `unknown_stable` na banda 150k-500k: inconclusivo — amostra de turnover insuficiente (221 / 64)
- `demand_stable` e `no_demand_falling` na banda 15k-50k: inconclusivo — amostra de turnover insuficiente (1041 / 45)
- `demand_stable` e `no_demand_stable` na banda 15k-50k: inconclusivo — amostra de turnover insuficiente (1041 / 92)
- `demand_stable` e `unknown_falling` na banda 15k-50k: distintos — velocidade diferente (0.006 vs 0.016 vendas/dia, diferença 62%)
- `demand_stable` e `unknown_stable` na banda 15k-50k: distintos — velocidade diferente (0.006 vs 0.014 vendas/dia, diferença 57%)
- `demand_stable` e `no_demand_falling` na banda 2k-5k: inconclusivo — amostra de turnover insuficiente (650 / 73)
- `demand_stable` e `no_demand_stable` na banda 2k-5k: distintos — velocidade diferente (0.009 vs 0.004 vendas/dia, diferença 56%)
- `demand_stable` e `unknown_falling` na banda 2k-5k: distintos — velocidade diferente (0.009 vs 0.133 vendas/dia, diferença 93%)
- `demand_stable` e `unknown_stable` na banda 2k-5k: distintos — velocidade diferente (0.009 vs 0.040 vendas/dia, diferença 77%)
- `demand_stable` e `no_demand_falling` na banda 500-2k: distintos — velocidade diferente (0.011 vs 0.005 vendas/dia, diferença 55%)
- `demand_stable` e `no_demand_stable` na banda 500-2k: distintos — velocidade diferente (0.011 vs 0.005 vendas/dia, diferença 55%)
- `demand_stable` e `unknown_falling` na banda 500-2k: distintos — velocidade diferente (0.011 vs 0.340 vendas/dia, diferença 97%)
- `demand_stable` e `unknown_stable` na banda 500-2k: distintos — velocidade diferente (0.011 vs 0.255 vendas/dia, diferença 96%)
- `demand_stable` e `no_demand_falling` na banda 50k-150k: inconclusivo — amostra de turnover insuficiente (599 / 19)
- `demand_stable` e `no_demand_stable` na banda 50k-150k: inconclusivo — amostra de turnover insuficiente (599 / 49)
- `demand_stable` e `unknown_falling` na banda 50k-150k: inconclusivo — amostra de turnover insuficiente (599 / 70)
- `demand_stable` e `unknown_stable` na banda 50k-150k: distintos — velocidade diferente (0.005 vs 0.014 vendas/dia, diferença 64%)
- `demand_stable` e `no_demand_falling` na banda 5k-15k: inconclusivo — amostra de turnover insuficiente (1055 / 60)
- `demand_stable` e `no_demand_stable` na banda 5k-15k: distintos — velocidade diferente (0.006 vs 0.004 vendas/dia, diferença 33%)
- `demand_stable` e `unknown_falling` na banda 5k-15k: distintos — velocidade diferente (0.006 vs 0.039 vendas/dia, diferença 85%)
- `demand_stable` e `unknown_stable` na banda 5k-15k: distintos — velocidade diferente (0.006 vs 0.018 vendas/dia, diferença 67%)
- `demand_stable` e `no_demand_falling` na banda <100: inconclusivo — amostra de turnover insuficiente (29 / 8)
- `demand_stable` e `no_demand_stable` na banda <100: inconclusivo — amostra de turnover insuficiente (29 / 4)
- `demand_stable` e `unknown_falling` na banda <100: inconclusivo — amostra de turnover insuficiente (29 / 3325)
- `demand_stable` e `unknown_stable` na banda <100: inconclusivo — amostra de turnover insuficiente (29 / 2271)
- `demand_stable` e `no_demand_falling` na banda >500k: inconclusivo — amostra de turnover insuficiente (69 / 1)
- `demand_stable` e `no_demand_stable` na banda >500k: inconclusivo — amostra de turnover insuficiente (69 / 5)
- `demand_stable` e `unknown_falling` na banda >500k: inconclusivo — amostra de turnover insuficiente (69 / 4)
- `demand_stable` e `unknown_stable` na banda >500k: inconclusivo — amostra de turnover insuficiente (69 / 28)
- `no_demand_falling` e `no_demand_stable` na banda 100-500: inconclusivo — amostra de turnover insuficiente (89 / 87)
- `no_demand_falling` e `unknown_falling` na banda 100-500: inconclusivo — amostra de turnover insuficiente (89 / 776)
- `no_demand_falling` e `unknown_stable` na banda 100-500: inconclusivo — amostra de turnover insuficiente (89 / 610)
- `no_demand_falling` e `no_demand_stable` na banda 150k-500k: inconclusivo — amostra de turnover insuficiente (4 / 10)
- `no_demand_falling` e `unknown_falling` na banda 150k-500k: inconclusivo — amostra de turnover insuficiente (4 / 28)
- `no_demand_falling` e `unknown_stable` na banda 150k-500k: inconclusivo — amostra de turnover insuficiente (4 / 64)
- `no_demand_falling` e `no_demand_stable` na banda 15k-50k: inconclusivo — amostra de turnover insuficiente (45 / 92)
- `no_demand_falling` e `unknown_falling` na banda 15k-50k: inconclusivo — amostra de turnover insuficiente (45 / 177)
- `no_demand_falling` e `unknown_stable` na banda 15k-50k: inconclusivo — amostra de turnover insuficiente (45 / 361)
- `no_demand_falling` e `no_demand_stable` na banda 2k-5k: inconclusivo — amostra de turnover insuficiente (73 / 111)
- `no_demand_falling` e `unknown_falling` na banda 2k-5k: inconclusivo — amostra de turnover insuficiente (73 / 283)
- `no_demand_falling` e `unknown_stable` na banda 2k-5k: inconclusivo — amostra de turnover insuficiente (73 / 228)
- `no_demand_falling` e `no_demand_stable` na banda 500-2k: **fundir** — velocidade quase igual (0.005 vs 0.005 vendas/dia, diferença 0%)
- `no_demand_falling` e `unknown_falling` na banda 500-2k: distintos — velocidade diferente (0.005 vs 0.340 vendas/dia, diferença 99%)
- `no_demand_falling` e `unknown_stable` na banda 500-2k: distintos — velocidade diferente (0.005 vs 0.255 vendas/dia, diferença 98%)
- `no_demand_falling` e `no_demand_stable` na banda 50k-150k: inconclusivo — amostra de turnover insuficiente (19 / 49)
- `no_demand_falling` e `unknown_falling` na banda 50k-150k: inconclusivo — amostra de turnover insuficiente (19 / 70)
- `no_demand_falling` e `unknown_stable` na banda 50k-150k: inconclusivo — amostra de turnover insuficiente (19 / 176)
- `no_demand_falling` e `no_demand_stable` na banda 5k-15k: inconclusivo — amostra de turnover insuficiente (60 / 137)
- `no_demand_falling` e `unknown_falling` na banda 5k-15k: inconclusivo — amostra de turnover insuficiente (60 / 310)
- `no_demand_falling` e `unknown_stable` na banda 5k-15k: inconclusivo — amostra de turnover insuficiente (60 / 266)
- `no_demand_falling` e `no_demand_stable` na banda <100: inconclusivo — amostra de turnover insuficiente (8 / 4)
- `no_demand_falling` e `unknown_falling` na banda <100: inconclusivo — amostra de turnover insuficiente (8 / 3325)
- `no_demand_falling` e `unknown_stable` na banda <100: inconclusivo — amostra de turnover insuficiente (8 / 2271)
- `no_demand_falling` e `no_demand_stable` na banda >500k: inconclusivo — amostra de turnover insuficiente (1 / 5)
- `no_demand_falling` e `unknown_falling` na banda >500k: inconclusivo — amostra de turnover insuficiente (1 / 4)
- `no_demand_falling` e `unknown_stable` na banda >500k: inconclusivo — amostra de turnover insuficiente (1 / 28)
- `no_demand_stable` e `unknown_falling` na banda 100-500: inconclusivo — amostra de turnover insuficiente (87 / 776)
- `no_demand_stable` e `unknown_stable` na banda 100-500: inconclusivo — amostra de turnover insuficiente (87 / 610)
- `no_demand_stable` e `unknown_falling` na banda 150k-500k: inconclusivo — amostra de turnover insuficiente (10 / 28)
- `no_demand_stable` e `unknown_stable` na banda 150k-500k: inconclusivo — amostra de turnover insuficiente (10 / 64)
- `no_demand_stable` e `unknown_falling` na banda 15k-50k: inconclusivo — amostra de turnover insuficiente (92 / 177)
- `no_demand_stable` e `unknown_stable` na banda 15k-50k: inconclusivo — amostra de turnover insuficiente (92 / 361)
- `no_demand_stable` e `unknown_falling` na banda 2k-5k: distintos — velocidade diferente (0.004 vs 0.133 vendas/dia, diferença 97%)
- `no_demand_stable` e `unknown_stable` na banda 2k-5k: distintos — velocidade diferente (0.004 vs 0.040 vendas/dia, diferença 90%)
- `no_demand_stable` e `unknown_falling` na banda 500-2k: distintos — velocidade diferente (0.005 vs 0.340 vendas/dia, diferença 99%)
- `no_demand_stable` e `unknown_stable` na banda 500-2k: distintos — velocidade diferente (0.005 vs 0.255 vendas/dia, diferença 98%)
- `no_demand_stable` e `unknown_falling` na banda 50k-150k: inconclusivo — amostra de turnover insuficiente (49 / 70)
- `no_demand_stable` e `unknown_stable` na banda 50k-150k: inconclusivo — amostra de turnover insuficiente (49 / 176)
- `no_demand_stable` e `unknown_falling` na banda 5k-15k: distintos — velocidade diferente (0.004 vs 0.039 vendas/dia, diferença 90%)
- `no_demand_stable` e `unknown_stable` na banda 5k-15k: distintos — velocidade diferente (0.004 vs 0.018 vendas/dia, diferença 78%)
- `no_demand_stable` e `unknown_falling` na banda <100: inconclusivo — amostra de turnover insuficiente (4 / 3325)
- `no_demand_stable` e `unknown_stable` na banda <100: inconclusivo — amostra de turnover insuficiente (4 / 2271)
- `no_demand_stable` e `unknown_falling` na banda >500k: inconclusivo — amostra de turnover insuficiente (5 / 4)
- `no_demand_stable` e `unknown_stable` na banda >500k: inconclusivo — amostra de turnover insuficiente (5 / 28)
- `unknown_falling` e `unknown_stable` na banda 100-500: distintos — velocidade diferente (1.873 vs 2.437 vendas/dia, diferença 23%)
- `unknown_falling` e `unknown_stable` na banda 150k-500k: inconclusivo — amostra de turnover insuficiente (28 / 64)
- `unknown_falling` e `unknown_stable` na banda 15k-50k: **fundir** — velocidade quase igual (0.016 vs 0.014 vendas/dia, diferença 12%)
- `unknown_falling` e `unknown_stable` na banda 2k-5k: distintos — velocidade diferente (0.133 vs 0.040 vendas/dia, diferença 70%)
- `unknown_falling` e `unknown_stable` na banda 500-2k: distintos — velocidade diferente (0.340 vs 0.255 vendas/dia, diferença 25%)
- `unknown_falling` e `unknown_stable` na banda 50k-150k: inconclusivo — amostra de turnover insuficiente (70 / 176)
- `unknown_falling` e `unknown_stable` na banda 5k-15k: distintos — velocidade diferente (0.039 vs 0.018 vendas/dia, diferença 54%)
- `unknown_falling` e `unknown_stable` na banda <100: distintos — velocidade diferente (15.453 vs 24.840 vendas/dia, diferença 38%)
- `unknown_falling` e `unknown_stable` na banda >500k: inconclusivo — amostra de turnover insuficiente (4 / 28)

</details>


### 4.2 O ajuste de preço de cada grupo

O ajuste é **1,00** em quase todos os grupos, e isso é uma conclusão, não uma omissão:

- a única medida em que o sinal está identificado é a **venda da própria conta** (o preço foi escolhido por nós). E2 não está disponível (§1), portanto esse caminho está fechado;
- o E1 medido (a coluna *E1 p50* acima) **não** é aplicado: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes.

O que resta é a **classificação**, que é o que separa as estratégias operacionais (undercut, duração, retenção) e o que decide a quem se pergunta primeiro. A tabela mostra a razão de cada ajuste.

| grupo | banda | ajuste | n | razão | E1 medido | material |
|---|---|---:|---:|---|---:|---|
| demand_falling | 100-500 | 1.00 | 44,622 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 1.115 | não |
| demand_falling | 150k-500k | 1.00 | 4,291 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.736 | não |
| demand_falling | 15k-50k | 1.00 | 31,843 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.943 | não |
| demand_falling | 2k-5k | 1.00 | 31,536 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.935 | não |
| demand_falling | 500-2k | 1.00 | 60,283 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 1.176 | não |
| demand_falling | 50k-150k | 1.00 | 15,280 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.812 | não |
| demand_falling | 5k-15k | 1.00 | 32,430 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.821 | não |
| demand_falling | <100 | 1.00 | 6,222 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.711 | não |
| demand_falling | >500k | 1.00 | 731 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 2.138 | não |
| demand_stable | 100-500 | 1.00 | 59,505 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 1.081 | não |
| demand_stable | 150k-500k | 1.00 | 9,898 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.764 | não |
| demand_stable | 15k-50k | 1.00 | 58,160 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.763 | não |
| demand_stable | 2k-5k | 1.00 | 32,301 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.684 | não |
| demand_stable | 500-2k | 1.00 | 53,311 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.841 | não |
| demand_stable | 50k-150k | 1.00 | 31,787 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.828 | não |
| demand_stable | 5k-15k | 1.00 | 54,171 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.686 | não |
| demand_stable | <100 | 1.00 | 1,718 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.687 | não |
| demand_stable | >500k | 1.00 | 2,469 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.963 | não |
| no_demand_falling | 100-500 | 1.00 | 5,865 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.607 | não |
| no_demand_falling | 150k-500k | 1.00 | 109 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.997 | não |
| no_demand_falling | 15k-50k | 1.00 | 1,715 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.236 | não |
| no_demand_falling | 2k-5k | 1.00 | 3,583 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.470 | não |
| no_demand_falling | 500-2k | 1.00 | 7,695 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.463 | não |
| no_demand_falling | 50k-150k | 1.00 | 1,072 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.611 | não |
| no_demand_falling | 5k-15k | 1.00 | 2,534 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.379 | não |
| no_demand_falling | <100 | 1.00 | 452 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.963 | não |
| no_demand_falling | >500k | 1.00 | 20 | sem amostra suficiente (n=20 < 100): 1,00 heurístico | 0.234 | não |
| no_demand_stable | 100-500 | 1.00 | 6,682 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.649 | não |
| no_demand_stable | 150k-500k | 1.00 | 407 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.026 | não |
| no_demand_stable | 15k-50k | 1.00 | 3,679 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.100 | não |
| no_demand_stable | 2k-5k | 1.00 | 6,245 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.358 | não |
| no_demand_stable | 500-2k | 1.00 | 10,206 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.471 | não |
| no_demand_stable | 50k-150k | 1.00 | 1,814 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.072 | não |
| no_demand_stable | 5k-15k | 1.00 | 6,409 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.293 | não |
| no_demand_stable | <100 | 1.00 | 254 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.439 | não |
| no_demand_stable | >500k | 1.00 | 141 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.016 | não |
| unknown_falling | 100-500 | 1.00 | 12,038 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.992 | não |
| unknown_falling | 150k-500k | 1.00 | 1,476 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 7.544 | não |
| unknown_falling | 15k-50k | 1.00 | 11,410 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 2.491 | não |
| unknown_falling | 2k-5k | 1.00 | 17,380 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 3.674 | não |
| unknown_falling | 500-2k | 1.00 | 20,845 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 2.626 | não |
| unknown_falling | 50k-150k | 1.00 | 3,806 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 2.843 | não |
| unknown_falling | 5k-15k | 1.00 | 22,069 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 3.093 | não |
| unknown_falling | <100 | 1.00 | 4,918 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 1.173 | não |
| unknown_falling | >500k | 1.00 | 191 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 18.499 | não |
| unknown_stable | 100-500 | 1.00 | 7,827 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.582 | não |
| unknown_stable | 150k-500k | 1.00 | 3,030 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 2.132 | não |
| unknown_stable | 15k-50k | 1.00 | 23,904 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 1.989 | não |
| unknown_stable | 2k-5k | 1.00 | 10,950 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 1.321 | não |
| unknown_stable | 500-2k | 1.00 | 13,656 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.851 | não |
| unknown_stable | 50k-150k | 1.00 | 9,673 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 1.953 | não |
| unknown_stable | 5k-15k | 1.00 | 17,128 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 1.959 | não |
| unknown_stable | <100 | 1.00 | 2,196 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 2.590 | não |
| unknown_stable | >500k | 1.00 | 1,412 | 1,00 heurístico: E1 não está identificada para o preço: num mercado popular a média de venda é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o mercado pague menos — significa que há mais snipes | 0.380 | não |

## 5. As estratégias

A fórmula normal e a de liquidação estão **separadas**, e os factores entram uma vez cada:

```
normal = banda_normal × ajuste_do_grupo × âncora_local
âncora_local = first(dbmarket, dbregionmarketavg, dbregionhistorical)
```

A âncora é **local**, por isso o factor do reino (local ÷ regional) **não** é aplicado outra vez aqui: contaria o mesmo ajuste duas vezes. O factor do reino vive no export por reino e na camada de campanha, onde é aplicado uma vez.

| grupo | banda | normal | min | max | ajuste | rush | duração | retenção |
|---|---|---|---|---|---:|---|---:|---:|
| demand_falling | 100-500 | 0.550 × âncora | 0.300 × âncora | 1.000 × âncora | 1.00 | parede × (1 − undercut) | 12 h | 14 d |
| demand_falling | 150k-500k | 0.850 × âncora | 0.500 × âncora | 1.600 × âncora | 1.00 | parede × (1 − undercut) | 12 h | 14 d |
| demand_falling | 15k-50k | 0.850 × âncora | 0.500 × âncora | 1.600 × âncora | 1.00 | parede × (1 − undercut) | 12 h | 14 d |
| demand_falling | 2k-5k | 0.680 × âncora | 0.400 × âncora | 1.300 × âncora | 1.00 | parede × (1 − undercut) | 12 h | 14 d |
| demand_falling | 500-2k | 0.620 × âncora | 0.350 × âncora | 1.150 × âncora | 1.00 | parede × (1 − undercut) | 12 h | 14 d |
| demand_falling | 50k-150k | 0.850 × âncora | 0.500 × âncora | 1.600 × âncora | 1.00 | parede × (1 − undercut) | 12 h | 14 d |
| demand_falling | 5k-15k | 0.780 × âncora | 0.450 × âncora | 1.450 × âncora | 1.00 | parede × (1 − undercut) | 12 h | 14 d |
| demand_falling | <100 | 0.450 × âncora | 0.250 × âncora | 0.900 × âncora | 1.00 | parede × (1 − undercut) | 12 h | 14 d |
| demand_falling | >500k | 0.850 × âncora | 0.500 × âncora | 1.600 × âncora | 1.00 | parede × (1 − undercut) | 12 h | 14 d |
| demand_stable | 100-500 | 0.550 × âncora | 0.300 × âncora | 1.000 × âncora | 1.00 | parede × (1 − undercut) | 48 h | 30 d |
| demand_stable | 150k-500k | 0.850 × âncora | 0.500 × âncora | 1.600 × âncora | 1.00 | parede × (1 − undercut) | 48 h | 30 d |
| demand_stable | 15k-50k | 0.850 × âncora | 0.500 × âncora | 1.600 × âncora | 1.00 | parede × (1 − undercut) | 48 h | 30 d |
| demand_stable | 2k-5k | 0.680 × âncora | 0.400 × âncora | 1.300 × âncora | 1.00 | parede × (1 − undercut) | 48 h | 30 d |
| demand_stable | 500-2k | 0.620 × âncora | 0.350 × âncora | 1.150 × âncora | 1.00 | parede × (1 − undercut) | 48 h | 30 d |
| demand_stable | 50k-150k | 0.850 × âncora | 0.500 × âncora | 1.600 × âncora | 1.00 | parede × (1 − undercut) | 48 h | 30 d |
| demand_stable | 5k-15k | 0.780 × âncora | 0.450 × âncora | 1.450 × âncora | 1.00 | parede × (1 − undercut) | 48 h | 30 d |
| demand_stable | <100 | 0.450 × âncora | 0.250 × âncora | 0.900 × âncora | 1.00 | parede × (1 − undercut) | 48 h | 30 d |
| demand_stable | >500k | 0.850 × âncora | 0.500 × âncora | 1.600 × âncora | 1.00 | parede × (1 − undercut) | 48 h | 30 d |
| no_demand_falling | 100-500 | 0.550 × âncora | 0.300 × âncora | 1.000 × âncora | 1.00 | parede × (1 − undercut) | 12 h | 7 d |
| no_demand_falling | 150k-500k | 0.850 × âncora | 0.500 × âncora | 1.600 × âncora | 1.00 | parede × (1 − undercut) | 12 h | 7 d |
| no_demand_falling | 15k-50k | 0.850 × âncora | 0.500 × âncora | 1.600 × âncora | 1.00 | parede × (1 − undercut) | 12 h | 7 d |
| no_demand_falling | 2k-5k | 0.680 × âncora | 0.400 × âncora | 1.300 × âncora | 1.00 | parede × (1 − undercut) | 12 h | 7 d |
| no_demand_falling | 500-2k | 0.620 × âncora | 0.350 × âncora | 1.150 × âncora | 1.00 | parede × (1 − undercut) | 12 h | 7 d |
| no_demand_falling | 50k-150k | 0.850 × âncora | 0.500 × âncora | 1.600 × âncora | 1.00 | parede × (1 − undercut) | 12 h | 7 d |
| no_demand_falling | 5k-15k | 0.780 × âncora | 0.450 × âncora | 1.450 × âncora | 1.00 | parede × (1 − undercut) | 12 h | 7 d |
| no_demand_falling | <100 | 0.450 × âncora | 0.250 × âncora | 0.900 × âncora | 1.00 | parede × (1 − undercut) | 12 h | 7 d |
| no_demand_falling | >500k | 0.850 × âncora | 0.500 × âncora | 1.600 × âncora | 1.00 | parede × (1 − undercut) | 12 h | 7 d |
| no_demand_stable | 100-500 | 0.550 × âncora | 0.300 × âncora | 1.000 × âncora | 1.00 | parede × (1 − undercut) | 24 h | 14 d |
| no_demand_stable | 150k-500k | 0.850 × âncora | 0.500 × âncora | 1.600 × âncora | 1.00 | parede × (1 − undercut) | 24 h | 14 d |
| no_demand_stable | 15k-50k | 0.850 × âncora | 0.500 × âncora | 1.600 × âncora | 1.00 | parede × (1 − undercut) | 24 h | 14 d |
| no_demand_stable | 2k-5k | 0.680 × âncora | 0.400 × âncora | 1.300 × âncora | 1.00 | parede × (1 − undercut) | 24 h | 14 d |
| no_demand_stable | 500-2k | 0.620 × âncora | 0.350 × âncora | 1.150 × âncora | 1.00 | parede × (1 − undercut) | 24 h | 14 d |
| no_demand_stable | 50k-150k | 0.850 × âncora | 0.500 × âncora | 1.600 × âncora | 1.00 | parede × (1 − undercut) | 24 h | 14 d |
| no_demand_stable | 5k-15k | 0.780 × âncora | 0.450 × âncora | 1.450 × âncora | 1.00 | parede × (1 − undercut) | 24 h | 14 d |
| no_demand_stable | <100 | 0.450 × âncora | 0.250 × âncora | 0.900 × âncora | 1.00 | parede × (1 − undercut) | 24 h | 14 d |
| no_demand_stable | >500k | 0.850 × âncora | 0.500 × âncora | 1.600 × âncora | 1.00 | parede × (1 − undercut) | 24 h | 14 d |
| unknown_falling | 100-500 | 0.550 × âncora | 0.300 × âncora | 1.000 × âncora | 1.00 | parede × (1 − undercut) | 12 h | 7 d |
| unknown_falling | 150k-500k | 0.850 × âncora | 0.500 × âncora | 1.600 × âncora | 1.00 | parede × (1 − undercut) | 12 h | 7 d |
| unknown_falling | 15k-50k | 0.850 × âncora | 0.500 × âncora | 1.600 × âncora | 1.00 | parede × (1 − undercut) | 12 h | 7 d |
| unknown_falling | 2k-5k | 0.680 × âncora | 0.400 × âncora | 1.300 × âncora | 1.00 | parede × (1 − undercut) | 12 h | 7 d |
| unknown_falling | 500-2k | 0.620 × âncora | 0.350 × âncora | 1.150 × âncora | 1.00 | parede × (1 − undercut) | 12 h | 7 d |
| unknown_falling | 50k-150k | 0.850 × âncora | 0.500 × âncora | 1.600 × âncora | 1.00 | parede × (1 − undercut) | 12 h | 7 d |
| unknown_falling | 5k-15k | 0.780 × âncora | 0.450 × âncora | 1.450 × âncora | 1.00 | parede × (1 − undercut) | 12 h | 7 d |
| unknown_falling | <100 | 0.450 × âncora | 0.250 × âncora | 0.900 × âncora | 1.00 | parede × (1 − undercut) | 12 h | 7 d |
| unknown_falling | >500k | 0.850 × âncora | 0.500 × âncora | 1.600 × âncora | 1.00 | parede × (1 − undercut) | 12 h | 7 d |
| unknown_stable | 100-500 | 0.550 × âncora | 0.300 × âncora | 1.000 × âncora | 1.00 | parede × (1 − undercut) | 24 h | 14 d |
| unknown_stable | 150k-500k | 0.850 × âncora | 0.500 × âncora | 1.600 × âncora | 1.00 | parede × (1 − undercut) | 24 h | 14 d |
| unknown_stable | 15k-50k | 0.850 × âncora | 0.500 × âncora | 1.600 × âncora | 1.00 | parede × (1 − undercut) | 24 h | 14 d |
| unknown_stable | 2k-5k | 0.680 × âncora | 0.400 × âncora | 1.300 × âncora | 1.00 | parede × (1 − undercut) | 24 h | 14 d |
| unknown_stable | 500-2k | 0.620 × âncora | 0.350 × âncora | 1.150 × âncora | 1.00 | parede × (1 − undercut) | 24 h | 14 d |
| unknown_stable | 50k-150k | 0.850 × âncora | 0.500 × âncora | 1.600 × âncora | 1.00 | parede × (1 − undercut) | 24 h | 14 d |
| unknown_stable | 5k-15k | 0.780 × âncora | 0.450 × âncora | 1.450 × âncora | 1.00 | parede × (1 − undercut) | 24 h | 14 d |
| unknown_stable | <100 | 0.450 × âncora | 0.250 × âncora | 0.900 × âncora | 1.00 | parede × (1 − undercut) | 24 h | 14 d |
| unknown_stable | >500k | 0.850 × âncora | 0.500 × âncora | 1.600 × âncora | 1.00 | parede × (1 − undercut) | 24 h | 14 d |

### 5.1 Semântica do TSM em que isto assenta

- **competitive_price**: minPrice <= lowestAuction.buyout - undercut <= maxPrice
- **no_competition_price**: normalPrice
- **below_minimum**: não posta (priceReset = none)
- **above_maximum**: posta no maxPrice (aboveMax = maxPrice)
- **max_expires**: expirações desde a última venda (janela de retenção)
- **undercut**: retail sanitiza undercut < 1s para 0c
- **duration**: horas de cada post

O alvo de liquidação é **relativo à parede** quando existe um comparável válido: a parede é o ask mais barato do mercado e undercutê-la é como a venda acontece. O ajuste do grupo **não** é aplicado à parede: foi estimado sobre o *ask*, e não há evidência de que se transfira para a parede. Sem comparável válido, o alvo volta à fórmula normal.

## 6. Inventário: onde cai cada peça

Alvo: **Spinebreaker**. Cada linha é um item do inventário, com a sua banda, o grupo e a estratégia.

| grupo | peças | valor de mercado | undercut | duração | retenção |
|---|---:|---:|---:|---:|---:|
| demand_stable | 80 | 394,936 g | 5%–15% | 48 h | 30 d |
| demand_falling | 75 | 157,720 g | 15%–25% | 12 h | 14 d |
| unknown_stable | 10 | 89,703 g | 10%–20% | 24 h | 14 d |
| unknown_falling | 10 | 11,006 g | 15%–25% | 12 h | 7 d |
| commodity | 136 | 7,814 g | 0%–5% | 24 h | 14 d |
| no_demand_stable | 3 | 4,620 g | 10%–20% | 24 h | 14 d |
| no_demand_falling | 3 | 4,448 g | 20%–30% | 12 h | 7 d |
| no_reference | 11 | 0 g | 0%–0% | 24 h | 7 d |

| item | banda | grupo | procura | ask 78d | parede | vendas | expirações |
|---|---|---|---:|---:|---:|---:|---:|
| Wrangler's Buckler of the Quic (15332) | 50k-150k | demand_stable | 0.748 | 1.067 | 74727.29 | 0 | 0 |
| Bristlebark Blouse (14570) | 50k-150k | demand_stable | 0.775 | 0.923 | 44960.0 | 0 | 0 |
| Pagan Vest of the Quickblade (14158) | 50k-150k | demand_falling | 0.918 | 0.709 | 52696.51 | 0 | 0 |
| Overseer's Mandate (174790) | 50k-150k | unknown_stable | — | 2.143 | 56100.29 | 0 | 0 |
| Slippers of the Hissing Cult (271435) | 15k-50k | demand_stable | 0.979 | — | 39623.81 | 0 | 0 |
| Evocator's Blade (2567) | 15k-50k | demand_stable | 0.549 | 1.101 | 14951.45 | 0 | 0 |
| Pattern: Hands of Darkness (7092) | 15k-50k | unknown_stable | — | 1.265 | 18227.55 | 0 | 0 |
| Mystic's Robe (14371) | 15k-50k | demand_stable | 0.858 | 0.989 | 9965.75 | 0 | 0 |
| Rigid Tunic (15118) | 15k-50k | demand_falling | 0.314 | 0.852 | — | 0 | 0 |
| Antipodean Rod (2879) | 5k-15k | demand_stable | 0.789 | 1.279 | 19069.56 | 0 | 0 |
| Sutarn's Ring (13105) | 5k-15k | demand_stable | 0.853 | 2.046 | 15658.24 | 0 | 0 |
| Pillager's Bracers of the Auro (15556) | 5k-15k | demand_stable | 0.855 | 1.219 | 13557.81 | 0 | 0 |
| Hulking Gauntlets (14747) | 5k-15k | demand_stable | 0.841 | 1.305 | 10129.81 | 0 | 0 |
| Enchanted Sea Kelp (4639) | 5k-15k | unknown_falling | — | 0.618 | — | 0 | 0 |
| Outrunner's Pauldrons (15505) | 5k-15k | demand_stable | 0.863 | 0.933 | 4439.53 | 0 | 0 |
| Ritual Amice (14126) | 2k-5k | demand_falling | 0.878 | 0.887 | 859.49 | 0 | 0 |
| Brackwater Vest (3306) | 2k-5k | demand_falling | 0.055 | 0.617 | 3033.11 | 0 | 0 |
| Spiked Chain Cloak of the Fire (15519) | 2k-5k | demand_stable | 0.793 | 1.049 | 3073.9 | 0 | 0 |
| Aboriginal Rod of the Quickbla (15971) | 2k-5k | demand_stable | 0.076 | 0.918 | 2578.0 | 0 | 0 |
| Cross-Stitched Gloves (1783) | 2k-5k | demand_stable | 0.756 | 1.062 | 3070.78 | 0 | 0 |
| Blacksmith Hammer (5956) | 2k-5k | demand_falling | 0.256 | 0.398 | 2187.36 | 0 | 0 |
| Prospector's Mitts (14564) | 2k-5k | demand_falling | 0.776 | 0.829 | 3625.0 | 0 | 0 |
| Sanguine Cuffs (14375) | 2k-5k | demand_stable | 0.837 | 1.002 | 2578.28 | 0 | 0 |
| Short Cutlass (1829) | 2k-5k | demand_falling | 0.492 | 0.877 | 2207.21 | 0 | 0 |
| Outrunner's Slippers of the Au (15498) | 2k-5k | demand_falling | 0.817 | 0.861 | 2382.07 | 0 | 0 |
| Pattern: Deviate Scale Cloak (6474) | 2k-5k | unknown_stable | — | 1.288 | 5541.03 | 0 | 0 |
| Loch Modan Bearskin Rug (242948) | 2k-5k | unknown_stable | — | 0.913 | 3385.4 | 0 | 0 |
| Plans: Silvered Bronze Shoulde (2882) | 2k-5k | unknown_stable | — | 2.994 | 3713.32 | 0 | 0 |
| Shiny Seashell (779) | 2k-5k | commodity | — | 0.001 | — | 0 | 0 |
| Headhunter's Cloak of the Quic (15354) | 2k-5k | demand_falling | 0.073 | 0.856 | 3781.63 | 0 | 0 |
| Pagan Shoes of the Aurora (14159) | 2k-5k | demand_stable | 0.806 | 1.181 | 3400.0 | 0 | 0 |
| Bladefist Axe of the Feverflar (55287) | 2k-5k | no_demand_falling | 0.026 | 0.530 | 1741.19 | 0 | 0 |
| Long Bastard Sword (1830) | 2k-5k | demand_stable | 0.709 | 0.911 | 2175.23 | 0 | 0 |
| Defender Bracers of the Quickb (6574) | 2k-5k | demand_stable | 0.653 | 1.002 | 1770.28 | 0 | 0 |
| Large Flat Tooth (5118) | 2k-5k | commodity | — | 0.537 | — | 0 | 0 |
| Arclight Spanner (6219) | 2k-5k | unknown_falling | — | 0.664 | 15.85 | 0 | 0 |
| Rakzur Club (12983) | 2k-5k | demand_stable | 0.809 | 1.085 | — | 0 | 0 |
| Canvas Gloves (1767) | 2k-5k | demand_falling | 0.735 | 0.838 | 1648.56 | 0 | 0 |
| Prospector's Woolies (14565) | 2k-5k | demand_falling | 0.501 | 0.842 | 1763.67 | 0 | 0 |
| Barbaric Cloth Cloak (4686) | 2k-5k | no_demand_stable | 0.050 | 1.011 | — | 0 | 0 |
| … mais 288 peças em `reports/pricing_groups/membership.csv` | | | | | | | |

## 7. Limitações

- **E2 indisponível**: sem vendas datadas, a clearance da própria conta não pode ser medida contra a parede correcta. É a medida que falta.
- **E1 é um limite inferior**: snipes puxam a média de venda para baixo. Compara-se a banda, não se usa como preço.
- **A amostra DfA é uma amostra**: mede falta de look numa amostra de contas, não intenção de compra. E o número de contas lidas por reino mede a cobertura da amostra, não a população.
- **A amostra de vendas próprias é minúscula** (28 vendas de mog): serve para validar, não para estimar.
- **Sem backtest de lucro**: não há série temporal de vendas por reino, logo não há comparação honesta de estratégias. Nenhum número de rentabilidade é apresentado.
- **Os custos de casa de auction são assumidos** (taxa 5 %, depósito 5 % limitado a 300 % do valor de venda ao fornecedor) e não verificados nesta base de dados.

## 8. O que não foi feito

- Nada foi escrito na base de dados nem no addon.
- Nenhuma operação, grupo ou fonte do TSM foi alterado.
- A string de import proposta **não** foi importada.
- Os coeficientes normais do projecto não foram mudados: a evidência nova está ao lado deles, com a incerteza, para decisão humana.
