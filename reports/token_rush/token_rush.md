# Token Rush — relatório de liquidação (dry run)

Gerado por `python scripts/token_rush.py` a partir de `tsm_markets.db` e `inventory.csv`. Nada foi posto: este ficheiro é uma recomendação, e cada valor tem a sua base escrita ao lado.

## 1. Estado da campanha

| medida | valor |
|---|---:|
| ouro actual | 213,246 g |
| preço do token usado | 420,000 g |
| falta para o token | 206,754 g |
| alvo operacional (ouro líquido a fazer) | 200,000 g |
| **a liquidar** (o maior dos dois) | **206,754 g** |
| dias restantes | 12 |
| horas de gestão restantes | 96 h |
| linhas na fila | 2952 |

O défice é recalculado a cada execução com o preço do token e o ouro actuais. A campanha tem de liquidar **o maior** entre a falta para o token e o alvo operacional: 206,754 g. Se o objectivo for ter as duas coisas (token comprado *e* 200,000 g de reserva), o número sobe para 406,754 g.

## 2. O que o inventário vale — e o que é realizável

Três números, e só um deles é uma previsão com base em observações:

| número | valor | o que é |
|---|---:|---|
| tecto | 4,451,642 g | se **tudo** vendesse ao preço recomendado, sem taxas nem depósitos. Não é uma previsão |
| heurística | 1,795,687 g | valor potencial ponderado pelo peso de evidência nas linhas sem histórico. **Heurística**: o peso não é uma probabilidade |
| observada | 1,908 g | o que a evidência da própria conta sustenta: 110 de 2952 linhas têm vendas registadas |

> O valor de mercado estimado do inventário (~600 k g) **não** é o que se vai realizar. A diferença entre o tecto (4,451,642 g) e o número observado (1,908 g) é o espaço onde esta campanha opera, e é muito maior do que o objectivo de 200,000 g.

**O orçamento de gestão decide quantas linhas cabem.** Com 96 h e 0.25-0.50 h por linha cabem **263 linhas** (1,586,602 g pela estimativa heurística). As 2689 restantes só entram se houver tempo ou se forem materiais a vender ao fornecedor.

## 3. Postura por reino

| reino | classe | factor | concorrência | anúncios/item | confiança | avisos |
|---|---|---:|---|---:|---|---|
| Dragonmaw | thin | 1.08 | baixa | 1 | alta | métricas idênticas a Haomarush, Stormreaver: é o mesmo mercado (casa de leilão partilhada), não mercados independentes<br>pouca concorrência observada: NÃO é prova de procura; exigir evidência de venda antes de pedir mais |
| Spinebreaker | thin | 1.08 | baixa | 1 | alta | métricas idênticas a Dragonmaw, Haomarush, Stormreaver: é o mesmo mercado (casa de leilão partilhada), não mercados independentes<br>pouca concorrência observada: NÃO é prova de procura; exigir evidência de venda antes de pedir mais<br>sem actividade desta conta neste reino: avaliado, mas não pode ser publicado sem personagem |
| Stormreaver | thin | 1.08 | baixa | 1 | alta | métricas idênticas a Dragonmaw, Haomarush: é o mesmo mercado (casa de leilão partilhada), não mercados independentes<br>pouca concorrência observada: NÃO é prova de procura; exigir evidência de venda antes de pedir mais<br>sem actividade desta conta neste reino: avaliado, mas não pode ser publicado sem personagem |
| Haomarush | thin | 1.08 | baixa | 1 | alta | métricas idênticas a Dragonmaw, Stormreaver: é o mesmo mercado (casa de leilão partilhada), não mercados independentes<br>pouca concorrência observada: NÃO é prova de procura; exigir evidência de venda antes de pedir mais<br>sem actividade desta conta neste reino: avaliado, mas não pode ser publicado sem personagem |
| Twisting Nether | balanced | 0.74 | alta | 3 | alta | — |
| Argent Dawn | deep | 0.21 | alta | 5 | alta | mercado cheio: reagir depressa, mas entrar numa guerra de preços só se a venda melhorar o resultado |
| Silvermoon | deep | 0.27 | alta | 4 | alta | mercado cheio: reagir depressa, mas entrar numa guerra de preços só se a venda melhorar o resultado |
| Draenor | deep | 0.30 | alta | 4 | alta | mercado cheio: reagir depressa, mas entrar numa guerra de preços só se a venda melhorar o resultado |
| Aggra (Português) | balanced | 1.04 | media | 2 | alta | — |
| Pozzo dell'Eternità | thin | 1.24 | baixa | 1 | alta | pouca concorrência observada: NÃO é prova de procura; exigir evidência de venda antes de pedir mais<br>sem actividade desta conta neste reino: avaliado, mas não pode ser publicado sem personagem |
| Shattered Halls | thin | 1.06 | baixa | 1 | alta | pouca concorrência observada: NÃO é prova de procura; exigir evidência de venda antes de pedir mais<br>sem actividade desta conta neste reino: avaliado, mas não pode ser publicado sem personagem |
| Uldaman | thin | 1.00 | desconhecida | — | inferida | contagem de anúncios por item indisponível neste reino: a concorrência é inferida, não observada<br>classe do reino inferida do nível de preço: a contagem de anúncios não foi observada neste reino<br>sem actividade desta conta neste reino: avaliado, mas não pode ser publicado sem personagem |

