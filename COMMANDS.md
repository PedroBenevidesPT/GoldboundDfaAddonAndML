# Comandos — FarmAnalysis / economia WoW

Referência dos entry points que existem no código (extraída do `--help` de cada comando,
não do README). Cada comando corre da raiz da árvore indicada.

1 Onde correr · 2 Pipeline · 3 Mercado e preços · 4 Snapshots TSM · 5 Probabilidade de venda ·
6 Coleccionadores · 7 Ranking de farms · 8 Zonas · 9 Itens · 10 Diário e Auction Intelligence · 11 Scanner DfA ·
12 Testes · 13 Cryptogame · 14 Auction-house intelligence · 15 Sentinel (ML validado) · Armadilhas

---

## 1. Onde correr

O projecto está partido em duas pastas e um comando só existe numa delas:

| Pasta | Tem | Não tem |
|---|---|---|
| `C:\FarmAnalysis` | analytics (`farm_ranking`, `collector_demand`, `boe_targets`, `dfa_report`), `scripts/`, `tests/`, `run_all.py` | o scanner DfA |
| `C:\economics` | scanner DfA (`services/dataforazeroth.py`, `services/dfa_fast.py`), `woweconomy/dfa/` (base + cache + CSVs), `cryptogame` | `woweconomy/analytics/` |

- `python -m woweconomy.analytics.*` → **só** em `C:\FarmAnalysis`.
- `python -m woweconomy.services.dataforazeroth …` → **só** em `C:\economics`.
- Os `demand_*.csv` são encontrados sozinhos: `DFA_DIR`, depois `<projecto>/woweconomy/dfa`,
  depois `..\economics\woweconomy\dfa`. `--dfa-dir` só se apontares para outro sítio.
- `calc_price.py` e `tsm_price_preview.py` existem nas duas árvores — usa o da árvore em que estás.

---

## 2. Pipeline do dia a dia

```bat
python scripts/run_all.py --list          REM etapas: sync scrape repair enrich rank score inventory journal tests
python scripts/run_all.py --dry-run       REM mostra a sequência, não corre nada
python scripts/run_all.py                 REM tudo
python scripts/run_all.py --scrape        REM inclui o scrape Wowhead (abre browser, lento)
python scripts/run_all.py --fetch-csv     REM o sync também descarrega os CSVs públicos da TSM
python scripts/run_all.py --only rank,inventory
python scripts/run_all.py --skip enrich,score --keep-going
python scripts/run_all.py --scrape-zone zone=331/ashenvale --scrape-zone zone=16/azshara
```

`--only` e `--skip` levam listas separadas por **vírgulas** (não por espaços).

---

## 3. Mercado e preços

```bat
python scripts/sync_market.py                 REM AppData -> tsm_* + CSVs públicos
python scripts/sync_market.py --check         REM só frescura, não escreve
python scripts/sync_market.py --no-csv        REM só AppData, sem rede
python scripts/sync_market.py --csv-only
python scripts/sync_market.py --force --quiet
python scripts/fetch_csv.py                   REM reescreve csv/items.csv (não tem flags)

python scripts/blizzard_realms.py             REM reinos + grupos connected (EU), API Blizzard
python scripts/blizzard_realms.py --region us --json reports/realms_us.json

python scripts/calc_price.py --inventory inventory.csv --db tsm_markets.db --rebuild
python scripts/calc_price.py --inventory inventory.csv --min-quality UNCOMMON --audit inventory_audit.csv
python scripts/calc_price.py --item 229155 --qty 1 --db tsm_markets.db
python scripts/calc_price.py --all-items --min-quality RARE --out priced.csv
python scripts/calc_price.py --inventory inventory.csv --sep ,
python scripts/calc_price.py --inventory scripts/inventario.csv --price-model tsm-avg
python scripts/calc_price.py build-db --appdata "…/AppData.lua"

python scripts/tsm_price_preview.py [--lenient] [--doc tsm_strings.md]
```

- **`calc_price` afináveis:** `--target-posts --elasticity --undercount --realm-cap --sale-cap
  --relist-cap --qty --vendor --drop-rate --own --realm --market --historical --region-market
  --region-hist --saleavg --names --discovery-fraction`.
- **`blizzard_realms`** usa o namespace *dynamic* (`dynamic-eu`/`dynamic-us`) e as mesmas
  credenciais do cliente de itens (`.env`: `BLIZZARD_CLIENT_ID` / `BLIZZARD_CLIENT_SECRET`).
  O `realm/index` não traz `connected_realm` — o mapa reino→grupo vem dos próprios grupos.

---

## 4. Snapshots TSM de todos os reinos

Os cinco CSV públicos (realm items/pets, region items/pets, commodities) são guardados por
**mercado connected**, não por slug: os membros de um grupo partilham leilão e a TSM serve
ficheiros idênticos, por isso só se descarrega o canonical e o `tsm_realm_group` mapeia o resto.

```bat
python scripts/fetch_tsm_snapshots.py --dry-run        REM 187 URLs, sem rede
python scripts/fetch_tsm_snapshots.py --limit 4        REM fumo: 4 ficheiros
python scripts/fetch_tsm_snapshots.py                  REM 92 grupos + region (retail/eu)
python scripts/fetch_tsm_snapshots.py --realms ravencrest,tarren-mill
python scripts/fetch_tsm_snapshots.py --no-region
python scripts/fetch_tsm_snapshots.py --missing-only   REM só os que ainda não têm dados
python scripts/fetch_tsm_snapshots.py --limit 20 --workers 1 --delay 3

python scripts/import_tsm_existing.py                  REM semeia as tabelas com o que está em disco
python scripts/import_tsm_existing.py --no-appdata     REM só os CSV
```

### Porque havia 429 (e o que mudou)

O limite do CDN é **por IP, não por ficheiro**: ~60 pedidos numa hora davam
`Retry-After: 3600` e uma hora de bloqueio, e o run seguinte voltava a pedir os mesmos.

- **GET condicional** — `ETag`/`Last-Modified` em `tsm_snapshot`; a TSM republica de ~3 em 3 h e
  o CDN guarda 4 h, por isso a maioria das releituras é **304 com zero bytes** (~800 KB poupados).
- **Ordem de retoma** — nunca buscados primeiro, depois os mais antigos (`ts.order_for_resume`);
  `--limit N` corta nessa ordem, não na da lista.
- **Cooldown** — 429 com espera > 15 s aborta o run e grava `tsm_meta.ratelimit_until`; um 429
  curto é dormido uma vez, nunca quatro tentativas no mesmo URL.
- **Cadência** — 1 worker + `--delay 3` (~20 pedidos/min) passou 42 ficheiros sem 429. Para os 92:
  blocos de ~20 com pausa de 1–2 min, ou o ciclo de hora a hora.

**Estado a 18-10-2026: 92/92 mercados com dados.**

### O que fica na base (`tsm_markets.db`)

| Tabela | Conteúdo |
|---|---|
| `tsm_snapshot` | um registo por ficheiro novo: scope, kind, `updatedAt`, SHA-256, linhas, alteradas, caminho |
| `tsm_realm_price` / `tsm_realm_pet_price` | último preço por item/species em cada mercado |
| `tsm_region_price` / `tsm_region_pet_price` / `tsm_commodity_price` | os três ficheiros de região |
| `tsm_price_change` | **só** as linhas alteradas desde o snapshot anterior (o 1.º é tudo) |
| `tsm_realm_group` | slug → grupo connected → slug canonical |

CSV originais em gzip: `data/tsm/snapshots/<timestamp>/`. Mesmo SHA-256 = sem snapshot nem
preços reescritos. Os CSV de reino **não trazem `numAuctions`** — profundidade de prateleira
continua a vir do AppData (`tsm_market.num_auctions`), só para os reinos escaneados.

---

## 5. Probabilidade de venda

Três leituras do mesmo modelo. `λ_região` é o `sold_per_day` da TSM (por item, regional);
`compradores_reino` são as contas da amostra DfA que ainda não têm a aparência; `N` é 1 quando
o reino já tem a peça à venda e 0 quando a prateleira está vazia:

```text
λ_reino    = λ_região · compradores_reino / compradores_EU
P(janela)  = 1 - exp(-dias · λ_reino / (N + 1))
E[ouro]    = P · preço      (listing mais barato, senão MV do reino, senão MV regional)
```

Esta é a leitura **analítica** (Poisson com `sold_per_day`), separada da
probabilidade calibrada do Sentinel. O jogo usa o Sentinel (`psellByHorizon`,
secção 15); este comando serve para explorar o catálogo inteiro.

```bat
python scripts/sale_probability.py                      REM catálogo completo: melhores reinos
python scripts/sale_probability.py --top 20 --bands
python scripts/sale_probability.py --items Silvermoon --items-top 15
python scripts/sale_probability.py --where 20            REM pares (reino, item) mais rentáveis
python scripts/sale_probability.py --csv reports/sale_probability.csv

python scripts/inventory_prob.py --inventory inventory.csv
python scripts/inventory_prob.py --inventory inventory.csv --realm Draenor
python scripts/inventory_prob.py --inventory inventory.csv --simulate 4000
python scripts/inventory_prob.py --inventory inventory.csv --per-item --sort p
python scripts/inventory_prob.py --inventory inventory.csv --csv reports/inventory_prob.csv

python scripts/realm_48h.py                              REM ciclo de 48 h por reino (95 itens)
python scripts/realm_48h.py --top 10 --csv reports/realm_48h.csv
```

- **`--days` 30** por omissão; `--cap 3` corta o preço a 3× a mediana entre reinos (sem isso uma
  listagem estagnada decide o ranking); `--min-realms`/`--min-buyers` exigem suporte mínimo.
- **`inventory_prob`** lê o export do TSM (`ItemID;Name;…;Quantity`, `;` ou `,`) e corre só nos
  itens que tens — rápido em vez de varrer 10 mil itens × 92 reinos. `--realm` detalha um reino,
  `--simulate N` repete as listagens com semente fixa (`random.Random(7)`) para dar intervalos.
- **`realm_48h`** é o outro lado: `--demand` lê `reports/item_demand_realms.csv` (gerado por
  `item_demand --where --where-csv` — atenção ao aviso de BoP em §6). Relatórios:
  [probabilidade_venda.md](reports/probabilidade_venda.md), [analise_92_reinos.md](reports/analise_92_reinos.md).

**Reinos ligados: agrupa pela casa de leilão, não pelo nome do ficheiro.** Um ficheiro TSM serve
todo o grupo connected, e a conta compra/vende no leilão partilhado. Os scripts somam as contas
do grupo via `tsm_realm_group` — Auchindoun = Auchindoun + Sylvanas + Dunemaul + Jaedenar (217
contas na amostra, não 37). Comparar reinos por nome dá rankings errados.

**O que estes números não provam:** a ordem dos reinos acompanha, em parte, a profundidade da
amostra (coluna `contas`: 217 em Auchindoun contra 23 de mediana por casa de leilão). E a banda
700–800 mil só tem bitmap para **79 de 4 746 contas (1,7 %)**, por isso os wanters do lado caro
do catálogo estão subcontados. A escolha de reino vale menos do que os factos que não dependem
dela — o stock vale o mesmo em qualquer leilão.

### Escolher o cluster com o stock real (`best_realm`)

```bat
python scripts/best_realm.py                                   REM cabaz = inventário
python scripts/best_realm.py --basket farm --top 12            REM 150 itens de aquisição
python scripts/best_realm.py --basket rare --sensitivity       REM >100k + robustez
python scripts/best_realm.py --combined --top 8                REM índice 15/60/25 dos 3 cabazes
python scripts/best_realm.py --explain Antonidas --items-csv reports/itens_antonidas.csv
python -m pytest tests/test_best_realm.py -q
```

Agrupa por casa de leilão (`tsm_realm_group`) e usa o **segmento ≥ 900 000**, o único em que a
amostra DfA está 100 % bitmapped (1 904 contas em 92 clusters; em 800–900k a cobertura é 40 % e
em 680–800k é 2 %). A probabilidade de venda é `1-exp(-30·λ·share/(N+1))` com `λ` = `sold_per_day`
regional, `share` = fracção dos compradores do item no cluster (com shrinkage) e `N` = 1 quando o
item já está listado, 0 quando a TSM mostra a prateleira vazia. Preço = `market_value` do cluster
limitado a [0,5; 2,0]× a mediana entre clusters.

