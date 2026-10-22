# 92 reinos: o mercado transmitido por inteiro

Dados: `tsm_markets.db`, 92 reinos com preços próprios (eram 50 com dados, 10 com
modelo de vendas), 1 179 338 linhas de preço por reino. Último `updatedAt` da TSM
**19-10-2026 04:36 UTC**, descarregado às 07:02 UTC. Cabaz de referência: os 95 itens
BoE de topo com demanda medida em `reports/item_demand_realms.csv` (amostra DfA de
3 130 contas com score ≥ 600k). Reproduz: `python scripts/realm_48h.py --csv reports/realm_48h.csv`.

## 0. O que mudou com os 92 reinos

- 92 é o número de **casas de leilão** e não de servidores: `tsm_realm_group` tem 267
  slugs ligados em 92 grupos canónicos, um ficheiro por grupo. Não há duplicados.
- Cada reino traz 3 622–18 638 itens com preço (média 12 797).
- Pares (item do cabaz, reino) com listagem: **3 581 de 8 740 = 41 %**.
- Por item: mediana de **30 reinos** com listagem em 92; 2 itens não estão listados em
  reino nenhum (`Deepvine Waistguard`, `Rootspeaker's Barkclasp`); 4 itens em ≤ 3 reinos.
- A mesma peça custa muito diferente de reino para reino: **p90/p10 mediana = 5,3×**,
  p75 = 10,8×, máximo 207×. Como não se move um item entre servidores, isto não é
  arbitragem — é o preço de um mercado fino. Foi por isso que o modelo passou a cortar
  o preço local a 3× a mediana entre reinos (294 preços cortados em ~5 500 pares).

## 1. Ranking (modelo de 48 h, preço cortado a 3× a mediana)

`% dem` = parte da procura EU na amostra; `list` = quantos dos 95 itens o reino tem à
venda; `vazio` = itens com preço no reino mas sem listagem agora; `dump` = listagens
abaixo de 70 % do valor de mercado local; `rec/hist` = preço médio recente / 60 dias do
próprio reino; `vs med` = preço local mediano / mediana entre reinos.

| reino | % dem | list | vazio | dump | rec/hist | vs med | ouro/48h | vendas/48h |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Silvermoon | 6,9 % | 89 | 1 % | 15 % | 0,98 | 0,69× | 4 548 | 0,084 |
| Draenor | 3,7 % | 68 | 22 % | 13 % | 1,00 | 0,80× | 3 265 | 0,048 |
| Kazzak | 3,0 % | 82 | 8 % | 10 % | 0,98 | 1,00× | 3 188 | 0,039 |
| Antonidas | 2,7 % | 48 | 33 % | 6 % | 1,07 | 0,99× | 3 064 | 0,035 |
| Ravencrest | 2,4 % | 80 | 8 % | 16 % | 1,07 | 1,00× | 3 023 | 0,029 |
| Twisting Nether | 2,3 % | 67 | 25 % | 16 % | 1,00 | 0,83× | 2 373 | 0,029 |
| Hyjal | 2,6 % | 58 | 23 % | 12 % | 0,97 | 1,00× | 2 248 | 0,034 |
| Gordunni | 1,9 % | 26 | 61 % | 27 % | 0,98 | 0,96× | 2 247 | 0,031 |
| Howling Fjord | 2,0 % | 17 | 75 % | 6 % | 0,93 | 1,00× | 2 231 | 0,033 |
| Blackmoore | 2,0 % | 74 | 13 % | 22 % | 1,00 | 0,92× | 2 005 | 0,024 |
| Soulflayer | 0,9 % | 19 | 37 % | 11 % | 1,00 | 1,56× | 1 903 | 0,015 |
| Pozzo dell'Eternità | 0,7 % | 19 | 66 % | 21 % | 1,00 | 1,00× | 1 899 | 0,012 |
| Argent Dawn | 2,2 % | 78 | 15 % | 18 % | 0,96 | 0,80× | 1 875 | 0,029 |
| Auchindoun | 1,5 % | 50 | 37 % | 10 % | 1,00 | 0,92× | 1 841 | 0,021 |

Top 5 = 23 % do ouro modelado nos 92 reinos. Tabela completa em
`reports/realm_48h.csv`.

## 2. O que decide

Correlações nos 78 reinos que têm amostra de demanda:

| par | r |
|---|---:|
| ouro/48h × % da demanda | **+0,93** |
| ouro/48h × itens listados | +0,43 |
| ouro/48h × preço local vs mediana | −0,09 |
| itens listados × preço local vs mediana | −0,59 |
| dumping × itens listados | +0,23 |