Factor = mediana de `mercado local / mercado regional` medido no catálogo. A classe (`thin`/`balanced`/`deep`) vem da concorrência observada; onde não há contagem de anúncios é inferida do preço e diz isso.

## 4. Distribuição por perfil

| perfil | o que significa | itens (linhas) | ouro potencial | peso de evidência |
|---|---|---:|---:|---:|
| A | líquido | 47 | 2,459 g | 1.00 |
| B | procura provada / pouca concorrência | 1140 | 494,208 g | 0.70 |
| C | especulativo | 1559 | 4,116,036 g | 0.35 |
| D | estoque parado | 107 | 73,239 g | 0.30 |
| U | desconhecido | 99 | 0 g | 0.20 |

## 5. A fila

Ordenada por contribuição para o défice por hora de gestão. `esperado` só aparece quando há histórico de venda do próprio item; caso contrário a linha usa o valor potencial e diz *heurística*.

| # | item | reino | perfil | evidência | concorrência | referência | comparável | preço | piso | líquido | esperado | acção |
|---:|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---|
| 1 | Brocade Belt (3378) | Pozzo dell'Eternità | C | estimada | baixa | 654,645 | — | **428,629** | 0 | 407,197 | — (heurística) | postar com desconto inicial e reavaliar  |
| 2 | Fortified Boots of the Quickblade (9810) | Pozzo dell'Eternità | C | estimada | baixa | 423,423 | — | **277,236** | 0 | 263,374 | — (heurística) | postar com desconto inicial e reavaliar  |
| 3 | Evocator's Blade (2567) | Aggra (Português) | C | estimada | media | 105,464 | 11,855 | **69,053** | 1 | 65,600 | — (heurística) | postar com desconto inicial e reavaliar  |
| 4 | Pagan Vest of the Quickblade (14158) | Aggra (Português) | C | estimada | media | 89,472 | — | **58,582** | 0 | 55,653 | — (heurística) | postar com desconto inicial e reavaliar  |
| 5 | Grunt's Shield of the Aurora (15512) | Shattered Halls | C | estimada | baixa | 321,651 | — | **210,601** | 0 | 200,071 | — (heurística) | postar com desconto inicial e reavaliar  |
| 6 | Greased Cogwheel (244700) | Dragonmaw | B | estimada | baixa | 133 | — | **90** | 9 | 85 | — (heurística) | postar no preço recomendado; vigiar se e |
| 7 | Slippers of the Hissing Cult (271435) | Dragonmaw | B | estimada | baixa | 25,976 | 39,624 | **25,976** | 44 | 24,677 | — (heurística) | postar no preço recomendado; vigiar se e |
| 8 | Greased Cogwheel (244700) | Aggra (Português) | B | estimada | media | 128 | — | **86** | 9 | 82 | — (heurística) | postar no preço recomendado; vigiar se e |
| 9 | Overseer's Mandate (174790) | Twisting Nether | C | nenhuma | alta | 97,767 | 98,197 | **77,154** | 0 | 73,296 | — (heurística) | postar com desconto inicial e reavaliar  |
| 10 | Greased Cogwheel (244700) | Twisting Nether | B | estimada | alta | 91 | — | **61** | 9 | 58 | — (heurística) | postar no preço recomendado; vigiar se e |
| 11 | Wrangler's Buckler of the Quickbla (15332) | Dragonmaw | C | estimada | baixa | 131,650 | 74,727 | **65,825** | 0 | 62,534 | — (heurística) | postar com desconto inicial e reavaliar  |
| 12 | Sutarn's Ring (13105) | Aggra (Português) | C | estimada | media | 66,551 | 74,467 | **58,509** | 0 | 55,584 | — (heurística) | postar com desconto inicial e reavaliar  |
| 13 | Slippers of the Hissing Cult (271435) | Twisting Nether | B | estimada | alta | 25,606 | 13,000 | **12,803** | 44 | 12,163 | — (heurística) | postar no preço recomendado; vigiar se e |
| 14 | Bristlebark Blouse (14570) | Twisting Nether | C | estimada | alta | 66,017 | 67,440 | **50,044** | 1 | 47,542 | — (heurística) | postar com desconto inicial e reavaliar  |
| 15 | Pagan Vest of the Quickblade (14158) | Shattered Halls | C | estimada | baixa | 150,000 | — | **98,212** | 0 | 93,302 | — (heurística) | postar com desconto inicial e reavaliar  |
| 16 | Slippers of the Hissing Cult (271435) | Aggra (Português) | B | estimada | media | 24,413 | 15,000 | **12,207** | 44 | 11,596 | — (heurística) | postar no preço recomendado; vigiar se e |
| 17 | Slippers of the Hissing Cult (271435) | Draenor | B | estimada | alta | 23,800 | 14,999 | **11,900** | 44 | 11,305 | — (heurística) | postar no preço recomendado; vigiar se e |
| 18 | Slippers of the Hissing Cult (271435) | Argent Dawn | B | estimada | alta | 23,518 | 14,300 | **11,759** | 44 | 11,171 | — (heurística) | postar no preço recomendado; vigiar se e |
| 19 | Slippers of the Hissing Cult (271435) | Silvermoon | B | estimada | alta | 22,675 | 10,001 | **11,337** | 44 | 10,770 | — (heurística) | postar no preço recomendado; vigiar se e |
| 20 | Overseer's Mandate (174790) | Dragonmaw | C | nenhuma | baixa | 56,100 | 56,100 | **44,078** | 0 | 41,874 | — (heurística) | postar com desconto inicial e reavaliar  |
| 21 | Overseer's Mandate (174790) | Silvermoon | C | nenhuma | alta | 56,100 | 56,100 | **44,078** | 0 | 41,874 | — (heurística) | postar com desconto inicial e reavaliar  |
| 22 | Overseer's Mandate (174790) | Aggra (Português) | C | nenhuma | media | 56,100 | 56,100 | **44,078** | 0 | 41,874 | — (heurística) | postar com desconto inicial e reavaliar  |
| 23 | Bristlebark Blouse (14570) | Dragonmaw | C | estimada | baixa | 84,754 | 44,960 | **42,377** | 1 | 40,258 | — (heurística) | postar com desconto inicial e reavaliar  |
| 24 | Pagan Vest of the Quickblade (14158) | Dragonmaw | C | estimada | baixa | 80,032 | 52,697 | **41,404** | 0 | 39,333 | — (heurística) | postar com desconto inicial e reavaliar  |
| 25 | Pagan Vest of the Quickblade (14158) | Silvermoon | C | estimada | alta | 71,871 | 50,001 | **35,936** | 0 | 34,139 | — (heurística) | postar com desconto inicial e reavaliar  |