- `--basket inventory|farm|rare`, `--shrinkage 10`, `--price-cap 2`, `--min-score 900000`.
- `--csv` grava os 92 clusters; `--explain <cluster>` imprime item a item e `--items-csv` grava.
- `--sensitivity` varre `shrink` 5/10/20 × tecto 1,5/2,0/3,0 e diz se o vencedor muda.
- As bases abrem em `mode=ro`. `num_auctions` só existe para 8 clusters e é do AppData antigo —
  serve de sinal de profundidade, nunca de preço. Não há histórico de vendas por item/reino:
  `P30` é modelo, não sell-through observado. Relatório: [melhor_reino.md](reports/melhor_reino.md).

---

## 6. Coleccionadores: o que falta a quem

```bat
python -m woweconomy.analytics.collector_demand inventory --db tsm_markets.db
python -m woweconomy.analytics.collector_demand inventory --db tsm_markets.db --csv gaps.csv
python -m woweconomy.analytics.collector_demand zone woweconomy/wowhead_zones/zone_16.json --db tsm_markets.db --out Azshara_gaps.txt
python -m woweconomy.analytics.collector_demand zone woweconomy/wowhead_zones/zone_331.json woweconomy/wowhead_zones/zone_16.json --db tsm_markets.db --out Ashenvale_azsh_c_report.txt
python -m woweconomy.analytics.collector_demand item "Pattern: Chimeric Gloves" --db tsm_markets.db
python -m woweconomy.analytics.collector_demand item 271435 14165 --db tsm_markets.db
python -m woweconomy.analytics.collector_demand zone woweconomy/wowhead_zones/zone_16.json --min-score 60
```

Ids para grupos do TSM (um grupo por linha, `i:<id>,i:<id>,...`, pronto a colar no Import do
TSM 4). N cortes = N+1 grupos; cada corte é um limite inferior inclusivo:

```bat
REM 3 grupos (default): 85+ / 65-85 / <65
python -m woweconomy.analytics.collector_demand ids woweconomy/wowhead_zones/zone_331.json woweconomy/wowhead_zones/zone_16.json --db tsm_markets.db --out-prefix mogs

REM 5 grupos (85+ / 75-85 / 65-75 / 55-65 / <55)
python -m woweconomy.analytics.collector_demand ids woweconomy/wowhead_zones/zone_331.json woweconomy/wowhead_zones/zone_16.json --db tsm_markets.db --cuts 85,75,65,55 --band todas --out-prefix mogs

REM uma banda só (rótulo, posição ou `alta`/`nicho`/`comum`/`todas`)
python -m woweconomy.analytics.collector_demand ids woweconomy/wowhead_zones/zone_16.json --cuts 50 --band alta
python -m woweconomy.analytics.collector_demand ids woweconomy/wowhead_zones/zone_16.json --cuts 85,75,65,55 --band 2,3
```

Flags comuns: `--dfa-dir --market-csv --db --facts-db --no-era-filter --min-score --inventory
--zones --no-zones --top --mob-drops --out --csv -q`; no modo `ids` também `--cuts --band
--out-prefix`.

Converter listas de IDs antigas (um por linha, ou `123,456`) para o formato do TSM 4.
Sem `--in-place` imprime a linha no stdout; `--check` só valida:

```bat
python scripts/tsm_item_list.py --in-place reports/mog_groups
python scripts/tsm_item_list.py --check reports/mog_groups
python scripts/tsm_item_list.py mogs_85plus.txt
```

String única com grupos + nomes + operações de auctioning (uma linha, pronta para
`Groups -> Import`; cria os grupos `Liquid`, `Collector`, `Slow`, `Very_Slow`,
`Unproven`, `Unknown`, `Conflict` e as operações `Mogs ...`):

```bat
python scripts/tsm_groups_export.py --show     REM revisão, não grava
python scripts/tsm_groups_export.py --write    REM reports/mog_groups/mogs_groups.tsm (referência regional)
python scripts/tsm_groups_export.py --write --realm Dragonmaw   REM mogs_groups_dragonmaw.tsm (perfil medido do reino)
python scripts/tsm_groups_export.py --write --realms "Dragonmaw,Argent Dawn"   REM um ficheiro por reino
python scripts/tsm_groups_export.py --write --all-realms        REM um ficheiro por reino com perfil
python scripts/tsm_groups_export.py --write --no-operations      REM só grupos
python scripts/tsm_groups_export.py --write --conflict-inline    REM Conflict dentro do grupo primário
```

Por omissão os itens de `conflict` passam para o grupo `Conflict` (o TSM só deixa
um item num grupo). Importa primeiro num perfil de teste: as strings de preço são
as do relatório, mas não foram executadas dentro do addon. As operações ancoram
no **mercado regional** (`dbregionmarketavg` → `dbregionhistorical` →
`dbhistorical`), nunca no `dbmarket`/`dbminbuyout` do reino nem no
`dbregionsaleavg` (vendas, contaminado por snipes). O factor é **bandado**
(`ifgt` sobre o valor: 500 g / 5 000 g / 50 000 g) e sobe com a banda; `unknown`
não posta. O `--realm` aplica o **perfil medido do reino** (factor local,
postura, duração e janela de retenção em `maxExpires`) e o `--all-realms` gera um
ficheiro por reino com perfil medido. O
classificador é por **aparência** (todas as variantes do mesmo look no
mesmo grupo); `python scripts/mog_groups.py --items variants` reduz aos 38 746
itens com preço no look, em vez dos 127 155 com aparência.

O perfil por reino vem de `python scripts/mog_realm_profiles.py` (que mede a
concorrência e o nível local de cada reino) e vive em
`woweconomy/analytics/mog_realm.py`; o relatório está em
`reports/mog_groups_realm_profiles.md`.

Dois detalhes do TSM que valem a pena saber: um grupo só fica com a operação se a
tabela do módulo trouxer `override = true` (o gerador já o faz; sem isso o TSM
descarta a operação em silêncio e o grupo herda a raiz do perfil). E o painel
"Ungrouped Items" do editor de grupos mostra as variantes (bonus id / item level)
como desagrupadas quando o campo *Show ungrouped items as* está em **Specific
Item** — os grupos guardam o id base. Põe esse campo em **Base Item** para veres
que os itens já estão nos grupos.

### Referência ponderada pela evidência (`reference_price`)

```bat
python scripts/reference_backtest.py --write    REM walk-forward + calibração
python scripts/pricing_groups.py --write        REM gera a proposta ponderada
python scripts/pricing_groups.py --write --method arithmetic   REM média aritmética
```

O preço de referência deixou de ser `first(dbmarket, dbregionmarketavg,
dbregionhistorical)` e passou a ser uma **mistura ponderada pela qualidade da
evidência local**. É uma **proposta**: nada é importado, e a proposta original
fica intacta.

| ficheiro | o que é |
|---|---|
| `reports/pricing_groups/pricing_groups_proposal.tsm` | a proposta **original** (31 grupos, referência `first(...)`) |
| `reports/pricing_groups/pricing_groups_weighted_proposal.tsm` | a **candidata** (os mesmos 31 grupos, referência ponderada) |
| `reports/pricing_groups/pricing_groups_weighted_report.md` | o relatório da candidata: o que muda, de onde vêm os pesos, os preços que mudam, a verificação do payload |
| `reports/pricing_groups/reference_backtest.md` | a avaliação dos estimadores (walk-forward do nível + calibração com as vendas) |

O peso local sai de três **medidas** (profundidade, frescura, dispersão) e é
limitado a [0,50 ; 1,00]: a referência regional nunca domina. A divergência
local/regional é um **diagnóstico** (baixa a confiança), nunca uma regra de
preço. A mistura por omissão é a **geométrica** — no walk-forward ela bate a
aritmética com os mesmos pesos e, sobretudo, não deixa um regional inflado
mover o resultado na proporção do seu tamanho.

A expressão gerada é TSM válido e não precisa de fonte customizada:

```
ifgt(ifgt(dbmarket, 99999998999, 0, dbmarket), 0,
  ifgt(ifgt(dbregionmarketavg, 99999998999, 0, dbregionmarketavg), 0,
     dbmarket ^ w * dbregionmarketavg ^ (1 - w),
     dbmarket),
  ifgt(ifgt(dbregionmarketavg, 99999998999, 0, dbregionmarketavg), 0,
     dbregionmarketavg, dbregionhistorical))
```

O `99999998999` é o sentinel da TSM tratado como ausente; a escada de fallbacks
é explícita. A verificação (`pricing_groups.verify_weighted_expression`) avalia
a expressão de cada operação com os valores medianos da célula e confirma que
bate com o modelo antes de o ficheiro ser escrito.

### Grupos de Token Rush por reino (`token_rush_profiles`)

```bat
python scripts/token_rush_profiles.py --write
python scripts/token_rush_profiles.py --write --realms "Dragonmaw,Argent Dawn"
```

Gera **um export TSM por reino**, para um **perfil TSM por reino**:

| ficheiro | o que é |
|---|---|
| `reports/mog_groups/token_rush_<reino>.tsm` | os **mesmos grupos e os mesmos 127 155 itens** do export mog (`tsm_groups_export.py --all-realms`), com o **preço do motor de evidência** e a operação de rush |
| `reports/token_rush_groups.md` | o relatório: os reinos, os perfis, os grupos com as suas razões e preços, o protocolo de importação e a reversão |

```bat
python scripts/token_rush_profiles.py --write
python scripts/token_rush_profiles.py --write --realms "Dragonmaw,Argent Dawn"
```

O preço de cada grupo é uma **fórmula viva**: `ifgt(dbmarket, banda, r × dbmarket, …)`,
onde `r` é a **razão medida** (preço recomendado ÷ mercado local) dos itens
daquele grupo e banda — o preço acompanha o mercado local e a calibragem vem
do motor de evidência, não de uma constante. O piso é
`max(razão × dbmarket, 1.5 × vendorsell)`.

A operação é de **rush**: 12 h de duração, 7 dias de retenção (`maxExpires` 13),
`priceReset = none`, `aboveMax = maxPrice`, sem cancelamento automático.

**Nada é importado.** O perfil cria-se à mão (`TSM → Settings → General →
Profiles`, nome `mogs-<reino>`) e o import faz-se **num perfil limpo**; as
propostas originais e ponderadas nunca devem coexistir no mesmo perfil.

### Registo de eventos do addon `DFA_AuctionIntelligence` (`dfa_ai`)

```bat
python scripts/dfa_ai.py --status     # onde está o export e o que há dentro
python scripts/dfa_ai.py --import     # importa para tsm_markets.db (idempotente)
python scripts/dfa_ai.py --measure    # as medições que os eventos permitem
python scripts/dfa_ai.py --plan       # o protocolo de teste (que postar, a que preço)
```

O addon `DFA_AuctionIntelligence` (em `Interface/AddOns`) captura o que faltava
ao banco do DFA: **data** de cada evento, o **preço pedido** e o **preço
realizado**, o **depósito** e a **taxa de casa de auction** efetivamente cobrados,
e a **referência de mercado (`dbmarket`) no momento do evento**. É isso que
transforma o estimador "E2" (venda ÷ referência *daquele momento*) de impossível
em mensurável.

Em jogo:

```
/dfaai status     # estado e número de eventos
/dfaai diag       # cobertura e campos em falta
/dfaai sync       # re-importa o arquivo do Journalator (datas!)
/dfaai export     # regenera o export nos SavedVariables
```

O export é refrescado no login e no logout; depois é só:

```bat
python scripts/dfa_ai.py --import --measure
```

O que passa a ser mensurável:

| medição | o que usa |
|---|---|
| **clearance datado** | a venda ÷ o `dbmarket` do momento, por banda — o estimador E2 |
| **tempo até à venda** | o evento `listing` e a venda que o fechou, pelo mesmo item e reino |
| **sell-through** | listagens vs vendas e expirações, por item e reino |
| **custos reais** | a taxa e o depósito efetivamente cobrados (substituem os 5 % assumidos) |
| **preço pedido vs realizado** | o undercut que resulted em venda |

**Protocolo para começar a medir** (`--plan` escreve
`reports/auction_intel/test_plan.csv`):

1. `--plan` escolhe os itens do inventário com preço de venda médio conhecido e
   escreve o **preço de teste** (a média de venda da região, que é o nível que o
   mercado paga) ao lado do ask e da parede;
2. posta-se esse conjunto a um **preço conhecido** e deixa-se o addon registar;
3. `/dfaai export`, sai do jogo, `--import --measure`;
4. o relatório diz, item a item, quanto tempo demorou, a que preço vendeu e a
   que razão do ask — que é exactamente o que faltava para validar o motor.

### Motor de preço baseado em evidência (`pricing_engine`)

```bat
python scripts/pricing_calibration.py --write   REM mede o peso de cada fonte
python scripts/pricing_engine.py --write        REM estimativas item a item
python scripts/pricing_engine.py --write --backtest   REM walk-forward
```