A procura manda, e nada mais chega perto. O preço *médio* do reino é irrelevante para o
resultado (+/−0,09): o que entra no modelo é o preço do item que efetivamente vende, não
o nível geral. E a segunda linha confirma o mecanismo já visto com 50 reinos: quanto
maior o livro, mais barato ele é (−0,59), ou seja, o próprio sucesso atrai undercut.

Consequência prática: **não há reino "caro mas vazio" que compense**. Os casos que
parecem bons por preço são fantasmas:

- **Galakrond** — 3 dos 95 itens listados, preço 2,50× a mediana, 0,006 vendas/48h.
  Os 1 408 g/48h saem todos do modelo a preço de tabela que ninguém está a pagar.
- **Soulflayer / Goldrinn / Chamber of Aspects / Deathguard** — mesma coisa entre 1,5×
  e 2,5× a mediana com 19 ou menos itens listados.

O contrário também é verdade: **Silvermoon e Draenor são ao mesmo tempo os mais líquidos
e os mais baratos** (0,69× e 0,80×). Num mercado que não permite transportar mercadoria,
ser barato é sinal de que o stock está lá e sai — não de que se ganha menos por venda.

## 3. Onde há espaço real: itens ausentes num reino que compra

Itens do cabaz com procura medida, sem listagem no reino (N = 0), e o preço de referência
(mediana entre reinos, cortada a 3×):

| reino | itens sem listagem | melhor oportunidade | ref. |
|---|---:|---|---:|
| Silvermoon | 1 | Kyros's Spare Cuffs | 50 000 g |
| Draenor | 12 | Aevery's Spare Brigandine | 595 296 g |
| Kazzak | 7 | Aevery's Spare Brigandine | 595 296 g |
| Antonidas | 16 | Kyros's Spare Handguards | 198 000 g |

Silvermoon já tem quase tudo — é o reino onde "falta" é raro. Draenor e Antonidas são os
que têm mais buracos no cabaz, e são também os que têm procura acima da mediana.

## 4. A aritmética honesta

O cabaz completo, nos 95 itens, num reino de topo:

- Silvermoon: **0,084 vendas/48h** = 1 venda a cada 24 dias; 4 548 g/48h esperados.
- Os 92 reinos somados dão **0,79 vendas/48h** (≈ 0,4/dia) contra λ regional de
  1,16/dia. A diferença é o que a listagem que já existe come: metade do fluxo.
- Para sustentar **1 venda/dia** com este cabaz seria preciso ~2,5× o catálogo
  (~240 listagens), e é isso que o "posta 48h e esquece" tem de significar: não 48 h de
  paciência, mas um catálogo grande o suficiente para haver sempre algo a cair.

O ouro/48h de Silvermoon (4 548 g) é o valor esperado de ter os 95 itens do cabaz à venda
ao mesmo tempo — não o de uma sessão, nem o de 5 listagens. E só 61 dos 95 itens têm
vendas medidas; os outros 34 entram no modelo a zero porque a TSM não registou uma única
venda deles na região. Num inventário tirado ao acaso, a expectativa é menor que esta.

## 5. Limitações

- **14 reinos não têm amostra de demanda** (Arak-arahm, Nefarian, Kor'gall, Scarshield
  Legion, Shattered Halls, Nazjatar, Confrérie du Thorium, Echsenkessel, Naxxramas,
  Todeswache, Genjuros, Emeriss, Karazhan, Deathguard). Aparecem com 0 no modelo, mas
  têm 12–71 itens do cabaz listados. É ausência de comprador *na amostra*, não reino morto.
- `sold_per_day` e `sale_rate` só existem por região; a conversão por reino usa a partilha
  de wanters, não vendas observadas. Para itens raros a TSM mede um piso, não o valor exato.
- `min_buyout = 0` significa "sem listagem", não "ninguém quer". Não há contagem de
  anúncios por item, por isso N é 0 ou 1 e nunca 3.
- O corte a 3× a mediana é uma escolha: sem ele, um único anúncio parado elegia Kazzak
  como o melhor reino da EU. O caso concreto: `Kyros's Spare Chestplate`, valor de
  mercado de 3,46 M g em Kazzak, quando 51 dos 64 reinos que a têm a põem entre 100 k e
  300 k (mediana ~198 k). O preço local não é escassez — é um anúncio que ninguém toca.
- A amostra DfA (3 130 contas, score ≥ 600k) não é a população do reino.