## 5.1 Onde publicar, por reino

| reino | linhas | potencial | observadas | activo na conta |
|---|---:|---:|---:|---|
| Dragonmaw (Dragonmaw, Spinebreaker, Stormreaver, Haomarush) | 328 | 486,188 g | 94 | sim |
| Twisting Nether | 328 | 326,250 g | 10 | sim |
| Argent Dawn | 328 | 69,733 g | 1 | sim |
| Silvermoon | 328 | 152,003 g | 4 | sim |
| Draenor | 328 | 110,885 g | 1 | sim |
| Aggra (Português) | 328 | 564,544 g | 0 | sim |
| Pozzo dell'Eternità | 328 | 1,395,997 g | 0 | não |
| Shattered Halls | 328 | 997,733 g | 0 | não |
| Uldaman | 328 | 582,607 g | 0 | não |

## 5.2 Os itens que decidem a campanha

Ordenados pelo ouro em jogo, não pela prioridade: são os que podem, sozinhos, cobrir o défice. Todos sem histórico de venda — por isso o risco é grande e o preço agressivo é a aposta, não a evidência.

| item | reino | perfil | referência | preço recomendado | potencial | risco |
|---|---|---|---:|---:|---:|---|
| Brocade Belt (3378) | Pozzo dell'Eternità | C | 654,645 | 428,629 | 428,629 g | sem vendas registadas |
| Fortified Boots of the Quickblad (9810) | Pozzo dell'Eternità | C | 423,423 | 277,236 | 277,236 g | sem vendas registadas |
| Grunt's Shield of the Aurora (15512) | Shattered Halls | C | 321,651 | 210,601 | 210,601 g | sem vendas registadas |
| Pagan Vest of the Quickblade (14158) | Uldaman | C | 133,325 | 101,843 | 101,843 g | sem vendas registadas |
| Pagan Vest of the Quickblade (14158) | Shattered Halls | C | 150,000 | 98,212 | 98,212 g | sem vendas registadas |
| Overseer's Mandate (174790) | Twisting Nether | C | 97,767 | 77,154 | 77,154 g | sem vendas registadas |
| Evocator's Blade (2567) | Aggra (Português) | C | 105,464 | 69,053 | 69,053 g | sem vendas registadas |
| Pagan Shoes of the Aurora (14159) | Shattered Halls | C | 84,463 | 67,583 | 67,583 g | sem vendas registadas |
| Pagan Vest of the Quickblade (14158) | Pozzo dell'Eternità | C | 78,877 | 66,784 | 66,784 g | sem vendas registadas |
| Wrangler's Buckler of the Quickb (15332) | Dragonmaw | C | 131,650 | 65,825 | 65,825 g | sem vendas registadas |
| Outrunner's Slippers of the Auro (15498) | Shattered Halls | C | 76,279 | 62,108 | 62,108 g | sem vendas registadas |
| Sutarn's Ring (13105) | Shattered Halls | C | 90,967 | 59,560 | 59,560 g | sem vendas registadas |

