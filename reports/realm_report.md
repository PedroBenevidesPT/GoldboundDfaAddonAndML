# Relatório — coleccionadores e mercados de transmog

**Gerado:** 2026-10-18 por `python -m woweconomy.analytics.dfa_report`
**Fontes:** `C:\economics\woweconomy\dfa\dfa.db` e `tsm_markets.db`

## Sumário

1. À mesma profundidade (o 100.º melhor), **Silvermoon** vale 869 304 e o último reino comparável, **Haomarush**, 134 356 — 6,47×.
2. A amostra (`realms --sample`) tem 7301 contas; Stormreaver domina com 2315 e média 260 916, mas a média mais alta é Ahn'Qiraj (1 110 839) com 3 contas: a média mede a profundidade lida.
3. O cabaz de 1971 transmogs comparáveis pede entre 0,32× em **Argent Dawn** e 1,89× em **Shattered Halls** a mediana entre mercados.
4. O mercado mais fundo é **Argent Dawn** (88,5% dos itens com 3+ listagens); o mais fino é **Pozzo dell'Eternità** (5,9%), e é onde os pedidos são mais altos ou mais irreais.
5. O prémio sobrevive ao filtro de liquidez: **Aggra (Português)** 1,82× e **Shattered Halls** 1,89× com 3+ listagens. É pedido, não venda: o TSM não tem velocidade por reino.

## Dados usados

| fonte | o que tem | idade |
|---|---|---|
| `dfa.db` | 7301 contas; escaneado: appearances 7300, mounts 7304, recipes 7302; boards: `eu/connected-dragonmaw` 3125, `eu` 2773, `eu/connected-auchindoun` 1425, `eu/connected-silvermoon` 200 | scan pode estar a correr |
| `tsm_markets.db` | 188 477 linhas, 31 083 itens, 11 nomes de reino | scan de 2026-10-16 21:36 UTC |
| demanda | 14755 aparições com valor ≥ 70; 2810 com preço; 1971 comparáveis nos 8 mercados | gerada do `dfa.db` |

Fora do cabaz: 20329 BoP (nunca vão ao leilão), 2140 era-Classic (preço de outro jogo), 3244 sem preço TSM em reino nenhum, 8701 sem item BoE no DB2.

## Porque a amostra engana

| reino | contas | média | @N | ≥ corte | máximo | lido até |
|---|---:|---:|---:|---:|---:|---:|
| Stormreaver | 2315 | 260 916 | 567 758 | 3 | 1 127 707 | 3 125 |
| Sylvanas | 1067 | 468 708 | 668 236 | 7 | 1 126 856 | 2 684 |
| Dragonmaw | 239 | 270 763 | 254 668 | 0 | 779 425 | 3 115 |
| Vashj | 238 | 303 409 | 283 632 | 1 | 1 152 515 | 3 099 |
| Auchindoun | 203 | 485 293 | 442 267 | 2 | 1 126 679 | 2 163 |
| Spinebreaker | 194 | 261 632 | 219 921 | 0 | 953 784 | 3 123 |

`@N` é o score do 100.º melhor daquele reino na própria escada; a média é sobre *tudo o que foi lido*, e por isso cai quando o scan desce a escada (`lido até` é o rank mais fundo que aquela escada recebeu). Reinos sem escada local aparecem com `—`: as suas linhas vêm da escada regional, onde o rank é a posição na região.

## Qualidade a profundidade fixa (`--depth 100`)

| # | reino | escada | chars | @N | média | ≥ corte | % | máximo |
|---:|---|---|---:|---:|---:|---:|---:|---:|
| 1 | Silvermoon | connected-silvermoon | 200 | **869 304** | 900 549 | 32 | 16,0% | 1 195 016 |
| 2 | Sylvanas | connected-auchindoun | 1087 | **668 236** | 466 039 | 7 | 0,6% | 1 126 856 |
| 3 | Stormreaver | connected-dragonmaw | 2328 | **567 758** | 259 999 | 3 | 0,1% | 1 127 707 |
| 4 | Auchindoun | connected-auchindoun | 207 | **442 267** | 482 172 | 2 | 1,0% | 1 126 679 |
| 5 | Vashj | connected-dragonmaw | 238 | **283 632** | 303 409 | 1 | 0,4% | 1 152 515 |
| 6 | Dragonmaw | connected-dragonmaw | 241 | **254 668** | 269 279 | 0 | 0,0% | 779 425 |
| 7 | Spinebreaker | connected-dragonmaw | 196 | **219 921** | 260 209 | 0 | 0,0% | 953 784 |
| 8 | Haomarush | connected-dragonmaw | 122 | **134 356** | 262 218 | 0 | 0,0% | 839 028 |

