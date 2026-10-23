# Validação do preço de referência ponderado

Alvo: **Dragonmaw** (cluster Spinebreaker). Comparação feita sobre **exatamente os 328 itens** do inventário, nas mesmas condições, com os mesmos coeficientes e os mesmos grupos: **só a referência muda**.

> Esta validação **não**demonstra ouro realizado melhorado. Não é possível demonstrá-lo com esta base de dados (ver §4). O que segue é uma auditoria de correcção, de semântica e de sanidade dos preços.

## 1. Auditoria da expressão TSM

| teste | resultado | evidência |
|---|---|---|
| precedência: `^` liga mais forte que `*` | **PASS** | a expressão devolve a média geométrica ponderada, como o otimizador do TSM assume (^ prioridade 3, * prioridade 2) |
| valores em falta: cada operação segue a escada documentada | **PASS** | 31 operações × 4 combinações (ambos/local ausente/regional ausente/nenhum): local→regional→histórico, como documentado |
| sentinel da TSM tratado como ausente | **PASS** | dbmarket = 99,999,999,000 dá 4,000,000 (a referência regional), não 10 M g |
| sem valores negativos, infinitos ou NaN | **PASS** | todas as combinações extremas dão um valor finito e não negativo |
| min <= normal <= max em todas as operações | **PASS** | 31 operações verificadas (com o coeficiente) |
| min <= normal <= max também quando local e regional divergem | **PASS** | 31 operações verificadas com local 4x o regional |
| só fontes que existem no TSM instalado | **PASS** | fontes usadas: dbmarket, dbregionhistorical, dbregionmarketavg |

### 1.1 Semântica do TSM instalado (lida no código do addon)

| comportamento | onde | o que diz |
|---|---|---|
| precedência | `TokenProcessor._GetOperatorPriority` | `^` = 3, `*` = 2, `+`/`-` = 1: `^` liga mais forte que `*` |
| `check(a, b)` | `Optimizer.ReplaceCheck` | é reescrito como `ifgt(a, 0, a, b)`: a nossa escada de fallbacks usa exactamente a mesma semântica |
| `ifgt(a, b, x, y)` | `CodeGen._GenerateComparisonStatement` | 4 argumentos: se `a > b` então `x`, senão `y` |
| preços em ouro | `AuctioningOperation.lua:162` | no Retail o valor é arredondado **para cima** ao prata (`Math.Ceil(value, COPPER_PER_SILVER)`) |
| limites de valor | `AuctioningOperation.valueLimits` | só `keepQuantity`, `maxExpires`, `postCap` e `stackSize` têm limites; **os preços não têm** → um valor extremo seria usado tal e qual |
| fonte em falta | `Util.GetItemPrice` | devolve `nil` (preço inválido), não 0: a operação trata-o como ausência |

**Contraste com o default do próprio TSM** (`AuctioningOperation.lua:208-210`):

```
minPrice   = check(first(crafting,dbmarket,dbregionmarketavg), max(0.25*avg(crafting,dbmarket,dbregionmarketavg),1.5*vendorsell))
normalPrice = check(first(crafting,dbmarket,dbregionmarketavg), max(2*avg(crafting,dbmarket,dbregionmarketavg),12*vendorsell))
```

O TSM **já** mistura local e regional (com `avg`), mas a mistura é fixa e aritmética. A proposta ponderada substitui-a por uma mistura **geométrica** com o peso medido pela evidência local. A melhoria proposta é, portanto, um refinamento do que o addon já faz — não uma invenção.

## 2. Auditoria do cálculo do peso

