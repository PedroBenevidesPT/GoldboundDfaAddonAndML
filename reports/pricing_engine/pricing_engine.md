# DFA — motor de preço baseado em evidência

Gerado por `python scripts/pricing_engine.py`. Alvo: **Dragonmaw** (328 itens). É análise: **nada foi importado no TSM**, nenhuma operação foi tocada e os ficheiros de proposta continuam intactos.

## 0. O principio

**Os anuncios mostram o que os vendedores querem receber; as transaccoes mostram o que alguem pagou de facto.** As duas coisas nao se medem da mesma maneira, por isso nao sao misturadas: primeiro estima-se o valor de transaccao a partir das vendas observadas, e depois pergunta-se ate que ponto as referencias de mercado sao compativeis com esse valor.

## 1. As fontes: origem, significado, data, cobertura, limitacoes

| campo | origem | significado | data | cobertura | limitacoes |
|---|---|---|---|---|---|
| `dbmarket` | `tsm_realm_price.market_value` | ask medio do reino, decays | `fetched_at` (19/10) | 176/328 itens | ask, nao transaccao; medido **3,9x acima** do preco realizado |
| `dbrecent` | `tsm_realm_price.recent` | ask dos ultimos 3 dias | idem | idem | correlacionado com `dbmarket` (mesma familia) |
| `dbhistorical` | `tsm_realm_price.historical` | ask de 60 dias do reino | idem | idem | correlacionado; pode estar atrasado |
| `dbminbuyout` | `tsm_realm_price.min_buyout` | o anuncio mais barato | idem | 114/328 com parede | **um** ask, nao o mercado |
| `dbregionmarketavg` | `tsm_region_price.market_value` | ask medio da regiao | snapshot de 18/10 | 31 184 itens | ask; 1,25x o preco realizado |
| `dbregionhistorical` | `tsm_region_price.historical` | ask de 60 dias da regiao | idem | idem | correlacionado com o anterior |
| `dbregionsaleavg` | `tsm_region_price.avg_sale_price` | **media do que se pagou** na regiao | idem | 25 128 itens com venda | a TSM **nao expoe a amostra**; sujeita a snipes |
| `dbregionsoldperday` | `tsm_region_price.sold_per_day` | estimativa de volume regional | idem | idem | **volume, nao preco**: nunca multiplica o preco |
| `dbregionsalerate` | `tsm_region_price.sale_rate` | frequencia de vendas | idem | idem | a definicao e da TSM |
| concorrencia | `tsm_market.num_auctions` | anuncios concorrentes | descarga AuctionDB | **12 reinos** | nao diz vendas futuras |
| procura | `dfa.db :: missing` | contas da amostra que ainda nao tem o look | leitura actual | 20 436 contas | procura **potencial**, nao intencao de compra |
| vendas proprias | `journal_stats` | as nossas transaccoes | **sem data** | 6 322 vendas | agregado: sem data e sem preco pedido |
| arquivo bruto | `Journalator.lua` (SavedVariables) | eventos por sessao | timestamps | 9 sessoes, 4 dias | comprimido (LibDeflate): nao descodificavel aqui |

## 2. As tres saidas

| | o que é |
|---|---|
| **A — valor realizável estimado** | a média aritmética ponderada pela qualidade da evidência, com intervalo, confiança e a proveniência de cada fonte |
| **B — avaliação de liquidez** | procura, velocidade, concorrência e o sell-through **observado** da conta; separado do preço |
| **C — preço de listagem recomendado** | derivado de A e B, com a razão de qualquer desvio e os custos marcados como assumidos |

- 317 de 328 itens com estimativa utilizável; 11 sem evidência suficiente (`INSUFFICIENT_EVIDENCE`).

## 3. Os pesos: medidos, nao escolhidos

Calibrados contra as vendas reais da conta (177 itens, medido em 2026-10-19T14:28:23Z):

| familia | cobertura | erro mediano (log) | peso medido |
|---|---:|---:|---:|
| local_ask | 16% | 1.350 | 0.425 |
| regional_ask | 100% | 0.286 | 0.777 |
| regional_sales | 100% | 0.280 | 0.781 |

Ver `calibration.md` para o metodo. **As referencias de ask recebem um peso medido, nao assumido** -- e o que a medicao mostra e que o ask local e uma previsao fraca do preco realizado.

## 4. As fontes e o que valem