O motor implementa a **estimativa aritmética ponderada pela evidência**, com
três saídas separadas por item: **A** o valor realizável estimado (com
intervalo, confiança e proveniência), **B** a avaliação de liquidez e **C** o
preço de listagem recomendado.

O princípio: **os anúncios mostram o que os vendedores querem receber; as
transacções mostram o que alguém pagou.** As duas famílias não são misturadas
— primeiro estima-se o valor de transacção, depois verifica-se se as
referências de mercado são compatíveis com ele. Os pesos **não são escolhidos**:
`pricing_calibration.py` mede, contra as vendas reais da conta, quanto cada
família de fontes prevê o preço realizado.

| ficheiro | o que é |
|---|---|
| `reports/pricing_engine/calibration.md` / `.json` | a calibração: cobertura, erro mediano e peso medido por família |
| `reports/pricing_engine/item_estimates.csv` | 328 linhas × 35 colunas: valor, intervalo, confiança, base, contribuições com peso, fontes rejeitadas com o motivo, liquidez, listagem, bruto e líquido |
| `reports/pricing_engine/pricing_engine.md` | o relatório: as fontes com origem/data/cobertura/limitações, os pesos medidos, os itens e as limitações |
| `reports/pricing_engine/backtest.md` | o walk-forward: o motor contra `dbmarket`, o regional, o histórico e a média de venda |

Duas hipóteses são testadas por item: **preço anunciado inflacionado** (as
vendas não confirmam o ask → a confiança na referência baixa e o preço segue as
vendas) e **preço elevado comercialmente defensável** (vendas recorrentes e
evidência compatível → o preço **não** é baixado).

Limites declarados: as vendas do `journal_stats` **não têm data** (o arquivo
bruto do Journalator é comprimido e cobre 4 dias), a TSM **não expõe** a amostra
da média de venda regional, e o volume é **regional** (nunca promete venda
local).

### Guard de preço mínimo do Token Rush (`token_rush_guard`)

```bat
python scripts/token_rush_guard.py --write
```

Compara, sobre os **mesmos 328 itens** e com os mesmos grupos e coeficientes,
quatro alternativas de piso de preço:

| | o que muda |
|---|---|
| **A** | a proposta original, sem alterações |
| **B** | a proposta ponderada, sem alterações |
| **C** | a ponderada com o piso do `minPrice` original em todos os itens |
| **D** | a ponderada com o piso só nos itens de evidência fraca (0-1 anúncios, divergência ≥75 %, procura não medida) |

| ficheiro | o que é |
|---|---|
| `reports/pricing_groups/token_rush_guard.md` | a semântica real do TSM (lida no addon), as alternativas, o efeito no capital, a classificação de liquidação, os 25 itens mais susceptíveis, a configuração de teste e a reversão, e a recomendação |
| `reports/pricing_groups/guard_alternatives.csv` | as 328 linhas × 50 colunas: min/normal/max e a decisão de postagem em cada alternativa, as % face à original, a classe de liquidação e as razões |

**Semântica verificada no TSM instalado** (`AuctioningOperation.lua`,
`PostScan.lua`): o `minPrice` é uma **porta**, não um preço — com concorrência o
TSM posta `parede − undercut`; se a parede ficar abaixo do piso e
`priceReset = none`, o item **não é posted** (`NOT_POSTING.BELOW_MIN`); com
`cancelUndercut`/`cancelRepost` a false **o TSM nunca cancela**; e **não existe
repost automático** (o anúncio expira e o *próximo* post scan re-avalia). Se o
`normalPrice` ficar abaixo do `minPrice`, a operação fica **inválida** e o item
deixa de ser posted de todo — é por isso que o guard eleva o `normalPrice` com o
piso.

### Validação do preço ponderado (`validate_weighted_pricing`)

```bat
python scripts/validate_weighted_pricing.py --write
```

Audita a proposta ponderada **contra a original**, sem importar nada e sem mexer
em nenhuma das duas.

| ficheiro | o que é |
|---|---|
| `reports/pricing_groups/validation_report.md` | o relatório: auditoria da expressão (precedência, `ifgt`/`check`, sentinel, valores em falta, extremos), auditoria do peso, comparação pareada, o que falta para medir ouro, teste de importação e reversão, riscos e recomendação |
| `reports/pricing_groups/weighted_vs_original.csv` | a comparação item a item (328 linhas): local, regional, histórico, profundidade, frescura, dispersão, peso, referência original, referência ponderada, variação, confiança e avisos |

As verificações correm contra a **expressão gerada** (não contra o modelo), e
a comparação é feita a nível de expressão: o que o TSM calcularia, com os
valores crus (sentinel incluído).

Resultado actual: **14 verificações, todas PASS**. O preço pedido **sobe** em
71 itens e **desce** em 53; a mediana da variação é 0 %. O subconjunto
comparável de vendas (n=25, sem datas) dá um erro mediano ligeiramente pior
para a ponderada (3,53 contra 3,18) — **não decide nada**, e o relatório
diz isso.

### Grupos de preço do inventário (`pricing_groups`)
```bat
python scripts/pricing_groups.py                    REM dry run: bandas, grupos, evidência
python scripts/pricing_groups.py --write            REM relatório + CSV + proposta de TSM
python scripts/pricing_groups.py --write --realm Argent Dawn
```

Descobre os **grupos de comportamento** do inventário de transmog e as suas
estratégias, e propõe os grupos e operações de TSM correspondentes. Lê
`tsm_markets.db`, `C:\economics\woweconomy\dfa\dfa.db` (só leitura) e o
inventário. **Nada é escrito na base de dados nem no TSM**, e a string proposta
nunca é importada.

| ficheiro | o que é |
|---|---|
| `reports/pricing_groups.md` | a análise: bandas e a sua medição, a política separada da estatística, os grupos, o teste de distinção, as estratégias, as limitações |
| `reports/pricing_groups/band_evidence.csv` | a medição por banda (clearance E1/E2, decay 78d, parede/ask, velocidade) |
| `reports/pricing_groups/groups.csv` | os grupos, as suas regras e as fórmulas |
| `reports/pricing_groups/membership.csv` | inventário → grupo |
| `reports/pricing_groups/tsm_proposal.md` + `pricing_groups_proposal.tsm` | a proposta de grupos e operações (string para `Groups → Import` num perfil de teste) |

As regras do motor (`woweconomy/analytics/pricing_groups.py`):

- **Banda ≠ grupo.** A banda é o nível de preço e os seus coeficientes são
  estimados **apenas** da banda. O grupo é comportamento (procura potencial
  medida, comportamento do ask em 78 dias) e pode trazer um ajuste
  *separado*; sem evidência de venda própria, o ajuste é **1,00 heurístico** e
  a classificação continua a existir sem mexer no preço.
- **Duas estimativas de clearance, separadas.** E1 = média de venda regional ÷
  parede local (amostra grande, mas com snipes: é um limite inferior). E2 =
  venda da própria conta ÷ parede **na data da venda** (a medida honesta) —
  exige vendas datadas, que o Journalator não exporta: a máquina está lá
  (`pricing_groups.estimate_e2`), o resultado é "indisponível" e a parede
  actual **não** é usada em seu lugar.
- **Política ≠ estatística.** Os pisos (`normal ≥ 0,15`, `min ≥ 0,08`) são
  decisões, com o seu nome e a sua origem no código.
- **O factor do reino não é contado duas vezes:** a âncora é local
  (`dbmarket`), logo o factor local/regional vive no export por reino e na
  camada de campanha, aplicado uma vez.

### Token Rush — liquidar inventário com prazo (12 dias)

```bat
python scripts/token_rush.py                                   REM dry run: fila e défice
python scripts/token_rush.py --write                           REM relatório + rush_queue.csv
python scripts/token_rush.py --write --export --flipqueue      REM + imports TSM e listas FlipQueue
python scripts/token_rush.py --feedback                        REM o que foi realmente vendido
python scripts/token_rush.py --days-remaining 6 --gold 250000  REM simular o meio da campanha
```

Lê `inventory.csv`, o estado local de cada reino (`tsm_realm_price`), a referência
regional (`tsm_region_price`), a contagem de anúncios (`tsm_market`), o histórico
próprio de vendas e expirações (`journal_stats`) e o perfil medido do reino
(`mog_realm`). **Nada é posto automaticamente**: o comando só escreve ficheiros.

Saídas:

| ficheiro | o que é |
|---|---|
| `reports/token_rush/token_rush.md` | o relatório: estado da campanha, postura por reino, fila, marcos dos dias 3/6/9/12, o que é dado e o que é heurística, bloqueios |
| `reports/token_rush/rush_queue.csv` | a fila, uma linha por item×reino, com perfil, evidência, preço, piso, líquido e avisos |
| `reports/token_rush/flipqueue_<reino>.csv` | lista para o FlipQueue (Import → colar): `Sell Price` é o preço recomendado, `Sell Realm` é o reino alvo |
| `reports/mog_groups/liquidity_mogs_groups_<reino>.tsm` | import do TSM **um grupo e uma operação por item**, com o preço recomendado, o piso e o tecto |
| `reports/token_rush/feedback.md` | preço recomendado vs preço a que vendeu, e a taxa de venda observada por item |

O mesmo import pode ser reconstruído a partir da fila, sem voltar a correr o
modelo:

```bat
python scripts/tsm_groups_export.py --liquidity-queue reports/token_rush/rush_queue.csv --write
python scripts/tsm_groups_export.py --liquidity-queue reports/token_rush/rush_queue.csv --liquidity-realm Uldaman --write
```

As regras do motor (`woweconomy/analytics/token_rush.py`):

- **perfil do item** (A líquido / B procura provada / C especulativo / D estoque
  parado / U desconhecido) sai de evidência: vendas e expirações da própria
  conta, e nunca do preço. Sem evidência, o item fica `unknown`; um `dbmarket`
  alto não é procura; um anúncio que desapareceu não é uma venda.
- **postura do reino** é uma dimensão separada: nível local face à região,
  pressão de concorrência e avisos de qualidade de dados (reinos com métricas
  idênticas são o mesmo mercado). Pouca concorrência não é prova de procura.
- **preço de rush** = referência local × banda de undercut (pela concorrência) ×
  factor do item (pela evidência) × factor de prazo (pelo calendário), com piso
  (valor de venda ao fornecedor e limite de perda) e tecto (a referência).
- **fila** ordenada por contribuição para o défice por hora de gestão. O valor
  esperado só aparece quando há histórico de venda do próprio item; caso
  contrário a linha é marcada *heurística* e a ordem usa um peso de evidência,
  não uma probabilidade.

### Ouro endereçável por comprador (`hungry_buyers`)

```bat
python scripts/hungry_buyers.py                        REM ouro por comprador, por reino
python scripts/hungry_buyers.py --min-score 1050000 --top 15
python scripts/hungry_buyers.py --boe-only --sort items
python scripts/hungry_buyers.py --details Ravencrest
python scripts/hungry_buyers.py --csv reports/hungry_buyers.csv
```

Lê os bitmaps `owned` da `dfa.db`, conta o que falta e junta o preço do **mesmo grupo connected**
(`minBuyout` > `marketValue` > `marketValue` regional). `itens/comprador` mede saturação,
`ouro/comprador` mede o ouro endereçável; `cobertura` diz que fracção dos itens em falta tem preço.

### Que item falta, item a item

```bat
python scripts/item_demand.py                          REM top por ouro em falta (600k)
python scripts/item_demand.py --min-score 600000 --top 30
python scripts/item_demand.py --min-score 1000000 --sort wanters
python scripts/item_demand.py --min-rate 0.5 --min-wanters 100
python scripts/item_demand.py --where --top 10         REM onde vender cada item
python scripts/item_demand.py --where --where-top 95 --where-csv reports/item_demand_realms.csv
python scripts/item_demand.py --csv reports/item_demand.csv
```

- As linhas são do **item** (o que se compra), com a aparência como coluna secundária: um item
  barato pode ser o que concede uma aparência cara. Fica o item mais barato com preço.
- Filtra `catalogue.obtainable=1`, `item_id>0`, `era=0`, preço regional > 0, e fontes
  vendor/Trading Post/quest/profissão.
- **BUG CONHECIDO — o filtro de binding está invertido.** O código usa `i.binding=1`, que a
  autoridade do projecto (`item_binding.py`: DB2 Bonding=1 = ON_ACQUIRE) lê como **BoP**; BoE é
  `binding=2` (é o que o `sale_probability.py` usa). Resultado: as 95 linhas actuais de
  `reports/item_demand.csv` são todas BoP — peças que nunca chegam à AH — e é essa lista que o
  `--where`, o `realm_48h` e o `compare_inventory` consomem. Consulta pontual fiável de uma peça:
  `collector_demand item <id>`. Ver também `reports/bop_priced.csv`.