| teste | resultado | evidência |
|---|---|---|
| o peso é calculado por item e agregado por célula | **PASS** | o motor calcula o peso por item (profundidade × dispersão × freshness); a expressão do TSM leva o **mediano** da célula (grupo × banda), porque o TSM avalia uma expressão por grupo. Distribuição: 0.00–1.00, mediana 0.50 |
| a referência regional nunca domina (piso do peso) | **PASS** | nenhum item com preço local tem peso local abaixo de 0.50 (0 excepções) |
| a profundidade pesa no peso | **PASS** | 0 anúncio(s) -> peso mediano 0.55 (n=35); 1 anúncio(s) -> peso mediano 0.50 (n=79); 2 anúncio(s) -> peso mediano 0.60 (n=33); 3 anúncio(s) -> peso mediano 0.90 (n=19); 4 anúncio(s) -> peso mediano 0.90 (n=6); 5 anúncio(s) -> peso mediano 1.00 (n=3) |
| a dispersão pesa no peso | **PASS** | 0,25-0,50 -> peso mediano 0.65 (n=31); <=0,25 -> peso mediano 0.65 (n=47); 0,50-1,00 -> peso mediano 0.50 (n=28); >1,00 -> peso mediano 0.50 (n=8) |
| a divergência local/regional é diagnóstico, não regra de preço | **PASS** | 34 itens divergem >= 75% e todos 34 trazem o aviso; o peso não muda por causa da divergência (só a confiança) |
| a frescura entra no peso (idade local vs regional) | **PASS** | idade local  h, regional 0.0 h: o local é mais fresco, o factor é 1,00 em todos os itens |
| itens sem anúncio local: a referência original é um valor decaído | **PASS** | 35 itens do inventário têm 0 anúncios no reino: o `dbmarket` deles é uma média decaída, não um ask vivo, e a proposta original posta esse valor como se fosse preço. Em 26 deles a ponderada muda ≥10 % (a mistura com a referência regional) |

### 2.1 Onde o peso é calculado

- **Por item** (no motor, `reference_price.estimate_reference`): o peso usa a profundidade, a frescura e a dispersão **daquele item**, naquele reino.
- **Por célula (grupo × banda)** no payload: a expressão do TSM é uma só por operação, por isso leva o **mediano** dos pesos dos itens da célula. Mediana dos pesos da amostra: 0.50 (mínimo 0.00, máximo 1.00).
- A granularidade por item **fica no cálculo do DFA** e no `weighted_vs_original.csv`; o TSM não a consegue avaliar.

## 3. Comparação pareada (os mesmos 328 itens)

- itens comparados: **328**
- com referência que muda ≥10 %: **124** (71 a subir, 53 a descer)
- sem preço local (a referência original caía no regional): 328
- sentinelas: 11

### 3.1 As 25 maiores subidas