| reino | escada | chars | média | máximo |
|---|---|---:|---:|---:|
| Dunemaul | connected-auchindoun | 94 | 460 937 | 950 677 |
| Jaedenar | connected-auchindoun | 37 | 509 300 | 1 061 334 |

Sem 100.º lugar nestes reinos: ficam fora do ranking para não comparar o 8.º com o 100.º.

Escada regional (contagem, não profundidade): Kazzak 33, Silvermoon 32, Ravencrest 30, Draenor 30, Hyjal 25, Twisting Nether 24, Tarren Mill 24, Blackhand 23.

## Mercado por mercado (cabaz comum de 1971 itens)

| mercado | itens | cobertura | preço mediano | vs mediana | leilões/item | cortes | com 3+ listagens | prémio (3+) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Aggra (Português) | 1905 | 96,7% | 12 576g | **1,83×** | 2 | 867 (45,5%) | 30,0% | 1,82× |
| Argent Dawn | 1971 | 100,0% | 2 155g | **0,32×** | 8 | 63 (3,2%) | 88,5% | 0,30× |
| Draenor | 1971 | 100,0% | 2 524g | **0,44×** | 4 | 65 (3,3%) | 74,4% | 0,39× |
| Dragonmaw +3 | 1813 | 92,0% | 12 500g | **1,87×** | 1 | 873 (48,2%) | 10,8% | 1,68× |
| Pozzo dell'Eternità | 1510 | 76,6% | 13 927g | **1,78×** | 0 | 683 (45,2%) | 5,9% | 1,81× |
| Shattered Halls | 1837 | 93,2% | 12 829g | **1,89×** | 1 | 883 (48,1%) | 9,7% | 1,54× |
| Silvermoon | 1971 | 100,0% | 2 501g | **0,42×** | 7 | 47 (2,4%) | 87,9% | 0,39× |
| Twisting Nether | 1971 | 100,0% | 5 573g | **0,97×** | 3 | 168 (8,5%) | 67,0% | 0,86× |

Preço cortado a 2× a mediana entre mercados; reinos ligados aparecem como um mercado só (mesmo leilão no TSM). `cortes` = pedidos acima desse teto; `com 3+ listagens` = itens com prateleira a sério.

## Leitura mecânica

| mercado | @N (profundidade) | prémio | liquidez | leitura |
|---|---:|---:|---:|---|
| Aggra (Português) | — | 1,83× | 30,0% | fino e caro: pouca prateleira, pedidos altos -- margem com paciência |
| Argent Dawn | — | 0,32× | 88,5% | fundo e barato: rotação, margem baixa |
| Draenor | — | 0,44× | 74,4% | fundo e barato: rotação, margem baixa |
| Dragonmaw +3 | 567 758 | 1,87× | 10,8% | fino e caro, mas com coleccionadores: margem com paciência, só no que tem valor alto |
| Pozzo dell'Eternità | — | 1,78× | 5,9% | fino e caro: pouca prateleira, pedidos altos -- margem com paciência |
| Shattered Halls | — | 1,89× | 9,7% | fino e caro: pouca prateleira, pedidos altos -- margem com paciência |
| Silvermoon | 869 304 | 0,42× | 87,9% | fundo, barato e com compradores: rotação |
| Twisting Nether | — | 0,97× | 67,0% | fundo e barato: rotação, margem baixa |

## Onde pôr o quê (coleccionadores × mercado)

| mercado | coleccionadores | mercado (liquidez) | leitura de uma linha |
|---|---|---|---|
| **Aggra (Português)** | só elite regional (2 ≥ 1M) | 30,0% com 3+ listagens, 1,83× | fino e caro: pouca prateleira, pedidos altos -- margem com paciência |
| **Argent Dawn** | só elite regional (12 ≥ 1M) | 88,5% com 3+ listagens, 0,32× | fundo e barato: rotação, margem baixa |
| **Draenor** | só elite regional (30 ≥ 1M) | 74,4% com 3+ listagens, 0,44× | fundo e barato: rotação, margem baixa |
| **Dragonmaw +3** | 567 758 @100, 3 ≥ 1M | 10,8% com 3+ listagens, 1,87× | fino e caro, mas com coleccionadores: margem com paciência, só no que tem valor alto |
| **Pozzo dell'Eternità** | só elite regional (8 ≥ 1M) | 5,9% com 3+ listagens, 1,78× | fino e caro: pouca prateleira, pedidos altos -- margem com paciência |
| **Shattered Halls** | sem escada nenhuma | 9,7% com 3+ listagens, 1,89× | fino e caro: pouca prateleira, pedidos altos -- margem com paciência |
| **Silvermoon** | 869 304 @100, 32 ≥ 1M | 87,9% com 3+ listagens, 0,42× | fundo, barato e com compradores: rotação |
| **Twisting Nether** | só elite regional (24 ≥ 1M) | 67,0% com 3+ listagens, 0,97× | fundo e barato: rotação, margem baixa |