- `--mode item` (default) conta `wanters` como **união** de quem falta uma das aparências do item;
  `--mode appearance` dá uma linha por aparência. `--where` cruza wanters de cada reino com o
  preço do grupo connected.

O limiar é um botão (`--min-score`, default **600k**; o máximo da amostra é 1 215 127):

| `--min-score` | contas | itens | topo (want_rate) |
|---:|---:|---:|---:|
| 0 | 6 220 | 181 | 95,2 % |
| **600k** | **3 130** | **95** | **91,4 %** |
| 1M | 760 | 56 | 83,8 % |

A 600k a lista inclui raid caros (`Deepvine Waistguard` a 9,6M); a 1M fica só com world drops
que um coleccionador de catálogo cheio ainda não tem. O CSV tem de ser regerado com o mesmo limiar
que o relatório usa.

### Cruzar com o teu inventário

```bat
python scripts/compare_inventory.py                      REM descobre o CSV sozinho
python scripts/compare_inventory.py inventory.csv
python scripts/compare_inventory.py --inventory inventory.csv --csv reports\inventory_vs_demand.csv
python scripts/compare_inventory.py --by gold --show-extra --top 100
python scripts/compare_inventory.py --no-names

python scripts\compare_inventory.py --realms
python scripts\compare_inventory.py --realms --top 100 --per-item 3 --realms-top 20
python scripts\compare_inventory.py --realms --min-score 600000 --realms-csv reports\inventory_realms.csv
```

- Sem argumentos procura `inventory.csv`, `inventario.csv`, `scripts\inventory.csv`,
  `scripts\inventario.csv`, `reports\inventory.csv`, `reports\inventario.csv` (a 1.ª que existir;
  o caminho sai na 1.ª linha). Nenhuma → erro com a lista de sítios, não traceback de `open`.
- Aceita o export do TSM (`;` ou `,`) e junta por **ItemID base** ao `reports/item_demand.csv`.
  Três blocos: **tens** (qty, stock a preço regional, cobertura qty/wanters), **em falta**
  (lista de compras) e **fora da lista** (`--show-extra`).
- Nomes: no bloco `tens` é o do teu export; onde vem `Unknown`/vazio vai ao `db2_items` buscar o
  real (`--no-names` desliga).
- `--realms` abre os bitmaps de aparências, mapeia cada item às suas aparências
  (`db2_item_appearance`) e conta por reino as contas a quem falta pelo menos uma (união):
  tabela **por reino** e tabela **por item** (melhor reino, wanters, fatia do reino, anúncios,
  **compradores por anúncio**, vendas/dia da região). CSV: `realm, item_id, wanters, accounts,
  share, auctions, per_listing, price_copper, sale_rate, sold_per_day`.

`realm_gaps` faz o mesmo para os itens da lista de demanda (lê o `--where-csv`), com `--buy N` a
juntar os N reinos mais baratos por item.

```bat
python scripts/realm_gaps.py --buy 3
```

**Não mistures grandezas:** no `--where`, `(reino)` é o **listing mais barato** e `(região)` é o
**market value**; o `realm_gaps --buy` mostra as duas lado a lado (`mercado` e `listing`).

### Catálogo BoE (`boe_targets`, este sim binding=2) e relatório de reinos

```bat
python -m woweconomy.analytics.boe_targets
python -m woweconomy.analytics.boe_targets --faltam --only-priced --sort valor
python -m woweconomy.analytics.boe_targets --top 50 --no-csv
```

`dfa_report` gera o relatório de reinos + mercados a partir das **duas** bases (`dfa.db` e
`tsm_markets.db`) — é o texto de `reports/realm_report_<data>.md`, escrito por código:

```bat
python -m woweconomy.analytics.dfa_report
python -m woweconomy.analytics.dfa_report --min-score 70 --depth 100 --out reports\realm_report.md
python -m woweconomy.analytics.dfa_report --dfa-dir "C:\economics\woweconomy\dfa" --csv reports\mercados.csv
```

Sem `--out` escreve `reports\realm_report_<data>.md` mas **nunca substitui um ficheiro que não
tenha escrito**: se lá estiver o relatório manual, escreve `realm_report_<data>.auto.md` ao lado.
Com `--out` o nome é teu.

**Dois `score` diferentes — não os troques:**

- `collector_demand --min-score N` corta pelo **valor DfA do item** (0–100 = falta × raridade),
  o único score por item nos CSV de demanda.
- O score **do coleccionador** (milhões) só existe no scanner (`dataforazeroth scan --min-score
  800000`), porque a demanda já vem agregada: os ficheiros guardam contagens, não quem.

---

## 7. Ranking de farms

```bat
python -m woweconomy.analytics.farm_ranking woweconomy/wowhead_zones/zone_331.json --db tsm_markets.db
python -m woweconomy.analytics.farm_ranking woweconomy/wowhead_zones/zone_331.json woweconomy/wowhead_zones/zone_719.json woweconomy/wowhead_zones/zone_16.json --db tsm_markets.db --blizz-db tsm_markets.db --top 20 --out FarmReport.txt
python -m woweconomy.analytics.farm_ranking woweconomy/wowhead_zones/zone_331.json --sort jackpot --stops 4 --kpm 25
python -m woweconomy.analytics.farm_ranking woweconomy/wowhead_zones/zone_331.json --session 15 30 60 --travel-sec 90 --mog-kills 600
python -m woweconomy.analytics.farm_ranking woweconomy/wowhead_zones/zone_331.json --no-era-filter   REM mantém preços era-Classic
python -m woweconomy.analytics.farm_ranking woweconomy/wowhead_zones/zone_331.json --price-source legacy   REM tabela market_snapshots congelada
python -m woweconomy.analytics.farm_ranking woweconomy/wowhead_zones/zone_331.json --price-source csv   REM csv/items.csv

python scripts/relocate.py --top 20 --min-ev 500          REM melhor spot por item
python scripts/plan_route.py --zone 331 --stops 5 --minutes 60
python scripts/plan_route.py --zones woweconomy/wowhead_zones/zone_331.json woweconomy/wowhead_zones/zone_16.json --stops 8 --kpm 20
python scripts/find_drop.py "Ruffled Chaplet"
python scripts/find_drop.py 14047 --limit 30 --db tsm_markets.db
python scripts/find_mob.py "Ashenvale Bear"
```

`--horizon` continua aceite mas é **ignorado** (DEPRECATED): `sale_rate` é sell-through por
listagem, não probabilidade diária.

---

## 8. Zonas: scrape e reparação

```bat
python -m woweconomy.services.wowhead "zone=331/ashenvale"
python -m woweconomy.services.wowhead "zone=331/ashenvale" "zone=16/azshara" all
python -m woweconomy.services.wowhead "zone=331/ashenvale" all no-cache   REM also: all-npcs | headless | <página>
python scripts/backfill_spawns.py                     REM spawns (#locations) de todos os zone_*.json
python scripts/backfill_spawns.py woweconomy/wowhead_zones/zone_16.json
python scripts/repair_zone.py woweconomy/wowhead_zones/zone_331.json
python scripts/repair_zone.py woweconomy/wowhead_zones/zone_331.json --db tsm_markets.db
```

---

## 9. Itens: DB2, binding, enriquecimento

```bat
python scripts/import_db2.py
python scripts/import_db2.py --build 12.1.7.70424
python scripts/import_db2.py --stats
python scripts/item_binding.py                       REM auditoria -> reports/
python scripts/item_binding.py 229155
python scripts/item_binding.py "Worn Shortsword"

python scripts/enrich_items.py                       REM Blizzard: quality/binding/sellPrice
python scripts/enrich_items.py woweconomy/wowhead_zones/zone_16.json --db tsm_markets.db --limit 500
python scripts/enrich_inventory.py inventory.csv --db tsm_markets.db --rps 8
```

`enrich_*` precisa de `.env` com `BLIZZARD_CLIENT_ID` / `BLIZZARD_CLIENT_SECRET`; é resumível
(`--rps` limita, `--limit 0` = tudo).

---

## 10. Diário de vendas

```bat
python scripts/journal.py import
python scripts/journal.py report
python scripts/journal.py report --realm silvermoon --min-sales 5 --brief
python scripts/journal.py stats
python scripts/journal.py import-csv journal.csv
python scripts/journal.py log-sale   --item "Wool Cloth" --price 5 --qty 99
python scripts/journal.py log-post   --item 1351 --price 12632
python scripts/journal.py log-fail   --item 1351 --note "expirou 48h"
python scripts/journal.py log-cancel --item 1351
python scripts/journal.py log-buy    --item 124640 --price 800 --qty 3
```

### 10.1 DFA Auction Intelligence (Addon + Ingestão Empírica)

O sistema de inteligência empírica de leilão do DFA captura eventos atómicos observáveis
(vendas, compras, publicações, cancelamentos, expirações) com timestamps UNIX precisos,
decomposição de taxas (comissão de 5% da AH) e proveniência auditável.

```bat
REM Ingestão automática (detecta Journalator.lua e DFA_AuctionIntelligence.lua no WTF)
python scripts/auction_intelligence.py import
python scripts/auction_intelligence.py import --journalator "C:\...\SavedVariables\Journalator.lua"
python scripts/auction_intelligence.py import --dfa-addon "C:\...\SavedVariables\DFA_AuctionIntelligence.lua"

REM Estatísticas agregadas e métricas de desempenho
python scripts/auction_intelligence.py stats
python scripts/auction_intelligence.py stats --realm Dragonmaw

REM Relatório completo de inteligência em Markdown
python scripts/auction_intelligence.py report
python scripts/auction_intelligence.py report --out reports/auction_intelligence_report.md
python scripts/auction_intelligence.py report --realm Dragonmaw --out reports/auction_intelligence_dragonmaw.md

REM Exportação estruturada para JSONL
python scripts/auction_intelligence.py export --out reports/auction_intelligence_sample.jsonl
python scripts/auction_intelligence.py export --out reports/events_recent.jsonl --since 1792000000 --realm Silvermoon

REM Validação rigorosa de integridade e conformidade de schema
python scripts/auction_intelligence.py validate reports/auction_intelligence_sample.jsonl

REM Ingestão de ficheiro JSONL externo
python scripts/auction_intelligence.py import-jsonl data/events.jsonl
```

#### Parâmetros e Opções (`auction_intelligence.py`)

- `--db <caminho>`: base SQLite alvo (default: `tsm_markets.db`).
- `import`:
  - `--journalator <path>`: caminho explícito para `Journalator.lua` (descompacta arquivos via Lupa/LibDeflate).
  - `--dfa-addon <path>`: caminho explícito para `DFA_AuctionIntelligence.lua`.
- `export`:
  - `--out <path>`: ficheiro JSONL de saída (**obrigatório**).
  - `--since <unix_ts>`: filtra eventos com timestamp superior ou igual.
  - `--realm <nome>`: filtra por reino específico.
- `validate <file>`: verifica integridade de JSON, validação de schema, tipos e detecta duplicados.
- `stats` / `report`:
  - `--realm <nome>`: filtra métricas para um reino específico.
  - `--out <path>`: grava o relatório em markdown.

#### Addon WoW: `DFA_AuctionIntelligence`

- **Instalação**: `World of Warcraft\_retail_\Interface\AddOns\DFA_AuctionIntelligence`.
- **SavedVariables**: `DFA_AUCTION_INTELLIGENCE_DB`, `DFA_AUCTION_INTELLIGENCE_EXPORT`.
- **Modos de operação**:
  - *Com Journalator*: Conecta-se à API de arquivo, faz backfill histórico e intercepta adições em tempo real.
  - *Sem Journalator (Fallback)*: Monitoriza hooks nativos da Blizzard (`C_AuctionHouse.PostItem`/`PostCommodity`, `MAIL_INBOX_UPDATE`, `CLOSE_INBOX_ITEM`).
  - *Com TSM*: Registra automaticamente a referência `dbmarket` e operação activa no momento exacto da publicação.
- **Comandos no jogo**:
  - `/dfaai status` (ou `/dfa status`): estado da integração, versão e contagem de eventos guardados.
  - `/dfaai diag`: diagnóstico de qualidade de dados, cobertura e campos em falta.
  - `/dfaai export`: força a actualização imediata do payload JSONL nas SavedVariables.
  - `/dfaai sync`: força nova sincronização com o arquivo do Journalator (diz quantas
    linhas leu em cada secção, quantas eram novas e quantas já estavam guardadas).
  - `/dfaai status`: mostra também o **último sync** (`invoices/postings/failures`,
    duplicados e erros) — é a prova do que o arquivo tinha.
  - `/dfaai kills`: kills, carcaças lootadas e cobre por NPC desde o último export.
  - `/dfaai mail`: lê as facturas da mailbox que estiver aberta, agora.
  - `/dfaai mail auto`: volta a ler a mailbox ao abrir; **desligado por omissão**.

