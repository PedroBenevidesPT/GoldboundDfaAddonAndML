# Relatório — coleccionadores e mercados de transmog

**Data:** 2026-10-18 · **Fontes:** `woweconomy/dfa/dfa.db` (scan DfA) e `tsm_markets.db` (TSM)
**Estado:** scan do DfA ainda a correr — os números de contas sobem ao longo do dia.

---

## Sumário em cinco linhas

1. A tabela do `realms --sample` **não ordena reinos por qualidade**: ordena-os por quanto
   o scan leu de cada um. Stormreaver (1716 contas, média 307k) e Kazzak (88 contas, média
   978k) têm o **mesmo máximo** (~1.1–1.2M); a média só difere porque um foi lido até ao
   rank 2330 e o outro apenas no topo.
2. Comparados à mesma profundidade (o 100.º melhor coleccionador do reino), a Silvermoon
   vale **869k** e o grupo Stormreaver **568k** — 1,5×, e 32 contra 3 contas acima de 1M.
   Era isto que a média escondia.
3. Só há preços TSM para **8 mercados** (11 nomes, 4 dos quais são o mesmo leilão). Neles,
   o cabaz de 1721 transmogs em falta (valor DfA ≥ 70) pede entre **0,31×** (Argent Dawn) e
   **1,91×** (Shattered Halls) a mediana entre mercados.
4. Os mercados que pedem mais são os **mais finos**: em Dragonmaw +3 só 26% dos itens têm 3+
   listagens e 48% dos pedidos tiveram de ser cortados por passarem de 2× a mediana; em
   Argent Dawn 95% têm 3+ listagens e só 3% foram cortados.
5. O prémio sobrevive ao filtro de liquidez (Dragonmaw +3 **1,74×**, Pozzo **1,81×**,
   Aggra **1,76×** só com itens de 3+ listagens) — não é só listagem-troll, é mercado fino a
   pedir mais. Continua a **não ser prova de venda**: o TSM não tem velocidade por reino.

---

## 1. Dados usados (e a idade deles)

| fonte | o que tem | idade |
|---|---|---|
| `dfa.db` | ≈6 370 contas, quase todas com `appearances`; escadas: `eu` 2598, `connected-dragonmaw` 2324, `connected-auchindoun` 1425, `connected-silvermoon` 200 | scan em curso (números sobem) |
| `tsm_markets.db` | 188 477 linhas de preço, 31 083 itens, 11 nomes de reino | scan non-commodity de **2026-10-16 21:36 UTC** (≈2 dias) |
| `demand_appearances.csv` | 13 287 aparições em falta com valor ≥ 70 | gerado do `dfa.db` |

Do cabaz pedido com valor ≥ 70, ficaram de fora **18 001** linhas BoP (nunca vão ao leilão),
**2 093** era-Classic (preço de outro jogo), **2 994** sem preço TSM em reino nenhum e
**7 792** sem item BoE no DB2. Sobram 2 501 com preço e **1 721 comparáveis nos 8 mercados**
(o “cabaz comum” — os mesmos itens em todos, senão as medianas comparam coisas diferentes).

---

## 2. Porque a tabela do `--sample` engana

Amostra por reino, do maior para o menor:

| reino | contas | média | @100 (100.º melhor) | ≥ 1M | máximo | lido até |
|---|---:|---:|---:|---:|---:|---:|
| Stormreaver | 1716 | 306 908 | **567 758** | 3 | 1 127 707 | rank 2335 |
| Sylvanas | 1067 | 468 708 | **668 236** | 7 | 1 126 856 | rank 2485 |
| Auchindoun | 203 | 485 293 | **442 267** | 2 | 1 126 679 | rank 2163 |
| Silvermoon | 193 | 904 144 | **869 304** | 32 | 1 195 016 | rank 2479 |
| Kazzak | 88 | 978 487 | — (só escada regional) | 33 † | 1 210 361 | rank 2437 |
| Ravencrest | 73 | 983 209 | — (só escada regional) | 30 † | 1 197 604 | rank 2483 |

† nestes dois o ≥ 1M conta as personagens que entraram na **escada da região**, não na do
reino: é uma amostra da elite, e é o que os torna não comparáveis às linhas de cima.

Duas coisas saltam à vista e ambas são artefacto:

- **A média cai quando se lê mais fundo.** O rank 1000 de um reino vale ~500–600k; incluir
  os ranks 500–2335 (Stormreaver) arrasta a média para 306k sem dizer nada sobre a qualidade
  do topo. O máximo é o mesmo em todo o lado.
- **A coluna `share` é o orçamento do scan.** 26.9% Stormreaver + 16.8% Sylvanas não é “onde
  estão os coleccionadores”: é onde o scan foi mandado olhar (boards `connected-dragonmaw` e
  `connected-auchindoun`).