| fonte | tipo | âmbito | família | peso | porquê |
|---|---|---|---|---|---|
| `dbmarket` | referência de mercado | local | ask local | 1,00 | o nível do mercado em que se vende |
| `dbrecent` | ask | local | ask local | (com `dbmarket`) | 3 dias: correlacionado, não é evidência independente |
| `dbhistorical` | ask | local | ask local | (com `dbmarket`) | 60 dias: correlacionado |
| `dbminbuyout` | ask | local | ask local | (com `dbmarket`) | o anúncio mais barato: um ask, não o mercado |
| `dbregionmarketavg` | referência de mercado | regional | ask regional | 0,70 | aferido, mas mais largo que o mercado onde vendemos |
| `dbregionhistorical` | ask | regional | ask regional | (com o anterior) | correlacionado |
| `dbregionsaleavg` | **transacção** | regional | vendas regionais | 1,00 | o que alguém pagou — é a grandeza que se quer estimar |
| venda própria | **transacção** | local | vendas próprias | 1,30 | a transacção mais relevante, com amostra mínima |

As quatro fontes locais de ask **não são quatro observações**: são uma família, reduzida a um valor (mediana) com a sua dispersão interna. A mesma coisa na família regional. Só as vendas são uma família independente, porque medem outra coisa.

O factor de clearance (média de venda ÷ nível de ask) é **medido** no catálogo, por banda:

| banda | factor | n |
|---|---:|---:|
| 100-500 | 0.199 | 3,994 |
| 150k-500k | 0.027 | 517 |
| 15k-50k | 0.046 | 2,950 |
| 2k-5k | 0.064 | 2,365 |
| 500-2k | 0.101 | 4,497 |
| 50k-150k | 0.037 | 1,538 |
| 5k-15k | 0.056 | 2,919 |
| <100 | 0.806 | 6,223 |
| >500k | 0.008 | 195 |

## 3. Itens: valor, liquidez, listagem