> A 1.6.1 deixou de fazer hook ao `OpenAllMail` e de ler a mailbox nos eventos
> dela. Neste cliente isso era respondido com **"action blocked by Blizzard"**
> mal se entrava no jogo, e a mailbox ficava inutilizável. Valores marcados
> como *secret* (`issecretvalue`) passam a ser ignorados — loot, combat log e
> facturas — em vez de rebentarem dentro do handler. O CSV/invoice linking do
> backend continua a usar `mail_jsonl` quando o `/dfaai mail` corre.

#### ZoneTimerRedux: o ouro por zona vem do motor de preço

O `ZoneTimerRedux` (1.3.0) deixou de mostrar um único número. Passa a separar:

- **Cash** — o dinheiro apanhado (`CHAT_MSG_MONEY`), que é o que o `ZoneGoldDB`
  sempre guardou. É o número que dispara os marcos de ouro.
- **Loot** — `vendor / ~market`: o valor de venda a vendor (tabela do cliente,
  a mesma que o dataset traz) e a estimativa de mercado **líquida do corte de
  5%**, marcada com `~`. Um item sem preço conta como `unpriced`, nunca como 0.

As duas linhas nunca se somam. O preço vem do dataset do `DFA_Estimate`
(`DFA_ESTIMATE_API.GetLootValues`): primeiro o registo de vendas do reino,
depois o preço regional líquido × o factor do reino; sem nenhum dos dois, não há
estimativa. O addon não fala com o TSM nem recalcula preços. Um dataset mais
velho do que `maxAgeHours` (o limite do próprio motor, 14 dias) é mostrado como
**stale**, e não é escondido.

```text
/zt gold    # cash, vendor e market (~) por zona, com o valor por hora
/ztt        # a tally; o tooltip de cada linha e o total repetem a divisão
```

O loot é lido uma só vez, pelo `DFA_AuctionIntelligence` (`Kills.lua`), que o
atribui à carcaça e ao NPC e o exporta para o `intel.sqlite` (fonte do ML). O
`ZoneTimerRedux` subscreve esse feed (`RegisterLootListener`) e guarda apenas as
quantidades por zona; o cobre da janela de loot **não** entra no cash, porque o
`CHAT_MSG_MONEY` já o reporta. As kills por NPC vêm da API documentada do
MobInfo2 (`MI2_GetMobData`), usada só como denominador do ouro por kill — o
SavedVariables dele continua a não ser lido pelo backend.

#### Sincronização e gravação ao sair do jogo (v1.1.0)

O cliente escreve as SavedVariables **depois** de `PLAYER_LOGOUT`: o que o addon fizer
nesse handler é o que fica em disco. Desde a 1.1.0 o handler faz, por esta ordem:

1. `SyncAll("logout")` — lê as três secções do Journalator (`Invoices`, `Posting`,
   `Failures`) e guarda o que ainda não estava na BD;
2. `GenerateExport()` — reconstrói `DFA_AUCTION_INTELLIGENCE_EXPORT` com todos os
   eventos e com `last_sync`;
3. os dois passos vão dentro de `pcall`: uma falha num não pode custar o outro.

Antes disto, duas coisas faziam o sync morrer em silêncio: a tabela `stats` de uma
versão antiga não tinha `by_type` e **cada** `StoreEvent` indexava `nil` (o arquivo era
lido e deitado fora — daí só haver vendas antigas e zero listagens); e o addon não
voltava a ler o arquivo no logout, por isso uma venda do fim da sessão só aparecia no
login seguinte.

```bat
REM o addon escreve SavedVariables\DFA_AuctionIntelligence.lua (nome da PASTA, não da variável)
REM importar para a base: tabela própria auction_intel_events (auction_events é do Journalator)
python scripts/dfa_ai.py --status
python scripts/dfa_ai.py --import
python scripts/dfa_ai.py --measure

REM o teste do addon: corre o ciclo real em Lua (lupa) e falha se algo se perder
python scripts/auction_intel_harness.py "C:\FarmAnalysis\DFA_AuctionIntelligence"
python -m pytest -q tests/test_auction_intel.py tests/test_auction_intel_addon.py
```

- As **faturas** do Journalator não trazem `itemLink` (o próprio pipeline o diz em
  `woweconomy/ml/journalator.py`), logo o export não tem `itemId`. O importador resolve
  o id pelo **nome**, contra `items` / `tsm_realm_price` / `tsm_region_price`, e diz
  quantos resolveu.
- O `--status` só olha para o export do addon; o `auction_intelligence.py` da secção
  10.1 lê o import do Journalator/TSM (`auction_events`). São dois caminhos distintos.

#### Correções da 1.1.1 (Journalator 171 / Auctionator 341)

Dois erros reais, ambos visíveis no `sync_log` guardado em
`SavedVariables\DFA_AuctionIntelligence.lua`:

1. **Erro no chat a cada venda arquivada** — *All sources must be registered
   (journalator_logs_updated)*. O Journalator 171 registra **o objecto-função**
   `Journalator.AddToLogs` como fonte do `Auctionator.EventBus` e chama
   `EventBus:Fire(Journalator.AddToLogs, ...)`. O `hooksecurefunc(Journalator,
   "AddToLogs", ...)` substitui esse campo por uma função nova, logo o `Fire`
   procurava uma fonte que nunca foi registada. A 1.1.1 deixa de fazer hook quando
   há EventBus e passa a ouvir `Journalator.Events.LogsUpdated`; sem EventBus,
   faz hook e volta a registar a função substituída como fonte.
2. **Sync de logout sempre a zero** — *Archivist.lua:395: attempt to index a nil
   value* nas três secções. O `Archivist:DeInitialize` corre no `PLAYER_LOGOUT` e
   faz `archive.sv = nil`; quando corre antes do nosso handler, `GetRange` já não
   consegue abrir o arquivo. A 1.1.1 lê `Journalator.State.Logs` (ainda em memória)
   nesse caso e regista as secções em `last_sync.fallback`.

3. **Referência de mercado sempre a zero** — `marketRefCopper = 0` nas 3 285
   listagens, mesmo com o TSM instalado. O `TSM_API.GetCustomPriceValue` valida o
   segundo argumento com `^[ip]:%d+` e **falha** num hyperlink; o `pcall` do addon
   transformava isso num `nil` silencioso. Passou a passar `i:<itemId>`; e, como as
   faturas do Journalator não trazem link nenhum, o id é pedido ao cliente pelo nome
   (`C_Item.GetItemIDForItemInfo`), senão a venda nunca podia ser comparada com o
   mercado nem emparelhada com a listagem.

O `--measure` lia o **export**, onde as vendas não têm `itemId` (as faturas não
trazem link), e por isso dizia sempre "sem pares listagem->venda" com 3 285
listagens na mão. Passou a medir a tabela importada, que é onde o `--import`
resolve os ids pelo nome: 163 pares, mediana 3,8 h.

O harness cobre os três: modela o `EventBus` do Auctionator (com a verificação de
fonte), desliga o `GetRange` antes do logout e valida o argumento do TSM como o
addon real. A cópia anterior falha 8 de 18 verificações (incluindo o erro exacto do
cliente); a 1.1.1 passa 18/18.

O teste de carga do `DFA_Estimate` disparava `frame.scripts["ADDON_LOADED"]`, um
script que o cliente nunca chama: o addon registra `SetScript("OnEvent")` e recebe
`(frame, event, addonName)`. O teste passou a disparar o script `OnEvent`, como o
cliente faz — as duas falhas eram do teste, não do addon. O `/dfaai status` passou a mostrar
`capture: eventbus` (ou `hook`) para se ver qual o caminho activo.

#### Ficheiros e Documentação Gerados

| Ficheiro | Conteúdo |
|---|---|
| `reports/auction_intelligence_report.md` | Relatório executivo: vendas realizadas, comissões pagas, depósitos perdidos, sell-through rate real e itens com atrito |
| `reports/auction_intelligence_sample.jsonl` | Linhas JSONL estruturadas e validadas com todos os 5.992 eventos empíricos da base |
| `reports/auction_intelligence_provenance.md` | Diagrama de fluxo de dados, proveniência e cadeia de custódia das medições |
| `reports/auction_intelligence_schema.md` | Especificação completa do schema de eventos (versão 1) |
| `reports/auction_intelligence_coverage.md` | Matriz de cobertura: eventos verificados, inferidos e limitações de API |
| `reports/auction_intelligence_limitations.md` | Limitações conhecidas (FIFO matching, transacções de terceiros) e logging futuro |


---

## 10.2 Motor de aprendizagem CatBoost (`woweconomy.ml`)

O motor aprende com o que a conta **realmente** fez: o arquivo do Journalator
(publicações, faturas, falhas) cruzado com o histórico de snapshots da TSM e com
os contadores de demanda do `dfa.db`. Não inventa histórico: cada linha de
treino só usa o estado do mercado que existia **no momento da publicação**.

**Princípio:** *Listings mostram o que o vendedor quer receber; transacções
mostram o que alguém pagou.* `dbmarket` e `dbregionmarketavg` são referências
de **pedido** — entram como *features*, nunca como etiquetas.

```bat
REM Relatório de prontidão dos dados (o que existe, o que falta, o que é treinável)
python scripts/dfa_ml.py audit

REM Treino + validação walk-forward + modelos finais + relatório
python scripts/dfa_ml.py train

REM Recomendação de preço para um item
python scripts/dfa_ml.py recommend --realm Dragonmaw --item-id 2318 --qty 20 --horizon 24

REM Atualizar primeiro o arquivo do Journalator (sempre "fetch updated")
python scripts/dfa_ml.py train --refresh-journal

REM Check rápido da cablagem (45 s em vez de 5 min) — só para wiring
python scripts/dfa_ml.py train --iterations 20
```

#### Os quatro modelos

| Modelo | Alvo | Validação |
|---|---|---|
| **A — preço realizável** | ouro líquido por unidade (log), de vendas observadas | walk-forward + dia de teste intocado: rmse_log, factor mediano do erro |
| **B — probabilidade de venda** | `sold_within_24h/72h/168h/336h` | AUC, Brier, log loss; **censurados e cancelados ficam de fora** |
| **C — tempo até venda** | horas até a venda, censurada à direita (`SurvivalAft`) | concordance index |
| **D — estratégia de preço** | não é um modelo: decide o preço a partir de A + B + prazo | replay walk-forward + contrafactual sobre a curva de demanda medida |

#### Guardas contra fuga de informação

- A **ligação** entre publicação e desfecho usa o preço: a conta publica só a
  preço de compra, logo `valor == buyout × quantidade` identifica a listagem
  exacta. A taxa de correspondência exacta é medida e reportada (99.7%).
- Uma listagem **sem desfecho observado** fica *censurada* — nunca é contada
  como falhada.
- Um **cancelamento** é o vendedor a mudar de ideias: não é evidência de
  falta de demanda e não entra nas etiquetas.
- A divisão é sempre **cronológica** (janelas de treino → validação → teste
  intocado). Nunca `train_test_split` aleatório sobre snapshots correlacionados.
- O dia de validação **não escolhe** o modelo (sem early stopping), senão a
  pontuação seria medida nos dados que escolheram o modelo.
- A **demanda do DfA** é um agregado sem relógio: está marcada como *não
  point-in-time* e o relatório mostra o que muda ao incluí-la.
- O `ask` é excluído do modelo de preço por construção (`ASK_FEATURES`), para
  o modelo não aprender a ler o próprio preço pedido.

#### O que o motor descobriu

- A **curva de demanda medida**: a taxa de venda é plana até ~1.4x a
  `dbregionsaleavg` e colapsa a partir de 2x. O ouro esperado por listagem
  tem o pico em **1.15x–1.4x a `dbregionsaleavg`**.
- A conta pede, em mediana, **4x a `dbregionsaleavg`** — e essas listagens
  quase não vendem (2.6%).
- `dbmarket` e `dbregionmarketavg` prometem ~30% acima do que os compradores
  pagam; `dbregionsaleavg` e `dbrecent` são comercialmente defensáveis.
- Quando a referência de pedido se separa da referência de transacções, a
  taxa de venda cai de 74% para 1.4% — o mecanismo, medido.

#### Opções

- `--tsm-db` / `--dfa-db`: bases alternativas.
- `--folds N` / `--test-fraction F`: desenho da validação walk-forward.
- `--iterations N`: número de árvores por modelo (para checks rápidos).
- `--availability-clock {updated,fetched}`: `updated` usa o carimbo do próprio
  TSM (quando o mercado era verdade); `fetched` usa a hora de download (visão
  pessimista, sem fuga). O padrão é `updated`.