E há uma terceira, mais subtil: **`rank` não significa a mesma coisa em todas as linhas.** Nos
boards `connected-*` é a posição na escada daquele mercado; no board `eu` é a posição na
**região**. Por isso os 88 do Kazzak não são “os coleccionadores do Kazzak” — são os que
entraram no topo da EU. Contagens nesse board comparam-se; profundidades não.

---

## 3. Qualidade a profundidade fixa (`depths --depth 100`)

Métrica: o score do **100.º melhor** coleccionador de cada reino na sua própria escada.
Não é “o 100.º da região”, e não é uma média — é o mesmo lugar em todos.

| # | reino | escada | chars | @100 | média | ≥ 1M | % ≥ 1M | máximo |
|---:|---|---|---:|---:|---:|---:|---:|---:|
| 1 | Silvermoon | connected-silvermoon | 200 | **869 304** | 900 549 | 32 | 16.0% | 1 195 016 |
| 2 | Sylvanas | connected-auchindoun | 1087 | **668 236** | 466 039 | 7 | 0.6% | 1 126 856 |
| 3 | Stormreaver | connected-dragonmaw | 1730 | **567 758** | 305 718 | 3 | 0.2% | 1 127 707 |
| 4 | Auchindoun | connected-auchindoun | 207 | 442 267 | 482 172 | 2 | 1.0% | 1 126 679 |
| 5 | Vashj | connected-dragonmaw | 192 | 283 632 | 346 460 | 1 | 0.5% | 1 152 515 |
| 6 | Dragonmaw | connected-dragonmaw | 174 | 254 668 | 326 024 | 0 | 0.0% | 779 425 |
| 7 | Spinebreaker | connected-dragonmaw | 141 | 219 921 | 312 415 | 0 | 0.0% | 953 784 |

Com menos de 100 coleccionadores na escada lida (não há 100.º lugar, logo ficam fora):

| reino | escada | chars | média | máximo |
|---|---|---:|---:|---:|
| Dunemaul | connected-auchindoun | 94 | 460 937 | 950 677 |
| Haomarush | connected-dragonmaw | 87 | 317 045 | 839 028 |
| Jaedenar | connected-auchindoun | 37 | 509 300 | 1 061 334 |

**Leitura:** a Silvermoon tem, ao mesmo lugar, 1,5× o score do 100.º do grupo Stormreaver, e
uma densidade de 1M+ ~80× maior (16% contra 0,2%). O grupo Auchindoun (Sylvanas) fica no meio.
Mas atenção: **a Silvermoon também é o reino mais barato do cabaz** (0,43×) — os coleccionadores
fortes e o mercado fundo andam juntos, e é isso que impede ler “mais score” como “mais lucro”.

Nos reinos que só aparecem no board `eu` (Kazzak 33 ≥1M, Ravencrest 30, Draenor 30, Hyjal 25,
Twisting Nether 24, Argent Dawn 12, Pozzo 8) há **contagem**, não profundidade: são os que
entraram no topo da região, uma amostra enviesada para a elite. Para os nivelar seria preciso
`leaderboard --realm <reino> --max-rank 100`, um pedido por reino.

---

## 4. Mercado por mercado (`realm_market`, cabaz comum de 1721 itens)

Preço por reino = `market_recent` do TSM (cobre→ouro), cortado a 2× a mediana entre mercados.
`vs mediana` = mediana ponderada de preço/mediana; 1,00× = pede o mesmo que a mediana.

| mercado | itens | cobertura | preço mediano | vs mediana | leilões/item | cortes | com 3+ listagens | prémio (3+) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Shattered Halls | 1596 | 93% | 15 248g | **1,91×** | 1 | 777 (49%) | 25% | 1,61× |
| Dragonmaw +3 | 1582 | 92% | 12 500g | **1,88×** | 1 | 764 (48%) | 26% | 1,74× |
| Aggra (Português) | 1658 | 96% | 14 100g | **1,79×** | 2 | 735 (44%) | 54% | 1,76× |
| Pozzo dell'Eternità | 1316 | 76% | 16 037g | **1,78×** | 1 | 593 (45%) | 18% | 1,81× |
| Twisting Nether | 1721 | 100% | 6 451g | **0,97×** | 4 | 153 (9%) | 84% | 0,87× |
| Draenor | 1721 | 100% | 3 040g | **0,45×** | 4 | 58 (3%) | 87% | 0,40× |
| Silvermoon | 1721 | 100% | 3 000g | **0,43×** | 7 | 45 (3%) | 93% | 0,40× |
| Argent Dawn | 1721 | 100% | 2 500g | **0,33×** | 8 | 53 (3%) | 95% | 0,31× |

Duas notas de método, porque sem elas a coluna `vs mediana` engana:

- **Reinos ligados são um mercado.** Stormreaver, Dragonmaw, Haomarush e Spinebreaker devolvem
  preços idênticos no TSM — um leilão só, quatro nomes. Contá-los como quatro inflacionava a
  tabela e puxava a mediana para o mercado deles; aparecem como `Dragonmaw +3`.