**Recomendação operacional**

1. **Rotação:** listar em **Silvermoon** ao preço da mediana (0,42×), aceitando a margem baixa. É onde o item tem mais probabilidade de encontrar comprador: 87,9% dos itens têm 3+ listagens e o 100.º melhor coleccionador vale 869 304.
2. **Margem:** no teu reino (Dragonmaw, do diário) o mercado pede 1,87× com 10,8% de prateleira. É onde há menos gente a competir: manter aqui só o que tem valor alto e baixa frequência, com duração longa.
3. **Teste controlado:** Aggra (Português) é o único mercado intermédio com preço e volume na mesma frase (1,83× com 30,0% de prateleira). Um lote de 10–20 itens mede se o prémio é alcançável antes de se apostar nele.
4. **Não ler 1,89× (Shattered Halls) como «vende-se mais caro lá».** Significa «pede-se mais»: a prova de venda continua a ser `saleRate`/`soldPerDay` (regional, não por reino) e o diário.

## Quem quer (dos bitmaps de colecção)

Contas com colecção guardada: 7301 em 251 reinos. Uma conta "quer" um item quando o bit da aparência está a 0; contados só os 1971 itens do cabaz com preço.

**Aggra (Português)** — 10 contas

| item | querem | % | preço no mercado |
|---|---:|---:|---:|
| Skullflame Shield | 10 | 100,0% | 1 003 465g |
| Blackrock Champion's Axe | 10 | 100,0% | 60 743g |
| Shadowfang | 10 | 100,0% | 699 000g |
| Pysan's Old Greatsword | 10 | 100,0% | 56 616g |
| Blade of Hanna | 10 | 100,0% | 981 914g |
| Scarlet Initiate Robes | 10 | 100,0% | 464 442g |
| Onyx Claymore | 10 | 100,0% | 50 193g |
| Seafarer's Pantaloons | 10 | 100,0% | 39 677g |
| Vicar's Robe | 10 | 100,0% | 401 000g |
| Solliden's Trousers | 10 | 100,0% | 16 500g |

**Argent Dawn** — 62 contas

| item | querem | % | preço no mercado |
|---|---:|---:|---:|
| Wildercloth Fishing Cap | 62 | 100,0% | 625g |
| Runic Hammer | 60 | 96,8% | 39 610g |
| Khorium Champion | 59 | 95,2% | 49 743g |
| Gauntlets of the Unrelenting | 58 | 93,5% | 98 323g |
| Weyrnkeeper's Timeless Treads | 58 | 93,5% | 15 000g |
| Weyrnkeeper's Timeless Treads | 58 | 93,5% | 15 000g |
| The Plague Belcher | 57 | 91,9% | 11 356g |
| The Final Dream | 57 | 91,9% | 21 374g |
| Death's Gamble | 57 | 91,9% | 19 980g |
| Whitemend Hood | 57 | 91,9% | 15 699g |

**Draenor** — 108 contas

| item | querem | % | preço no mercado |
|---|---:|---:|---:|
| Wildercloth Fishing Cap | 108 | 100,0% | 130g |
| Shadowfang | 101 | 93,5% | 179 999g |
| Formal Dangui | 101 | 93,5% | 96 031g |
| Runic Hammer | 101 | 93,5% | 14 995g |
| Shroud of Forbidden Magic | 101 | 93,5% | 40 000g |
| Gauntlets of the Unrelenting | 100 | 92,6% | 15 000g |
| The Glazer | 100 | 92,6% | 250 000g |
| The Plague Belcher | 99 | 91,7% | 21 587g |
| The Face of Doom | 99 | 91,7% | 6 667g |
| Death's Gamble | 99 | 91,7% | 12 222g |

**Dragonmaw +3** — 2870 contas

| item | querem | % | preço no mercado |
|---|---:|---:|---:|
| Animist's Legguards | 2870 | 100,0% | 42 561g |
| Formal Dangui | 2869 | 100,0% | 1 500 000g |
| Animist's Footwraps | 2869 | 100,0% | 138 931g |
| Sanctified Leather Hat | 2869 | 100,0% | 117 584g |
| Midnight's Graze | 2869 | 100,0% | 35 000g |
| Blade of Unholy Might | 2869 | 100,0% | 59 000g |
| Bracers of Vengeance | 2869 | 100,0% | — |
| Bindings of the Harvested Soul | 2869 | 100,0% | 243 111g |
| Encased Frigid Heart | 2869 | 100,0% | — |
| Gauntlets of the Unrelenting | 2869 | 100,0% | 86 274g |