- `--no-dfa-demand`: desliga as features de demanda.
- `--refresh-journal`: relê o `Journalator.lua` e actualiza `auction_events`.

#### Ficheiros gerados

| Ficheiro | Conteúdo |
|---|---|
| `reports/dfa_ml/dfa_ml_data_readiness.md` | Prontidão dos dados: schemas reais, cobertura, ausência, modelos treináveis |
| `reports/dfa_ml/dfa_ml_report.md` | O que o motor aprendeu, com os números e os limites |
| `reports/dfa_ml/dfa_ml_report.json` | Os mesmos números, em JSON |
| `models/dfa_ml/*.cbm` | Os modelosCatBoost treinados |


---

## 10.3 Preço DfA dentro do TSM (`DFAEstimate`)

Motor de preço independente que responde **por reino**: estimativa do preço
realizável, probabilidade de venda, tempo até a venda (quando os dados o
sustentam), recomendação, confiança e evidência.

**Princípio:** *os listings mostram o que o vendedor quer receber; as
transacções mostram o que alguém pagou.* A referência de pedido
(`dbmarket`, `dbregionmarketavg`) é **entrada**, nunca etiqueta. O
interesse do DfA (aparências) é **evidência**, nunca procura.

#### Onde vive

| Peça | Onde | O que faz |
|---|---|---|
| Motor de aprendizagem | `woweconomy/ml/` | aprende das transacções observadas |
| Motor de preço | `woweconomy/pricing/` | uma resposta explicável por (reino, item) |
| Export | `scripts/dfa_pricing_export.py` | escreve `DFA_Estimate/Data/DFA_Estimate_Data.lua` |
| Addon de dados | `DFA_Estimate/` | lê o ficheiro gerado, publica `DFA_ESTIMATE`, mostra tooltip e painel |
| Fork do TSM | `DFA_TSM/` | regista a fonte de preço `DFAEstimate` |
| Patcher | `scripts/dfa_tsm_patch.py` | instala, verifica e remove o fork |

#### Comandos

```bat
REM Gerar o dataset (determinístico, versionado) + relatório + validação
python scripts/dfa_pricing_export.py --report --validation

REM Fork do TSM
python scripts/dfa_tsm_patch.py verify     # o TSM tem as APIs de que o fork precisa?
python scripts/dfa_tsm_patch.py apply      # instala (faz backup do TOC)
python scripts/dfa_tsm_patch.py status
python scripts/dfa_tsm_patch.py revert     # desfaz (restaura o TOC)

REM Instalar o addon de dados
xcopy /Y /I C:\FarmAnalysis\DFA_Estimate "C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns\DFA_Estimate"
```

#### A fonte `DFAEstimate`

- Registada por `CustomString.RegisterSource("DFA", "DFAEstimate", ...)` —
  a API **interna** do TSM 4.14.78, verificada antes de o fork ser escrito.
  Não é uma API pública: `TSM_API` não a expõe, e é por isso que a
  integração é um **fork** e não um addon externo.
- Usa-se **a nu** em qualquer custom string do TSM: `dfaestimate`.
  A linguagem de custom strings do TSM **não tem chavetas** e o
  identificador tem de ser só letras — `{dfaestimate}` é inválido.
- Devolve um valor **positivo em cobre** ou `nil`. Nunca `0`, nunca um
  número inventado.
- **Não faz** post, cancel, undercut, repricing, nem escreve em
  settings, grupos, perfis ou custom strings do TSM.
- Respeita o reino: `SessionInfo.GetRealmName()`, com a mesma sanitização
  que o TSM usa. Reinos do mesmo grupo ligado (Dragonmaw/Spinebreaker)
  partilham a casa de leilões e o dataset guarda o mapeamento de **todos**
  os reinos que o TSM conhece, não só os que têm registos.
- **Cobertura:** os registos por reino existem só para os itens que a conta
  negociou nesse reino (`realms[reino].items`). Todo o resto é respondido
  pelo modelo (referências regionais, iguais em todos os reinos) quando o
  TSM tem dados vivos para o item; sem nenhuma referência viva, a fonte usa
  a tabela `prices` congelada no build (31 184 itens na última corrida).
  Portanto `dfaestimate` pode responder num reino onde a conta nunca
  vendeu, mas um item fora das referências regionais continua sem
  estimativa — nil, não zero.

#### O modelo que vai para o jogo

O jogo não corre o ensemble CatBoost. O motor destila-o num modelo
linear sobre os **logaritmos das referências regionais** e mede o que a
destilação custa:

| variante | rmse (log) | dentro de 25% | dentro de 50% | linhas |
|---|---|---|---|---|
| as seis referências | 0.961 | 27% | 45% | 11 |
| **as três regionais** | **0.739** | **50%** | **73%** | **345** |
| só a média de venda regional | 0.838 | 41% | 65% | 345 |

As referências **locais** só existem para os itens que estão
actualmente na casa de Illnesses — 4% das transacções. Por isso o modelo
enviado é o regional: é melhor *e* funciona para todos os itens.

#### Avisos

O dataset não leva tempo até à venda: o concordance index do modelo de
sobrevivência anda perto do acaso, e um número que os dados não
sustentam é pior do que nenhum número.

#### Alternativas que foram avaliadas

- **Uma custom string gigante** (uma condição por item, por reino): é
  expressível — `ifeq(baseitem, "i:114931", 5000g, dbmarket)` — mas o TSM
  recusa compilar uma custom string com **mais de 190 variáveis locais**
  (cada `ifeq` custa uma), ou seja ~190 itens por string. Além disso
  teria de ser escrita nas SavedVariables do TSM (base versionada e com
  hash), ficaria com os preços **congelados**, e não é por reino sem um
  perfil por reino. É um *fallback*, não um desenho.
- **Auctionator**: é a alternativa viável e está implementada como ponte
  separada — `/dfaestimate fill on` faz o preço ser preenchido ao
  carregar num item. Nunca posta nada.

---

## 11. Scanner Data for Azeroth (`C:\economics`)

Resumo — a lista completa daquela árvore (com `realms`/`depths`, `collections`, `wants`,
`dfa_fast` e as armadilhas) está em [`C:\economics\COMMANDS.md`](../economics/COMMANDS.md).

```bat
python -m woweconomy.services.dataforazeroth fetch
python -m woweconomy.services.dataforazeroth fetch --kinds mounts,recipes --force
python -m woweconomy.services.dataforazeroth leaderboard --realm all --max-rank 10000
python -m woweconomy.services.dataforazeroth leaderboard --realm silvermoon,twisting-nether
python -m woweconomy.services.dataforazeroth realms --sample --sort avg-score --top 25
python -m woweconomy.services.dataforazeroth realms --sample --min-accounts 10 --sort accounts

REM --sample mede a profundidade do scan, nao a qualidade do reino:
REM compara antes o N-esimo melhor coleccionador (offline, le dfa.db)
python -m woweconomy.services.dataforazeroth depths --depth 100
python -m woweconomy.services.dataforazeroth depths --depth 50 --score 900000 --csv depths.csv

python -m woweconomy.services.dataforazeroth scan --realm silvermoon --kinds mounts,recipes --accounts 10000
python -m woweconomy.services.dataforazeroth scan --all-realms --min-score 800000 --out-dir woweconomy\dfa_hi
python -m woweconomy.services.dataforazeroth status
python -m woweconomy.services.dataforazeroth reweight --kinds mounts --limit 500
python -m woweconomy.services.dataforazeroth reweight --realm silvermoon --kinds mounts,recipes

REM bitmaps: o que cada conta TEM (2.7 KB/conta) -- e' o que o `wants` le
python -m woweconomy.services.dataforazeroth collections --status
python -m woweconomy.services.dataforazeroth collections --status --realm dragonmaw
python -m woweconomy.services.dataforazeroth collections --kinds appearances --realm dragonmaw,vashj,spinebreaker,haomarush,stormreaver
python -m woweconomy.services.dataforazeroth collections --realm jaedenar --limit 2
python -m woweconomy.services.dataforazeroth collections --kinds appearances
python -m woweconomy.services.dataforazeroth collections --kinds appearances --limit 1500
python -m woweconomy.services.dataforazeroth collections --kinds mounts --limit 500
python -m woweconomy.services.dataforazeroth collections --kinds recipes --limit 500
python -m woweconomy.services.dataforazeroth scan --accounts 5000 --keep-collections

REM quem ainda quer um item -- offline, le os bitmaps, 0 requests
python -m woweconomy.services.dataforazeroth wants --realm silvermoon --item 70593
python -m woweconomy.services.dataforazeroth wants --items precos.csv --realm dragonmaw --csv quem.csv
python -m woweconomy.services.dataforazeroth wants --top 25 --realm silvermoon --score 800000

python -m woweconomy.services.dataforazeroth to-sql
python -m woweconomy.services.dataforazeroth to-sql --kinds mounts
python -m woweconomy.services.dataforazeroth report
python -m woweconomy.services.dataforazeroth report --sort value --top 40
python -m woweconomy.services.dataforazeroth report --sort weighted --top 40
python -m woweconomy.services.dataforazeroth report --realm silvermoon --kinds mounts

REM candidatos: procura (bitmaps >=1M) x BoE (db2) x preco (TSM), read-only, 0 requests
python scripts\dfa_candidates.py
REM o preco regional vem do csv/items.csv (mais fresco que o AppData): corre fetch_csv.py antes

python -m woweconomy.services.dfa_fast pull --realm silvermoon --workers 6 --live
python -m woweconomy.services.dfa_fast ingest
python scripts/fix_dfa_overcount.py            REM dry-run; --apply escreve
woweconomy\dfa\continue_scan.cmd               REM duplo-clique: retoma o scan
woweconomy\dfa\run_scan_hidden.cmd             REM idem, sem consola (log em supervisor.log)
```

- **Token:** `--token`, senão `$DFA_TOKEN`, senão `woweconomy\dfa_token.txt`.
- **Uma corrida de cada vez:** `scan`, `pull`, `collections` e `reweight` disputam a mesma cota e
  a mesma `dfa.db`. Não repetem trabalho entre si, mas escrevem na mesma base — medir o «falta»
  do `--status` pede o scan parado.
- **`collections` não é `scan`:** só pergunta pelas contas que já estão na base e não têm bitmap
  (melhores scores primeiro). Não descobre contas novas, não toca em contadores e não tem quota
  `--accounts` — a quota é o que falta. Ctrl-C é seguro (cada bitmap é commitado).
- **`scan --keep-collections` não preenche para trás:** a conta já conhecida sai antes de o bitmap
  ser escrito. Quem apanha as antigas é o `collections` (1 pedido por par kind × conta).
- **`--limit` corta depois de concatenar** (kind a kind na ordem escrita): com 3 kinds os N
  pedidos vão todos para o primeiro. Dá um `--limit` a cada.
- **O progresso do `collections` conta duas unidades:** bitmaps (pares kind × conta) e contas
  completas (fecha os três kinds). O `falta ~4h59m` é o ritmo medido, não o `--delay`.
- **Idade do bitmap:** fica em `owned.written_at` (ISO UTC), impressa pelo `--status` e pelo
  `wants`. Depois de um patch um bitmap antigo só pode inflar (slots novos contam como «quer»).
- **`report` ≠ `wants`:** o `report` lê os contadores `missing`/`missing_realm` do `scan` (correr
  `collections` não lhe muda nada), não tem filtro de score e `--sort weighted` pesa mas não
  exclui. O filtro de score está no `wants --score X`.
- **`--realm` casa com o reino do personagem**, não com o grupo ligado: para um leilão ligado
  listas os membros à mão.
- **Os perfis DfA não são a população:** mediana de 48,7 % do catálogo de aparências contra
  10,95 % da raridade do site (corr. 0,856). `wants` mede coleccionadores a sério e subestima a
  procura geral.
- **Ids negativos no catálogo** (`-5330` Mark of the Thunderlord): 93 entradas, 72 obteníveis,
  encantamentos/ilusões sem item de leilão. Saem a 100 % «querido» e poluem o `wants --top`; não
  há filtro — ignora-lhes a linha ou usa `--item`.
- **Auto-teste dos bitmaps** (slot estável, ordem dos bits, `wants`, `reweight` sem contar duas
  vezes): `python -m woweconomy.services._collections_check` (segundos, base descartável).

---

## 12. Testes

```bat
python -m pytest tests -q                          REM 200 passed
python -m pytest tests -q -k collector_demand
python -m pytest tests/test_dfa_report.py -q       REM profundidade, quem-quer, recomendação
```