## 5.3 Como cada preço foi construído

**1. Brocade Belt (3378) em Pozzo dell'Eternità**

- Perfil **C** (especulativo), confiança **estimada**, cobertura da evidência 67%.
  - nenhuma venda observada; a procura não está demonstrada
  - referência regional 405 g é um *ask*, não uma venda
  - 0 anúncio(s) ativo(s) no reino
  - referência 654,645 g - 25% (sem comparável válido)
  - perfil C x0.97
  - referência 654,645 g → preço recomendado **428,629 g**, piso 0 g, tecto 654,645 g, líquido depois da taxa de 5% 407,197 g, depósito 1 g por tentativa
  - esperado: sem estimativa (heurística)

**2. Fortified Boots of the Quickblade (9810) em Pozzo dell'Eternità**

- Perfil **C** (especulativo), confiança **estimada**, cobertura da evidência 67%.
  - nenhuma venda observada; a procura não está demonstrada
  - referência regional 133 g é um *ask*, não uma venda
  - 0 anúncio(s) ativo(s) no reino
  - referência 423,423 g - 25% (sem comparável válido)
  - perfil C x0.97
  - referência 423,423 g → preço recomendado **277,236 g**, piso 0 g, tecto 423,423 g, líquido depois da taxa de 5% 263,374 g, depósito 1 g por tentativa
  - esperado: sem estimativa (heurística)

**3. Evocator's Blade (2567) em Aggra (Português)**

- Perfil **C** (especulativo), confiança **estimada**, cobertura da evidência 67%.
  - nenhuma venda observada; a procura não está demonstrada
  - referência regional 26,178 g é um *ask*, não uma venda
  - 1 anúncio(s) ativo(s) no reino
  - referência 105,464 g - 25% (sem comparável válido)
  - perfil C x0.97
  - referência 105,464 g → preço recomendado **69,053 g**, piso 1 g, tecto 105,464 g, líquido depois da taxa de 5% 65,600 g, depósito 2 g por tentativa
  - esperado: sem estimativa (heurística)
  - ⚠ anúncio mais barato (11,855 g) não é um comparável válido (11% da referência): ignorado

**4. Pagan Vest of the Quickblade (14158) em Aggra (Português)**

- Perfil **C** (especulativo), confiança **estimada**, cobertura da evidência 67%.
  - nenhuma venda observada; a procura não está demonstrada
  - referência regional 89,071 g é um *ask*, não uma venda
  - 0 anúncio(s) ativo(s) no reino
  - referência 89,472 g - 25% (sem comparável válido)
  - perfil C x0.97
  - referência 89,472 g → preço recomendado **58,582 g**, piso 0 g, tecto 89,472 g, líquido depois da taxa de 5% 55,653 g, depósito 1 g por tentativa
  - esperado: sem estimativa (heurística)

**5. Grunt's Shield of the Aurora (15512) em Shattered Halls**