**Pozzo dell'Eternità** — 21 contas

| item | querem | % | preço no mercado |
|---|---:|---:|---:|
| The Glazer | 21 | 100,0% | 599 000g |
| Wildercloth Fishing Cap | 21 | 100,0% | 251g |
| Venom Stalker's Strap | 21 | 100,0% | 5 000g |
| Varruth's Guillotine | 20 | 95,2% | 640 654g |
| Prime Slime Slippers | 20 | 95,2% | 13 055g |
| Web Acolyte's Hood | 20 | 95,2% | 115 000g |
| Captured Earthen's Ironhorns | 20 | 95,2% | 187 000g |
| Shadowfang | 19 | 90,5% | 699 000g |
| Ancestral Boots | 19 | 90,5% | 107 096g |
| Steelscale Crushfish | 19 | 90,5% | — |

**Shattered Halls** — 2 contas

| item | querem | % | preço no mercado |
|---|---:|---:|---:|
| Footpad's Pants | 2 | 100,0% | 250g |
| Destiny | 2 | 100,0% | 85 000g |
| Shortsword of Vengeance | 2 | 100,0% | 1 501g |
| Vendetta | 2 | 100,0% | 84 828g |
| Stone Gnoll Hammer | 2 | 100,0% | 5 000g |
| Riverpaw Leather Vest | 2 | 100,0% | 15 410g |
| Dazzling Longsword | 2 | 100,0% | — |
| Black Metal Axe | 2 | 100,0% | 28 437g |
| Black Metal Shortsword | 2 | 100,0% | 14 298g |
| Venom Web Fang | 2 | 100,0% | 44 635g |

**Silvermoon** — 193 contas

| item | querem | % | preço no mercado |
|---|---:|---:|---:|
| Wildercloth Fishing Cap | 193 | 100,0% | 100g |
| Blue Dragonscale Shoulders | 182 | 94,3% | 19 975g |
| The Glazer | 179 | 92,7% | 598 980g |
| Weyrnkeeper's Timeless Treads | 179 | 92,7% | 49 900g |
| Vicar's Robe | 178 | 92,2% | 34 747g |
| Netherdrake Helm | 178 | 92,2% | 14 975g |
| Broken Wine Bottle | 177 | 91,7% | 104 380g |
| Formal Dangui | 177 | 91,7% | 105 001g |
| Devout Ashdevil's Fanged Sash | 177 | 91,7% | 44 998g |
| Death's Gamble | 175 | 90,7% | 14 975g |

**Twisting Nether** — 79 contas

| item | querem | % | preço no mercado |
|---|---:|---:|---:|
| Wildercloth Fishing Cap | 79 | 100,0% | 1 989g |
| Runic Hammer | 72 | 91,1% | 480 000g |
| The Glazer | 72 | 91,1% | 598 672g |
| Khorium Champion | 71 | 89,9% | 39 989g |
| Blue Dragonscale Shoulders | 69 | 87,3% | 68 313g |
| Battlecast Hood | 69 | 87,3% | 25 828g |
| Flamescale Belt | 69 | 87,3% | 9 015g |
| Iceguard Breastplate | 69 | 87,3% | 45 034g |
| Extreme-Impact Hole Puncher | 69 | 87,3% | 250 000g |
| Broken Wine Bottle | 68 | 86,1% | 134 001g |

## O que falta para decidir

- **Demanda por reino:** `missing_realm` tem 7230368 linhas; 4802 wishlists marcadas `by_realm`. Preencher com `dataforazeroth reweight --realm <reino>`.
- **Preço TSM:** 144 reinos com 5+ contas na amostra não têm preço: Aegwynn, Aerie Peak, Aggramar, Alexstrasza, Alleria, Aman'Thul, Ambossar, Anachronos….
- **Velocidade:** não existe por reino no TSM (é regional). Só vendas reais a resolveriam; o diário tem 92 itens, 204 unidades vendidas e 259 falhas.
- **Profundidade dos reinos só-regionais:** `leaderboard --realm <reino> --max-rank 100`, um pedido por reino, para os trazer à tabela comparável.

## Reproduzir

```bat
cd C:\FarmAnalysis
python -m woweconomy.analytics.dfa_report
python -m woweconomy.analytics.dfa_report --min-score 70 --depth 100 --out reports\realm_report.md
```

Os números de `dfa.db` mexem-se enquanto o scan corre; os de preço ficam parados até ao próximo scan do TSM.