---

## 13. Cryptogame (`C:\economics`)

Outro projecto, sem dependências partilhadas.

```bat
python -m uvicorn cryptogame.server:app --host 127.0.0.1 --port 8000
python run_all_tests.py
python stress_test.py
python genworld.py
python scripts/generate_resources.py --width 256 --height 256
python phase_test.py
```

O `.venv` de `C:\economics` está quebrado (aponta para um `python.exe` 3.12 da Store que não
existe); o `python` global é 3.13 e não tem PIL, por isso `generate_resources.py` e `genworld.py`
falham por dependência. O `run_all_tests.py` tem `cwd="C:\\economics"` hardcoded.

---

## Armadilhas

- **Sem argparse** (posicionais, ignoram flags): `woweconomy/services/wowhead.py`,
  `scripts/backfill_spawns.py`, `scripts/repair_zone.py`, `scripts/fetch_csv.py`. `--help` num
  deles não dá ajuda: o `fetch_csv.py` **descarrega o CSV** e o `backfill_spawns.py` tenta abrir
  `--help` como ficheiro.
- **Scripts de uma corrida só, sem argumentos** (caminhos hardcoded): `analyze_farm.py`,
  `analyze_goblin.py`, `run.py`, e os `_*.py` da raiz (`_deep_dive.py`, `_check_csv.py`, …).
- **README desactualizado em dois pontos:** diz «50 testes» (são 200) e mostra
  `python -m woweconomy.services.dataforazeroth report` a partir de `FarmAnalysis` — esse módulo
  só existe em `C:\economics`.
- **`tsm_market.realm` tem maiúsculas** (`Dragonmaw`): `WHERE realm IN ('dragonmaw', …)` não
  casa nada e responde «zero leilões» sem erro — foi assim que a 1.ª leitura de candidatos deu
  425 falsas lacunas. Compara com `LOWER(realm)`.
- **`tsm_region` envelhece com o AppData, `csv/items.csv` não:** reimportar o AppData não
  refresca nada se o desktop não descarregou (os módulos dentro do `.lua` podem ter dias). O CSV
  público traz `updatedAt` e sale rate/vendas por dia; `scripts/fetch_csv.py` actualiza-o.
- **Caminho Windows sem aspas perde as barras no Git Bash** (`\e`, `\w` são escapes):
  `--dfa-dir C:\economics\woweconomy\dfa` funciona no `cmd` e falha no bash. Põe entre aspas.
- Ajuda verdadeira de qualquer comando com argparse:

```bat
python -m woweconomy.analytics.collector_demand --help
python -m woweconomy.services.dataforazeroth report --help
python scripts/journal.py log-sale --help
```

---

## DfA Estimate: o dataset, o modelo e o addon

```bat
REM medir variantes do CatBoost nas folds reais (não é um palpite)
python scripts/bench_price_model.py

REM treinar (walk-forward + modelos finais) e gerar o dataset do addon
python scripts/dfa_ml.py train
python scripts/dfa_pricing_export.py

REM o addon lê Data\DFA_Estimate_Data.lua; copiar para o jogo
xcopy /E /Y DFA_Estimate "C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns\DFA_Estimate\"
```

- O dataset leva **duas coisas diferentes**. Os `realms[realm].items` são as vendas
  observadas nesse reino (207 registos: preço, confiança, `psell`, `tts`). O bloco
  `prices` é outra coisa: o preço que o modelo linear dá para **cada item que a região
  conhece** (31 184 com os snapshots actuais), para o addon responder mesmo sem TSM
  instalado.
- No jogo a ordem é: registo do reino -> referências TSM ao vivo -> tabela `prices`.
  Sem nenhuma das três, o addon não inventa preço; diz que não tem.
- O `within25`/`within50` que vai no bloco `model` é medido, não escolhido: é a
  percentagem de itens que o modelo linear acerta a 25 %/50 % do preço realizado
  (51 %/73 % hoje). É a confiança que o addon mostra para um item sem registo.
- O modelo do preço usa `Lossguide` com 31 folhas e 600 árvores — escolhido por medição
  (`scripts/bench_price_model.py`): rmse_log 0.927 contra 1.032 do default simétrico,
  mae_log 0.612 contra 0.658, nas mesmas folds. O classificador e o modelo de
  sobrevivência **não** mudaram: nas mesmas folds o default ganhava.
- O export reaproveita o fit do modelo linear e a curva de procura medida na mesma
  frame: ficam em `models/dfa_ml/shipped_fit.json`, com a fingerprint dos `tsm.db` e
  `dfa.db` (tamanho + mtime), o horizonte e a configuração de treino. Inputs iguais = 
  export de ~17 s em vez de ~33 s; qualquer mudança refaz o fit (e um fit que falha
  rebenta, nunca serve o modelo antigo). Apagar o ficheiro força o retreino.
- Cada registo leva agora um `action` (post / undercut / hold / relist / wait / stop)
  com o preço, o net esperado, a probabilidade e a razão, e um bloco `market` com o
  que foi **observado**: listagens, unidades, variação de oferta, tendência, volatilidade
  e a nossa própria quota da oferta. Vem de `data/intel.sqlite` (snapshots Blizzard +
  scans de undercut) e do `tsm_price_change`; um reino sem snapshot diz que a oferta é
  desconhecida, nunca zero. O `watch.py` usa a mesma leitura para o conselho de relist.
  Sem `intel.sqlite` o export continua a correr e os campos ficam ausentes.

### As strings TSM do leilão (política, não previsão)

```bat
python -m pytest tests/test_tsm_strings.py -q
```

`woweconomy/pricing/tsm_strings.py` transcreve as três strings (Minimum/Normal/Maximum,
com `normalmarket` como fonte própria) e avalia-as com a semântica documentada da TSM —
fonte sem dados é inválida (não zero), `min`/`max` saltam a inválida, `ifgt` com
comparação inválida é *desconhecido* e não o ramo `else`. O `FeatureBuilder` usa-as
para produzir `tsm_normalmarket_log`, `tsm_normal_log`, `tsm_normal_over_region_market`
e `tsm_maximum_over_normal`; `ask_over_tsm_normal` existe mas está em `ASK_FEATURES`,
por isso o modelo do preço não a vê.

### Treinar e validar o modelo do preço (DFA ML)

```bat
REM walk-forward (treina num dia, valida no dia seguinte) + modelos finais
python scripts/dfa_ml.py train

REM o relatório sai em reports\dfa_ml\dfa_ml_report.json (+ o .md ao lado)
REM as métricas de preço estão em validation.price e validation.price_folds
```

- `scripts/dfa_ml.py` treina e escreve `reports\dfa_ml\dfa_ml_report.json` e o
  `dfa_ml_report.md` que lhe corresponde (`reports_dir` de `MLConfig`). Quem gera o
  dataset do addon é o `scripts/dfa_pricing_export.py` — são passos separados.
- Métricas de hoje (**357 linhas, 2 folds**): rmse_log **0.9072**, mae_log **0.6071**,
  dentro de ±25 % **35,5 %**, ±50 % **62,6 %**, *com* as features das strings TSM; *sem*
  elas 0.9274 / 0.6124 / 36,3 % / 61,8 %. Ganho pequeno e misto — 2 folds não chegam para
  o chamar robusto. A leitura e a calibração da Normal (mediana 1,626) estão em
  `C:\economics\tsm_strings.md`, secção **Current — DFA ML pricing validation**.
- Testes que cobrem isto:

```bat
python -m pytest -q tests/test_dfa_ml.py tests/test_pricing_engine.py tests/test_tsm_strings.py
```

  `test_dfa_ml` cobre o pipeline e o relatório, `test_pricing_engine` o motor/dataset que
  o addon lê, `test_tsm_strings` a transcrição das três strings (no repo: `tests\`).

---

## 14. Auction-house intelligence (`woweconomy.intel`)

Camada nova, aditiva. Lê o `tsm_markets.db` e escreve num ficheiro próprio
(`data/intel.sqlite`); não reescreve nenhuma tabela existente.

```bat
REM snapshots da API (ficheiro já capturado, ou directo da Blizzard)
python scripts\intel.py ah-ingest snapshot.json --region eu --realm Kazzak --connected-realm-id 1099
python scripts\intel.py ah-fetch --realm Kazzak --sync-realms

REM export do addon: eventos + inventário + sellers + mails
python scripts\intel.py addon-import

REM mails de AH: preenche o comprador das vendas que ficaram sem counterparty
REM (actualiza a venda existente; nunca cria outra venda)
python scripts\intel.py mail-import
python scripts\intel.py mail-link --realm Dragonmaw --dry-run
python scripts\intel.py mail-link --realm Dragonmaw

REM quem está a fazer undercut, com nome. `/dfaai undercut` no jogo faz o
REM scan (UI da Blizzard); o scan do Auctionator também alimenta isto.
python scripts\intel.py undercut-import
python scripts\intel.py undercut --realm Dragonmaw --days 30

REM cancelar e repor, manter, ou deixar expirar? (análise, não executa)
python scripts\intel.py relist --realm Dragonmaw --days 14

REM o que os sellers observados dizem, separado em factos e inferências
python scripts\intel.py sellers --realm Dragonmaw --days 30

REM dinâmica de um reino: undercuts, liquidez, tempo de venda, resposta a cancel
python scripts\intel.py diagnose --realm Dragonmaw --days 14

REM um modelo por reino (nunca um modelo para a Europa toda)
python scripts\intel.py realm-ml --min-rows 30 --iterations 60

REM farms em retorno realizável (vendas confirmadas do reino; senão média
REM regional descontada; nunca dbmarket). Usa as kills/h e as drop rates
REM medidas no jogo quando existem; o Wowhead fica para o que não foi medido.
python scripts\intel.py farms --realm Dragonmaw --kpm 20 woweconomy\wowhead_zones\zone_16.json

REM o que cada mob vale: kills, loot e cobre observados no jogo, com vendor,
REM valor de mercado e venda realizada em colunas separadas e datadas
python scripts\intel.py mobs --realm Dragonmaw --days 30 --persist

REM registo de experiências, com cadeia de hashes
python scripts\intel.py ledger-append data\experiments\ledger.jsonl --kind hypothesis --json "{\"claim\":\"...\"}"
python scripts\intel.py ledger-verify data\experiments\ledger.jsonl
```

Regras que o código impõe:

- `time_left` fica guardado **como veio**; um valor desconhecido fica com
  `time_left_band = NULL`, não é encaixado à força em SHORT/LONG/VERY_LONG.
- Um leilão que desapareceu entre dois snapshots é `unresolved_departures`,
  **nunca** uma venda. Só eventos confirmados alimentam a liquidez.
- Commodities não têm seller: a análise é agregada e
  `seller_identity_available = false`.
- `realm-ml` filtra o treino a um reino antes do CatBoost e recusa treinar um
  reino com poucas vendas; um prior partilhado só é reportado se ganhar na
  janela não vista daquele reino, e nunca substitui a previsão do reino.
- Em farms, o preço realizável é a mediana das vendas confirmadas do reino;
  sem amostra, a média regional de venda é descontada e o `price_basis` diz
  isso. `marketValue`/`dbmarket` não é preço realizável.
- O inventário marca as localizações ilegíveis (banco longe, posted sem API)
  como desconhecidas; não escreve zero. Um desaparecimento sem evento é
  `unexplained_decrease`, não uma venda.
- As kills e o loot vêm do nosso addon (`Kills.lua`), não do SavedVariables do
  MobInfo2: esse ficheiro é um blob LibSerialize + LibDeflate, e descodificá-lo
  em Python seria um segundo sistema. Os campos copiados são os mesmos (kills
  por NPC, loots/carcaças vazias, cobre, `itemList`); os dois addons podem
  correr ao mesmo tempo. Dentro do jogo, o `ZoneTimerRedux` lê o MobInfo2 pela
  API documentada (`MI2_GetMobData`) para ter as kills do NPC — nunca o blob —
  e usa-as apenas como denominador do ouro por kill.
- Uma morte é uma kill: `UNIT_DIED` e `PARTY_KILL` para o mesmo GUID contam
  uma vez, e só quando fomos nós (ou o pet) a fazer o dano. Uma carcaça
  reaberta para os restos não é um segundo loot, e um `batchId` importado
  duas vezes grava uma vez.
- `mobs` guarda vendor (`blizzard_items.sellPrice`), mercado
  (`tsm_realm_price.market_value`) e realizado (vendas confirmadas do reino,
  menos o corte) em colunas separadas, com `valued_at` e `price_source` para
  poder recalcular. Um item sem preço fica fora dessa coluna e é reportado —
  nunca contado como zero.
- `mail-link` só actualiza vendas com `counterparty` vazio, por item,
  quantidade e valor dentro da validade da mail. Nunca insere uma venda; dois
  compradores possíveis ou uma mail para duas vendas ficam ambíguos e intactos.
  Uma mail já ligada não volta a ser usada noutra venda, e a confiança é `high`
  quando o `bid` da invoice é o valor registado no journal, `medium` quando só
  o `buyout` do anúncio coincide. `seller_temp_invoice` ("Sale Pending") e a
  `seller` que a substitui são a mesma mail. Um scan que responde 0 mails
  antes de a inbox carregar não bloqueia o scan seguinte. Sem bloco
  `mail_jsonl` o `mail-import` sai com código 2 e diz que o addon tem de ser
  recarregado.
- O `undercut` só regista o que o AH devolveu: item, o meu preço unitário, o
  preço mais baixo e o vendedor desse listing. É um facto de preço, não uma
  venda; commodities não têm seller e ficam de fora. Há dois caminhos: o
  `/dfaai undercut` (query própria pela API da Blizzard, uma por item) e o
  scan do Auctionator (passivo, sem queries extra). O alcance do primeiro são
  os teus leilões com buyout e sem licitações; o do segundo tem também os
  filtros do Auctionator. Se o Auctionator mudar os nomes internos,
  `/dfaai status` mostra `auctionator hook=false`.
- O `relist` decide entre `hold`, `cancel_and_relist`, `let_expire` e `watch`
  por gap, unidades à frente e horas restantes. Só sugere um ask; o depósito
  é sunk nos dois caminhos (cancelar e expirar perdem-no, só a venda o
  devolve), e nada é cancelado ou reposto automaticamente. Um
  `cancel_and_relist` exige uma referência **verificada**: o scan in-game
  guarda o `itemKey` e o seller. Uma linha `price-only` da API, ou uma linha
  cujo item tem variantes (item level, suffix, bonus list, species) sem
  `itemKey` do nosso lado, fica `watch` com `review_required` e nunca produz
  um ask.
- O inventário lê o **banco de guilda** quando o abres; fica gravado, mas não
  entra na reconciliação nem em `current_units` — é armazenamento partilhado
  e outro membro a mexer não é um movimento inexplicável teu. `posted` passa
  a legível no `OWNED_AUCTIONS_UPDATED` (abrir o AH ou correr o cancel scan),
  não no login.
- "Open All" da Blizzard UI (e o do TSM) não abre cada mail; as invoices são
  lidas na mesma. O botão da Blizzard é hookado para forçar um scan antes de
  recolher anexos, e `MAIL_SHOW`/`MAIL_INBOX_UPDATE` já cobrem o resto.
- Em `farms`, cada NPC usa uma taxa de mortes/hora explícita
  (`--kill-rates`); sem valor medido, o default é marcado como suposição.

Testes: `python -m pytest -q tests\test_intel_ah.py tests\test_intel_inventory.py tests\test_intel_sellers.py tests\test_intel_realm_ml.py tests\test_intel_dynamics.py tests\test_intel_farming.py tests\test_intel_mail.py tests\test_intel_addon.py`

## 15. DFA Sentinel — o ciclo automático (`scripts/watch.py`)

Um só processo substitui a lista de comandos repetidos. Ele vê **o que mudou** e
corre só a etapa que essa mudança implica:

| fonte | tarefa |
|---|---|
| SavedVariables do addon | `intel.py addon-import` (eventos, stock, mails, sellers, undercuts) + fila do HUD |
| Journalator | `journal.py import` (vendas reais) |
| TSM `AppData.lua` | `sync_market.py --quiet --no-csv` |
| CSV públicos (`--with-csv`) | `fetch_csv.py`, no máximo 1×/dia |
| relógio, 1×/hora | `intel.py ah-fetch` por reino + undercut fora do jogo + reset |
| journal novo e ≥6 h | `intel.py realm-ml` |
| journal/TSM/training | `dfa_pricing_export.py --realm-model --all-realms`, no máximo 1×/30 min |

```bat
python scripts\watch.py                     REM fica a correr (intervalo 120 s)
python scripts\watch.py --once --verbose    REM uma passagem, com o plano
python scripts\watch.py --once --dry-run    REM decide, não corre, não escreve
python scripts\watch.py --once --offline    REM só ficheiros (sem Blizzard)
python scripts\watch.py --realms Dragonmaw,Silvermoon --no-notify
```

Estado em `data/watch_state.json` (assinaturas, último treino/fetch, cooldowns de
alertas); log em `logs/watch.log`. Um SavedVariables com menos de 30 s desde a
última escrita não é lido (`--stable-s`): meio ficheiro Lua é um erro, não dados.
Um só daemon corre de cada vez: o processo guarda `data/watch.lock` (o SO liberta-o
quando o processo morre) e um segundo arranque sai com `ERROR another DFA Sentinel
already holds ...` no log. `--once` e `--dry-run` não tomam o lock, para poderem ser
usados com o daemon a correr.

O export é o trabalho caro (minutos) e o TSM reescreve o `AppData.lua` de poucos em
poucos minutos. Uma mudança de journal/TSM dentro de `--export-min-gap-min` (30 min)
não dispara outro export: fica em fila no estado (`export_pending`), os ticks seguintes
mostram `pricing inputs changed` só ao `sync`, e o export corre no primeiro tick depois
da janela — mesmo que entretanto nada volte a mudar, por isso a mudança não se perde.
Um export que falha mantém a fila (retenta depois da janela) em vez de esperar pelas
6 h do fallback `--export-every-min`.

Depois de um export válido o daemon faz o **deployment** das duas cópias do
dataset, com os caminhos da configuração e nunca de uma pesquisa:
`PricingConfig.dataset_path` (cópia canónica do projeto) e
`PricingConfig.addons_dir` / `--addons-dir` (o `Interface/AddOns` do jogo, destino
`DFA_Estimate/Data/DFA_Estimate_Data.lua`). Só escreve se os bytes mudarem, valida
antes de escrever, escreve atomicamente (temporário + `os.replace`), guarda a cópia
substituída como `*.lua.bak` (só se for válida) e retenta um ficheiro preso pelo
jogo. O `.toc` e o resto do addon nunca são tocados, e uma pasta em falta é
reportada como `missing`, não criada. Se um destino falhar o deployment é
`ok=false` (e sai alerta); se um export novo aterrar a meio, o passe repete com os
bytes novos e, se nunca estabilizar, o `export_pending` volta à fila. O ficheiro
canónico é também uma fonte vigiada: um export corrido à mão chega ao jogo no tick
seguinte, sem esperar pelo próximo export do daemon.

```bat
REM undercut sem abrir o jogo: cruza os teus leilões com o retrato da Blizzard
python scripts\intel.py blizzard-undercut --realm Dragonmaw
REM as pressões do HUD (e o resultado de cada uma)
python scripts\intel.py actions
```

Regras novas que o código impõe:

- A API da Blizzard **não tem seller**. As observações dela têm
  `source=blizzard_api` e `confidence=price-only`, e o resumo mostra
  `(seller not in API)`; nenhum nome é inventado. Só o scan in-game dá
  identidade. `reference_status` fica `variant-ambiguous` quando o listing
  mais barato tem `item_context`/`bonus_lists`/`modifiers` e o nosso
  `posted` não guarda o `itemKey` correspondente; fica `price-only` nos
  restantes. Nenhum dos dois é acionável: o Sentinel mostra-os como **review**,
  não como `cancel_and_relist`.
- `time_left` da API é uma categoria (SHORT/MEDIUM/LONG/VERY_LONG). Se o addon não
  trouxe segundos, `relist` recebe `hours_left=None` e **não** aplica a regra
  "deixar expirar"; numa referência verificada, um gap grande ainda justifica
  repor. Numa referência `price-only` ou `variant-ambiguous`, o resultado é
  sempre `watch`/review.
- Um reset de mercado é preço mais baixo ≥30 % acima e oferta ≥40 % abaixo entre
  dois snapshots; uma venda baixa oferta sem subir preço, um listing novo baixa o
  preço, e nenhum dos dois é reset.
- O export indexa **267 reinos** (`realmIndex`): factor de preço contra o regional
  (encolhido com prior 10), `undercutShare` (só com lista de postados lida) e
  `babysit24h` (só com dois snapshots da API). O tooltip mostra `Best realm` e o
  risco do reino actual; sem medição, a linha não aparece.
- O HUD (`/dfaai hud`) tem Snapshot, Undercut scan, Mail e Export. Cada pressão fica
  em `actions_jsonl`; o daemon importa só as novas e corre o seguimento. O HUD é
  construído em `pcall`: sem moldura, a fila continua a funcionar.
- O banco de personagem usa as abas compradas (`C_Bank`), com fallback para a gama
  antiga; o warband é lido (containers 13–17) e guardado à parte, nunca reconciliado
  (um alt a mexer não é um movimento teu).
- O export do dataset demora minutos e refita o modelo: corre a 6 h ou quando o
  journal/referências mudam, nunca por uma alteração só de inventário.

```bat
REM alertas: desktop sempre; Discord se o webhook existir (nunca no log)
setx DFA_DISCORD_WEBHOOK "https://discord.com/api/webhooks/..."
REM no .env também funciona: DFA_DISCORD_WEBHOOK=...
```

Relatório completo da lógica: `reports/automation.md`.

---

## 15. Sentinel: camada ML validada (`woweconomy.ml.sentinel`)

Treina e publica os modelos que o dataset do jogo consome. Usa CatBoost para
o preço e os riscos, e scikit-learn para a calibração isotonic/Platt e para os
baselines logísticos/Ridge que o CatBoost tem de bater no bloco de teste.

```bat
REM instalar as dependências novas (scikit-learn/joblib)
python -m pip install -r requirements-ml.txt

REM treinar e promover um artefacto versionado
python scripts/dfa_ml.py sentinel --sentinel-iterations 120

REM só treina se houver labels novas suficientes, ou se o modelo estiver stale
python scripts/dfa_ml.py sentinel --if-needed

REM treina e verifica, mas não publica CURRENT
python scripts/dfa_ml.py sentinel --dry-run

REM reconstruir o dataset do addon (schema 2) depois de promover
python scripts/dfa_pricing_export.py
```

Regras que o código impõe:

- O split é **cronológico**: treino → calibração → teste intocado. O teste
  nunca escolhe hiperparâmetros nem calibração.
- O rótulo de venda usa só vendas confirmadas; expiração é 0 confirmado;
  cancelamento e listing censurado ficam **fora** daquele horizonte. Um
  listing resolvido aos 40h tem rótulo de 168h (1 ou 0), mesmo que ainda não
  tenham passado 168h de calendário.
- A probabilidade de venda de 24/48/168h é calibrada e projectada numa curva
  monótona (PAVA) só para exportação; as métricas por horizonte são as
  medidas, não a projecção.
- A selecção usa **log loss** (probabilidade) e **MAE log** (preço); Brier,
  AUC, ECE, MAE gold e SMAPE são reportados. Uma melhoria só é declarada se
  bater o baseline no teste intocado.
- O preço realizado (`ereal`) não usa features do ask; `price` continua a ser
  a estimativa do motor de preços. São campos distintos de propósito.
- `undercut` é **pressão de mercado** (o mínimo observado desce), não prova
  de que um leilão nosso foi undercut. `decline` é a queda do valor de
  mercado; são targets separados.
- Um target sem amostra suficiente fica `insufficient_data`/`unsupported_signal`
  e o campo sai `nil`; nunca sai 0 por omissão.
- Cada preço leva um **intervalo split-conformal a 80 %** e a cobertura
  medida num bloco que a calibração nunca viu. O tooltip mostra o intervalo
  e a cobertura lado a lado (`plow`/`phigh` para `price`, `elow`/`ehigh`
  para `ereal`); um campo ausente continua `nil`, nunca uma largura zero.
- O artefacto só é promovido depois de todos os ficheiros serem escritos,
  checksumados e carregados de volta. `CURRENT` é substituído atomicamente.
- `watch.py` agenda `sentinel --if-needed` a cada 24h; o refit só acontece
  quando há >= max(20, 10% dos rows anteriores) labels novas confirmadas.
- O leitor do addon aceita schema 1 e 2; schema 2 acrescenta
  `psellByHorizon`, `plow`, `phigh`, `plevel`, `pcov`, `pintn`, `ereal`,
  `elow`, `ehigh`, `elevel`, `ecov`, `eintn`, `undercut`, `decline`,
  `scope`, `model`, `status`, `fallback`, `dq`.

Relatório medido, limitações e contagem por reino:
`reports/dfa_sentinel_ml.md`; métricas completas em
`reports/dfa_sentinel/sentinel_report.json`.