- Perfil **C** (especulativo), confiança **estimada**, cobertura da evidência 67%.
  - nenhuma venda observada; a procura não está demonstrada
  - referência regional 9,527 g é um *ask*, não uma venda
  - 0 anúncio(s) ativo(s) no reino
  - referência 321,651 g - 25% (sem comparável válido)
  - perfil C x0.97
  - referência 321,651 g → preço recomendado **210,601 g**, piso 0 g, tecto 321,651 g, líquido depois da taxa de 5% 200,071 g, depósito 1 g por tentativa
  - esperado: sem estimativa (heurística)

**6. Greased Cogwheel (244700) em Dragonmaw**

- Perfil **B** (procura provada / pouca concorrência), confiança **estimada**, cobertura da evidência 33%.
  - sem vendas próprias; a região estima 2.78 vendas/dia (estimativa TSM, não observação)
  - referência 133 g - 25% (sem comparável válido)
  - referência 133 g → preço recomendado **90 g**, piso 9 g, tecto 133 g, líquido depois da taxa de 5% 85 g, depósito 4 g por tentativa
  - esperado: sem estimativa (heurística)
  - ⚠ sem preço local neste reino: a referência é a regional corrigida

## 6. Marcos da campanha

| dia | dias que restam | défice | por dia | acção |
|---:|---:|---:|---:|---|
| 3 | 9 | 206,754 g | 22,973 g | até ao dia 3 precisas de 206,754 g de mais (22,973 g/dia) a 8 h/dia |
| 6 | 6 | 206,754 g | 34,459 g | até ao dia 6 precisas de 206,754 g de mais (34,459 g/dia) a 8 h/dia |
| 9 | 3 | 206,754 g | 68,918 g | até ao dia 9 precisas de 206,754 g de mais (68,918 g/dia) a 8 h/dia |
| 12 | 0 | 206,754 g | 206,754 g | até ao dia 12 precisas de 206,754 g de mais (206,754 g/dia) a 8 h/dia |

## 7. O que é dado e o que é heurística

**Dado (observado):** as vendas e expirações do próprio inventário (`journal_stats`), o mercado local e o anúncio mais barato de cada reino (`tsm_realm_price`), a referência regional (`tsm_region_price`), a contagem de anúncios nos 12 reinos descarregados (`tsm_market`) e o preço de venda ao fornecedor (`blizzard_items`).

**Estimado (não observado):** `sold_per_day` e `sale_rate` regionais da TSM (estimativas da Blizzard, não vendas medidas). Usados apenas para classificar a procura, e sempre com confiança *estimada*.

**Heurística (decisão, não medição):** as bandas de undercut 15-25 % / 10-20 % / 5-15 %, o factor de prazo (1,00 → 0.90), o peso de evidência da fila, o limite de perda (35% face ao custo conhecido), o preço mínimo (0.00 g, ou o valor de venda ao fornecedor quando existe), a taxa de casa de auction (5%) e a regra de depósito (5%, mínimo 1 g, máximo 300% do valor de venda ao fornecedor). Estes custos estão **assumidos, não verificados** nesta base de dados.

**Nunca garantido:** nenhuma linha promete venda. O modelo nunca inventa probabilidades: onde não há histórico, a linha é marcada *heurística* e a ordem usa um peso de evidência, não uma probabilidade de venda.

## 8. Bloqueios

- **Sem custo de acquisition:** o inventário não traz o preço de compra. O piso de perda só pode ser calculado contra o preço a que o item vendeu no passado (`sold_copper`), e é esse que é usado; quando não há vendas, o piso fica no mínimo configurado e a perda fica *desconhecida*.
- **Sem histórico por realm além do observado:** o `journal_stats` é agregado por item e reino, sem datas, o que impede medir tempo até à venda e taxa de venda por item com precisão de dias.
- **Contagem de anúncios em 12 reinos:** nos outros 80 a concorrência é inferida do nível de preço e o preço de venda fica *sem comparável*.
- **Sem backtest de lucro por reino:** não há série histórica de vendas por reino; a validação tem de ser feita com as vendas desta campanha.

## 9. Ficheiros gerados

- `reports/token_rush/rush_queue.csv` — a fila completa (2952 linhas).
- `reports/token_rush/flipqueue_<reino>.csv` — lista para o FlipQueue (Import → colar): `Sell Price` é o preço recomendado e `Sell Realm` é o reino alvo.
- `reports/mog_groups/liquidity_mogs_groups_<reino>.tsm` — import do TSM, **um grupo e uma operação por item**, com o preço recomendado, o piso e o tecto. Importa-se num perfil novo; não altera as operações normais.

> Nenhum ficheiro foi importado no TSM e nada foi configurado em jogo. Importa primeiro num perfil de teste.