| item | banda | valor estimado | intervalo | conf. | liquidez | listagem | razão |
|---|---|---:|---|---:|---|---:|---|
| Slippers of the Hissing Cu (271435) | 15k-50k | 45,001 g | 34,588–55,414 g | 35% | média | 41,626 g | preço ancorado em transacções observadas; liquidez média: 8% |
| Pagan Vest of the Quickbla (14158) | 50k-150k | 1,422 g | 711–2,133 g | 10% | baixa | 0 g | INSUFFICIENT_EVIDENCE |
| Loch Modan Bearskin Rug (242948) | 2k-5k | 1,046 g | 523–1,569 g | 17% | baixa | 0 g | INSUFFICIENT_EVIDENCE |
| Evocator's Blade (2567) | 15k-50k | 950 g | 475–1,426 g | 10% | baixa | 0 g | INSUFFICIENT_EVIDENCE |
| Mystic's Robe (14371) | 15k-50k | 585 g | 292–877 g | 10% | baixa | 0 g | INSUFFICIENT_EVIDENCE |
| Tarnished Dawnlit Corsair' (258921) | 100-500 | 570 g | 410–730 g | 37% | média | 527 g | preço ancorado em transacções observadas; liquidez média: 8% |
| Enchanted Sea Kelp (4639) | 5k-15k | 519 g | 493–545 g | 12% | desconhecida | 0 g | INSUFFICIENT_EVIDENCE |
| Tarnished Dawnlit Sentinel (258934) | 100-500 | 475 g | 281–669 g | 28% | média | 0 g | INSUFFICIENT_EVIDENCE |
| Prospector's Mitts (14564) | 5k-15k | 425 g | 213–638 g | 10% | baixa | 0 g | INSUFFICIENT_EVIDENCE |
| Wrangler's Buckler of the  (15332) | 15k-50k | 408 g | 204–612 g | 10% | baixa | 0 g | INSUFFICIENT_EVIDENCE |
| Prospector's Buckler (15893) | 500-2k | 367 g | 183–550 g | 10% | baixa | 0 g | INSUFFICIENT_EVIDENCE |
| Prospector's Woolies (14565) | 2k-5k | 329 g | 164–493 g | 10% | baixa | 0 g | INSUFFICIENT_EVIDENCE |
| Unknown (7092) | 2k-5k | 300 g | 150–450 g | 10% | desconhecida | 0 g | INSUFFICIENT_EVIDENCE |
| Unknown (13105) | 5k-15k | 292 g | 146–439 g | 11% | baixa | 0 g | INSUFFICIENT_EVIDENCE |
| Beazel's Basher (13024) | 2k-5k | 279 g | 140–419 g | 11% | média | 0 g | INSUFFICIENT_EVIDENCE |
| Pillager's Bracers of the  (15556) | 5k-15k | 276 g | 138–414 g | 11% | baixa | 0 g | INSUFFICIENT_EVIDENCE |
| Unknown (15118) | 15k-50k | 265 g | 133–398 g | 10% | baixa | 0 g | INSUFFICIENT_EVIDENCE |
| Bristlebark Blouse (14570) | 15k-50k | 263 g | 132–395 g | 10% | baixa | 0 g | INSUFFICIENT_EVIDENCE |
| Barbaric Cloth Cloak (4686) | 2k-5k | 250 g | 125–375 g | 10% | desconhecida | 0 g | INSUFFICIENT_EVIDENCE |
| Plans: Mirrored Boots (69969) | 5k-15k | 250 g | 125–375 g | 10% | desconhecida | 0 g | INSUFFICIENT_EVIDENCE |
| Unknown (3612) | 2k-5k | 250 g | 125–375 g | 10% | desconhecida | 0 g | INSUFFICIENT_EVIDENCE |
| Pagan Britches of the Fire (14165) | 2k-5k | 249 g | 125–374 g | 10% | baixa | 0 g | INSUFFICIENT_EVIDENCE |
| Rakzur Club (12983) | 2k-5k | 238 g | 119–357 g | 10% | baixa | 0 g | INSUFFICIENT_EVIDENCE |
| Rough Wooden Staff (1515) | 100-500 | 238 g | 119–356 g | 19% | baixa | 0 g | INSUFFICIENT_EVIDENCE |
| Antipodean Rod (2879) | 2k-5k | 212 g | 106–318 g | 11% | média | 0 g | INSUFFICIENT_EVIDENCE |
| Haranir Phial of Perceptio (241316) | 100-500 | 208 g | 187–229 g | 38% | baixa | 192 g | preço ancorado em transacções observadas; liquidez baixa: 8% |
| Grunt's Shield of the Auro (15512) | 5k-15k | 204 g | 102–306 g | 10% | baixa | 0 g | INSUFFICIENT_EVIDENCE |
| Ritual Gloves of the Firef (14124) | 500-2k | 203 g | 102–305 g | 10% | baixa | 0 g | INSUFFICIENT_EVIDENCE |
| Darkmoon Firewater (124671) | 100-500 | 200 g | 190–211 g | 40% | baixa | 185 g | preço ancorado em transacções observadas; liquidez baixa: 8% |
| Outrunner's Pauldrons (15505) | 5k-15k | 188 g | 94–282 g | 10% | baixa | 0 g | INSUFFICIENT_EVIDENCE |

## 4. A proveniência, item a item

O CSV `item_estimates.csv` traz, por item: o valor estimado, o intervalo, a confiança, **as fontes que contribuíram com o peso efectivo**, as fontes **rejeitadas com o motivo**, os sinais de liquidez, o preço recomendado e o motivo. Nada é uma caixa-preta: cada número aponta para a fonte que o produziu.

## 5. Limitações

- **Não há vendas datadas.** O `journal_stats` tem contagens e valores agregados, sem datas; o arquivo bruto do Journalator é comprimido (LibDeflate) e cobre só os últimos dias. Não há forma de reconstruir quando uma venda aconteceu, nem o preço pedido no momento da publicação.
- **A média de venda regional não expõe a amostra.** A TSM não diz quantas vendas entram nessa média, por isso o motor **não inventa** uma amostra: usa o volume (`sold_per_day`) e a discordância como sinais de qualidade, e trata a média como uma transacção (peso alto) com confiança limitada.
- **A média de venda baixa é evidência, e fica.** Quando ela é muito abaixo do ask, o factor de clearance medido reduz o ask e a estimativa desce; a confiança desce se a causa não for estabelecida. O preço não é descartado por ser pouco apelativo.
- **O volume é regional.** A cobertura de stock usa o volume regional e é sinal de escala, não promessa de venda local.
- **Os custos são assumidos** (taxa 5 %, depósito 5 % limitado a 300 % do vendor). O resultado líquido é marcado como tal.