- **A referência é a mediana entre mercados, não o `region_market` do TSM.** O preço regional é
  uma média arrastada exactamente pelas listagens mortas que se querem desarmar (um pedido de
  68k num item de 700g), e com ele a Silvermoon aparecia a 0,14× “a região” — um número sem
  significado. A mediana entre mercados sobrevive a uma cotação absurda.

**A coluna que decide:** `cortes` + `com 3+ listagens`. Nos mercados fundos, 3–9% dos pedidos
são irreais e 84–95% dos itens têm mercado; nos finos, 44–49% são irreais e só 18–26% têm.
O prémio de 1,6–1,8× que resta depois de filtrar por 3+ listagens é real **como pedido**, em
mercados onde pouca gente está a vender — o que tanto pode ser oportunidade como deserto.

---

## 5. Cruzamento: onde pôr o quê

| mercado | coleccionadores (profundidade) | mercado (liquidez) | leitura |
|---|---|---|---|
| **Silvermoon** | 869k @100, 32 ≥1M (16%) | 93% com 3+ listagens, 0,43× | o melhor dos dois lados para **volume**: há compradores fortes e prateleira funda; margem baixa e concorrência a sério |
| **Argent Dawn** | só elite regional (12 ≥1M) | 95% com 3+ listagens, 0,33× | o mercado mais fundo e mais barato: **rotação máxima**, margem mínima |
| **Draenor** | 30 ≥1M (elite regional) | 87% com 3+ listagens, 0,45× | alternativa de volume, com mais folga de preço que Argent Dawn |
| **Twisting Nether** | 24 ≥1M (elite regional) | 84% com 3+ listagens, 0,97× | **melhor equilíbrio** entre preço e prateleira: pede a mediana e tem mercado |
| **Aggra (Português)** | 2 ≥1M na amostra regional (pequena) | 54% com 3+ listagens, 1,79× | meio-termo: pede 1,8× mas só metade do cabaz tem mercado; testar com poucos itens |
| **Dragonmaw +3** | 568k @100, 3 ≥1M | 26% com 3+ listagens, 1,88× | **o teu reino**: pouca concorrência e pedidos altos, mas 536 dos 1582 itens não têm uma única listagem. Vender devagar, sem pressa |
| **Shattered Halls / Pozzo** | sem escada local | 18–25% com 3+ listagens, 1,6–1,9× | finos demais para servir de plano; o preço alto é sinal de ausência de vendedores |

**Recomendação operacional**

1. **Rotação:** listar em Silvermoon (ou Draenor) ao preço da mediana, aceitando 0,40×.
   É onde o item tem mais probabilidade de encontrar comprador.
2. **Margem:** manter no grupo Dragonmaw +3 os itens de valor alto e baixa frequência, com
   duração longa. Não é onde se faz volume; é onde há menos gente a competir.
3. **Teste controlado:** Aggra com um lote pequeno (10–20 itens) para medir se o 1,76× é
   alcançável — é o único mercado intermédio com preço e volume na mesma frase.
4. **Não ler 1,9× como “vende-se mais caro lá”.** Significa “pede-se mais”; a prova de venda
   continua a ser `saleRate`/`soldPerDay` (região, não reino) e o diário.

---

## 6. O que falta para isto deixar de ser inferência

| falta | como resolver | custo |
|---|---|---|
| Demanda **por reino** (`missing_realm` está vazio) | `reweight --realm silvermoon --kinds appearances` | 1 request por conta × kind (193 contas Silvermoon ≈ 3 min a 1 req/s) |
| Preço TSM para Sylvanas, Auchindoun, Vashj, Dunemaul | visitar/scanear esses reinos com o addon TSM; sem isso não entram na comparação | — |
| Velocidade de venda por reino | não existe no TSM (é regional). Só o `journal` com vendas de **mogs** responderia | registar vendas reais |
| Comparação justa para Kazzak/Ravencrest/Draenor etc. | `leaderboard --realm <reino> --max-rank 100`, um por reino | 4 requests por reino |

Nota sobre o diário: o `journal_stats` tem 92 itens e 204 unidades vendidas, todas em
**Dragonmaw**, mas são materiais (Wool Cloth 99 un/5g, Medium Leather 64/124g, Luminous Flux
11/98g) — **não serve para calibrar transmog**. O total de 261g em 204 vendas diz algo sobre
materiais, não sobre mogs.

---

## 7. Reproduzir

```bat
REM qualidade a profundidade fixa (offline)
cd C:\economics
python -m woweconomy.services.dataforazeroth depths --depth 100 --csv depths.csv

REM mercado por reino (precisa do TSM)
cd C:\FarmAnalysis
python -m woweconomy.analytics.realm_market --db tsm_markets.db --min-score 70 --csv realms.csv

REM a tabela antiga, para contraste
cd C:\economics
python -m woweconomy.services.dataforazeroth realms --sample --min-accounts 5 --top 25
```

Os CSV referidos neste relatório estão em `reports/` quando gerados com `--csv`; os números
acima foram lidos directamente das bases em 2026-10-18.