| item | banda | local | regional | peso | original | ponderado | variação | motivo |
|---|---|---:|---:|---:|---:|---:|---:|---|
| Reinforced Chain Shoulderp (1760) | <100 | 49 g | 816 g | 0.55 | 49 g | 150 g | +209% | preço local assenta em 0 anúncio(s) | divergência local/regional de -9 |
| Linked Chain Pants (1751) | <100 | 42 g | 221 g | 0.68 | 42 g | 96 g | +131% | divergência local/regional de -81%: confiança reduzida, o preço não mu |
| Plans: Mirrored Boots (69969) | 500-2k | 1,764 g | 7,927 g | 0.50 | 1,764 g | 3,739 g | +112% | peso limitado a 0.50 (bruto 0.49) | preço local assenta em 1 anúncio(s |
| Grunt's Shield of the Auro (15512) | 2k-5k | 2,192 g | 9,527 g | 0.65 | 2,192 g | 4,570 g | +108% | preço local assenta em 1 anúncio(s) | divergência local/regional de -7 |
| Scorching Wand (5213) | <100 | 75 g | 296 g | 0.50 | 75 g | 149 g | +98% | peso limitado a 0.50 (bruto 0.41) | preço local assenta em 0 anúncio(s |
| Gloom Reaper of the Firefl (863) | <100 | 94 g | 308 g | 0.65 | 94 g | 170 g | +81% | preço local assenta em 1 anúncio(s) |
| Beazel's Basher (13024) | 500-2k | 1,387 g | 4,913 g | 0.90 | 1,387 g | 2,451 g | +77% | — |
| Gorestained Garb (120166) | 100-500 | 106 g | 330 g | 0.50 | 106 g | 187 g | +76% | peso limitado a 0.50 (bruto 0.23) |
| Dilapidated Cloth Gloves (25344) | <100 | 24 g | 100 g | 0.60 | 24 g | 43 g | +76% | divergência local/regional de -76%: confiança reduzida, o preço não mu |
| Pagan Belt of the Fireflas (14164) | 100-500 | 254 g | 893 g | 0.90 | 254 g | 447 g | +76% | — |
| Green Woolen Vest (2582) | <100 | 100 g | 299 g | 0.50 | 100 g | 173 g | +73% | peso limitado a 0.50 (bruto 0.49) | preço local assenta em 1 anúncio(s |
| Pattern: Heavy Woolen Cloa (4346) | 100-500 | 125 g | 367 g | 0.50 | 125 g | 214 g | +72% | peso limitado a 0.50 (bruto 0.20) |
| Prospector's Mitts (14564) | 2k-5k | 4,149 g | 10,971 g | 0.65 | 4,149 g | 6,746 g | +63% | preço local assenta em 1 anúncio(s) |
| Brown Linen Vest (2568) | 100-500 | 118 g | 317 g | 0.60 | 118 g | 184 g | +56% | — |
| Linked Chain Gloves (1750) | 100-500 | 127 g | 306 g | 0.50 | 127 g | 197 g | +55% | peso limitado a 0.50 (bruto 0.49) | preço local assenta em 1 anúncio(s |
| Woolen Boots (2583) | 100-500 | 286 g | 715 g | 0.50 | 286 g | 432 g | +51% | peso limitado a 0.50 (bruto 0.49) | preço local assenta em 1 anúncio(s |
| Schematic: Portable Bronze (4414) | 500-2k | 1,239 g | 2,821 g | 0.50 | 1,239 g | 1,870 g | +51% | peso limitado a 0.50 (bruto 0.41) | preço local assenta em 0 anúncio(s |
| Brown Linen Pants (4343) | 100-500 | 109 g | 246 g | 0.50 | 109 g | 163 g | +50% | peso limitado a 0.50 (bruto 0.49) | preço local assenta em 1 anúncio(s |
| Tarnished Dawnlit Sentinel (258934) | 100-500 | 131 g | 293 g | 0.50 | 131 g | 196 g | +49% | peso limitado a 0.50 (bruto 0.16) | preço local assenta em 1 anúncio(s |
| Plans: Green Iron Gauntlet (3612) | 500-2k | 1,623 g | 3,574 g | 0.50 | 1,623 g | 2,409 g | +48% | peso limitado a 0.50 (bruto 0.16) | preço local assenta em 1 anúncio(s |
| Simple Linen Boots (10046) | 100-500 | 268 g | 619 g | 0.50 | 268 g | 391 g | +46% | peso limitado a 0.50 (bruto 0.49) | preço local assenta em 1 anúncio(s |
| Battle Knife (15241) | <100 | 100 g | 211 g | 0.50 | 100 g | 145 g | +46% | peso limitado a 0.50 (bruto 0.41) | preço local assenta em 0 anúncio(s |
| Ritual Amice (14126) | 2k-5k | 4,777 g | 10,097 g | 0.50 | 4,777 g | 6,945 g | +45% | peso limitado a 0.50 (bruto 0.20) |
| Tough Leather Armor (1810) | 100-500 | 370 g | 778 g | 0.50 | 370 g | 536 g | +45% | peso limitado a 0.50 (bruto 0.49) | preço local assenta em 1 anúncio(s |
| Ivycloth Boots of the Auro (9792) | 100-500 | 101 g | 223 g | 0.50 | 101 g | 144 g | +43% | peso limitado a 0.50 (bruto 0.16) | preço local assenta em 1 anúncio(s |

### 3.2 As 25 maiores descidas

| item | banda | local | regional | peso | original | ponderado | variação | motivo |
|---|---|---:|---:|---:|---:|---:|---:|---|
| Blacksmith Hammer (5956) | 2k-5k | 4,197 g | 58 g | 0.50 | 4,197 g | 493 g | -88% | peso limitado a 0.50 (bruto 0.23) | divergência local/regional de +714 |
| Arclight Spanner (6219) | 2k-5k | 2,830 g | 91 g | 0.50 | 2,830 g | 507 g | -82% | peso limitado a 0.50 (bruto 0.23) | divergência local/regional de +301 |
| Defender Bracers of the Qu (6574) | 2k-5k | 3,075 g | 252 g | 0.50 | 3,075 g | 881 g | -71% | peso limitado a 0.50 (bruto 0.16) | preço local assenta em 1 anúncio(s |
| Bright Belt (4708) | 500-2k | 1,948 g | 351 g | 0.50 | 1,948 g | 827 g | -58% | peso limitado a 0.50 (bruto 0.14) | preço local assenta em 0 anúncio(s |
| Short Cutlass (1829) | 2k-5k | 4,033 g | 797 g | 0.50 | 4,033 g | 1,792 g | -56% | peso limitado a 0.50 (bruto 0.14) | preço local assenta em 0 anúncio(s |
| Wrangler's Buckler of the  (15332) | 50k-150k | 131,650 g | 25,957 g | 0.50 | 131,650 g | 58,457 g | -56% | peso limitado a 0.50 (bruto 0.20) | divergência local/regional de +407 |
| Defender Tunic of the Quic (6580) | 500-2k | 937 g | 197 g | 0.50 | 937 g | 447 g | -52% | peso limitado a 0.50 (bruto 0.16) | preço local assenta em 1 anúncio(s |
| Pattern: Hands of Darkness (7092) | 15k-50k | 17,098 g | 3,507 g | 0.55 | 17,098 g | 8,381 g | -51% | preço local assenta em 0 anúncio(s) | divergência local/regional de +3 |
| Crochet Gloves (3940) | 500-2k | 692 g | 147 g | 0.50 | 692 g | 345 g | -50% | peso limitado a 0.50 (bruto 0.49) | preço local assenta em 1 anúncio(s |
| Brackwater Vest (3306) | 2k-5k | 4,679 g | 1,171 g | 0.50 | 4,679 g | 2,341 g | -50% | peso limitado a 0.50 (bruto 0.14) | preço local assenta em 0 anúncio(s |
| Cadet Leggings of the Auro (9763) | 100-500 | 287 g | 75 g | 0.55 | 287 g | 147 g | -49% | preço local assenta em 0 anúncio(s) | divergência local/regional de +2 |
| Cross-Stitched Gloves (1783) | 2k-5k | 4,300 g | 1,176 g | 0.80 | 4,300 g | 2,249 g | -48% | divergência local/regional de +265%: confiança reduzida, o preço não m |
| Bladefist Axe of the Fever (55287) | 2k-5k | 3,124 g | 963 g | 0.50 | 3,124 g | 1,734 g | -44% | peso limitado a 0.50 (bruto 0.20) | divergência local/regional de +225 |
| Ritual Gloves of the Firef (14124) | 2k-5k | 2,016 g | 708 g | 0.65 | 2,016 g | 1,195 g | -41% | preço local assenta em 1 anúncio(s) | divergência local/regional de +1 |
| Fortified Bracers of the Q (9811) | 100-500 | 296 g | 109 g | 0.80 | 296 g | 180 g | -39% | divergência local/regional de +171%: confiança reduzida, o preço não m |
| Canvas Gloves (1767) | 2k-5k | 2,720 g | 1,013 g | 0.50 | 2,720 g | 1,660 g | -39% | peso limitado a 0.50 (bruto 0.20) | divergência local/regional de +169 |
| Long Bastard Sword (1830) | 2k-5k | 3,120 g | 1,191 g | 0.80 | 3,120 g | 1,928 g | -38% | divergência local/regional de +162%: confiança reduzida, o preço não m |
| Headhunter's Cloak of the  (15354) | 2k-5k | 3,341 g | 1,350 g | 0.90 | 3,341 g | 2,124 g | -36% | divergência local/regional de +147%: confiança reduzida, o preço não m |
| White Linen Shirt (2576) | 100-500 | 353 g | 134 g | 0.65 | 353 g | 228 g | -35% | preço local assenta em 1 anúncio(s) | divergência local/regional de +1 |
| Aboriginal Rod of the Quic (15971) | 2k-5k | 4,398 g | 2,064 g | 0.50 | 4,398 g | 3,013 g | -32% | peso limitado a 0.50 (bruto 0.16) | preço local assenta em 1 anúncio(s |
| Taut Compound Bow (3778) | 100-500 | 456 g | 223 g | 0.50 | 456 g | 319 g | -30% | peso limitado a 0.50 (bruto 0.41) | preço local assenta em 0 anúncio(s |
| Antipodean Rod (2879) | 5k-15k | 14,519 g | 2,753 g | 0.90 | 14,519 g | 10,411 g | -28% | divergência local/regional de +427%: confiança reduzida, o preço não m |
| Overseer's Mandate (174790) | 50k-150k | 56,100 g | 28,050 g | 0.55 | 56,100 g | 41,068 g | -27% | preço local assenta em 0 anúncio(s) | divergência local/regional de +1 |
| Beatstick (3190) | 100-500 | 117 g | 58 g | 0.55 | 117 g | 85 g | -27% | preço local assenta em 0 anúncio(s) | divergência local/regional de +9 |
| Bristlebark Blouse (14570) | 50k-150k | 84,754 g | 45,483 g | 0.50 | 84,754 g | 62,087 g | -27% | peso limitado a 0.50 (bruto 0.20) | divergência local/regional de +86% |

### 3.3 Leitura dos extremos

- **As subidas** são, quase todas, itens cujo preço local assentava em 0-1 anúncio: a referência original pegava nesse ask isolado, a ponderada puxa o nível para a referência regional (com o peso no piso de 0,50). É exatamente a protecção pedida contra dados locais esparsos — mas também significa que **o preço sobe face ao que o TSM postava**, e o impacto em vendas não é medível aqui.
- **As descidas** são itens em que o ask local era muito acima do nível regional (outliers): a ponderada baixa-os. Sem histórico de vendas datadas, **não há como dizer** se o ask local estava errado ou se o regional estava inflado; a divergência está assinalada como diagnóstico em cada linha.
- Nenhum item com o preço local ausente é reposto por um valor inventado: a escada de fallbacks é a mesma da expressão do TSM.
- **Atenção ao comportamento no TSM**: a expressão só affecta o `normalPrice` (o preço quando não há concorrência). Com concorrência, o TSM posta `parede − undercut` limitado por `minPrice`/`maxPrice`, que também mudaram. Se a parede de um item ficar **abaixo** do `minPrice` ponderado, a operação deixa de postar (`priceReset = none`). Isto afecta os itens com o ask local muito acima do regional: a proposta ponderada baixa o `minPrice` e pode **voltar a postar** itens que a original bloqueava — o inverso do que se quer num liquidation rush. É o ponto que precisa de teste em jogo.

## 4. Validação contra as vendas reais (e os seus limites)

- vendas de transmog no journal: **195**
- **com** preço local hoje (comparáveis): **5**
- **sem** preço local hoje (não comparáveis): 190

**O que falta para uma comparação válida de ouro realizado:**

| falta | onde teria de vir |
|---|---|
| data da venda | o `journal` do Journalator tem `kind`/`qty`/`price_copper` mas o `journal_stats` **não guarda datas** |
| a parede local na data da venda | `tsm_price_change` só tem 3 dias (16-19/10) e só para 5-8 reinos; para uma venda de há meses não há reconstrução |
| o preço pedido no momento da publicação | o journal regista a venda, não o pedido |
| taxa e depósito cobrados | `journal_stats` não tem os custos; a taxa de 5 % e o depósito de 5 % (máx. 300 % do vendor) são **assumidos** |
| o custo de aquisição | não existe em lado nenhum da base |
| o inventário não vendido | só se pode inferir do export actual, que é depois das vendas |

**O que é mensurável hoje, e o que não é:**

- Mensurável: a comparação *item a item* das duas referências, a distribuição dos pesos, a sanidade dos preços e a semântica da expressão.
- **Não mensurável**: ouro realizado, tempo até à venda, sell-through, o custo das expirações. Qualquer afirmação nesses termos seria inventada.

O subconjunto de calibração (vendas de transmog com preço local hoje) tem **n = 25** vendas. O erro mediano do preço realizado contra a referência é 3.18 (original) e 3.53 (ponderada): **a diferença não decide nada** com uma amostra desta dimensão, e as vendas não são datadas (a comparação é contra a referência de hoje, não a da venda).

## 5. Execução no TSM e reversão

**Nada foi importado.** As duas propostas estão em disco:

| ficheiro | conteúdo | estado |
|---|---|---|
| `pricing_groups_proposal.tsm` | 31 grupos, referência `first(...)` | intacto |
| `pricing_groups_weighted_proposal.tsm` | os mesmos 31 grupos, referência ponderada | intacto |

### 5.1 Teste de importação (isolado)

Não é possível importar por via automática sem tocar no perfil vivo: o import do TSM aplica sempre ao **perfil activo** e não há modo headless. O que foi verificado, e é verificável, é a **estrutura do payload**:

- uma linha, formato AceSerializer (`^1^T...`), terminador `^^`;
- `groupExport` com os mesmos grupos e os mesmos itens que a proposta original;
- `operations.Auctioning` com os mesmos campos e os mesmos valores, **excepto** `normalPrice`, `minPrice` e `maxPrice`;
- `groupOperations` com `override = true` em todos os grupos.

Procedimento para o teste em jogo (perfil isolado):

1. `TSM → Settings → General → Profiles` → criar `teste-rush` (ou `/tsm profile` num perfil novo).
2. `Groups → Import` → colar o conteúdo de `pricing_groups_weighted_proposal.tsm`.
3. `Operations → Auctioning` → confirmar que a operação tem os campos esperados e que o *Price Tester* não acusa expressão inválida.
4. `Post Scan` num item de cada banda: confirmar que o preço proposto está entre `minPrice` e `maxPrice` e que o undercut é o esperado.

### 5.2 Reversão

- Nada foi alterado no perfil vivo, portanto **não há nada a reverter**.
- Se a candidata for importada num perfil de teste e sair mal, apagar o perfil de teste (ou os grupos importados) e voltar a importar a proposta original: as duas são mutually exclusive no mesmo perfil, e a original continua em `pricing_groups_proposal.tsm`.
- Importar as duas no mesmo perfil **não** substitui: o TSM cria `PG ... 1`, `PG ... 2`. Importar sempre num perfil limpo.

## 6. Riscos por resolver e o mínimo de dados extra

| risco | porquê | dados mínimos |
|---|---|---|
| não se sabe se a referência ponderada vende mais | não há vendas datadas nem parede histórica | o `journal` com **data** por venda e o preço pedido, e o log de paredes por reino com mais de 3 dias |
| a divergência local/regional não é decidível | o regional é a média de 92 reinos e o local pode ser um outlier | as vendas datadas, para saber qual dos dois lados estava certo |
| o nível do ask está 3-5× acima do preço realizado | é um problema do coeficiente de banda, não da referência | o preço de venda por item, com data |
| o peso por item não chega ao TSM | a expressão é por grupo | um export por item (o formato `liquidity_mogs_groups_<reino>.tsm` já faz isso) |
| custos de AH assumidos | não estão na base | o extrato de vendas com taxa e depósito, ou a confirmação em jogo |

## 7. Recomendação

**Usar a proposta ponderada num perfil de teste, e não no perfil vivo.**

Motivos:

1. A auditoria da expressão e do peso **passa**: a precedência, a semântica de `ifgt`/`check`, o tratamento do sentinel, os valores ausentes e os extremos estão todos verificados contra o código do addon, e a expressão de cada operação bate com o modelo.
2. A candidata é mais **cuidada com os dados** que a original: nunca transforma o sentinel num preço de 10 M g, nunca fixa o preço de um item raro num ask isolado, e nunca deixa um outlier local subir o preço. **Isso não é ser mais conservador quanto ao preço pedido**: para 71 itens o preço pedido **sobe** (o local assentava em 0-1 anúncio) e para 53 desce. Num liquidation rush, subir o preço é um risco, não uma melhoria — e é precisamente o que o teste em perfil tem de medir.
3. **Não há evidência de que venda mais** — nem de que venda menos. O único subconjunto comparável (n = 25 vendas, sem datas) dá um erro mediano ligeiramente **pior** para a ponderada (3,53 contra 3,18), o que não decide nada com esta amostra mas também não a favor. O relatório diz isso em vez de inventar um número.
4. Como o ganho é teórico e o risco é real, o caminho certo é o perfil de teste: importar, fazer um *Post Scan* e comparar com o que a proposta original teria pedido no mesmo dia.

**Ponto de atenção concreto** (§3.3): ao baixar o `minPrice` nos itens com ask local muito acima do regional, a candidata pode voltar a postar itens que a original bloqueava (`priceReset = none`). Se isso for indesejado num rush, a correção é limitar o `minPrice` ao piso do piso de perda — não mexer na referência.

O que **não** se recomenda: importar a candidata no perfil activo, rejeitar a original por ser pior (a auditoria não mostra que o é), ou afirmar melhoria de ouro realizado (não é mensurável).
