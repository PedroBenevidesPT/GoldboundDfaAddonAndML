# Mogs por tipo de procura — grupos, strings e plano

Escrito a 19-10-2026. Base: `tsm_markets.db` (snapshot regional 326, 31 184 itens;
snapshot de Spinebreaker 601), `db2_item_appearance` (127 155 itens com
aparência, 57 480 aparências), `db2_items` e `reports/inventory_prob_novo.csv`
(140 itens do inventário × 92 reinos, modelo de coleccionadores). O classificador
é `woweconomy/analytics/mog_groups.py`; o CLI é `scripts/mog_groups.py`; os
testes estão em `tests/test_mog_groups.py`.

O objectivo é dividir **todos os mogs** — não só os teus drops — em grupos que se
possam operar uma vez no TSM, e dar uma string por grupo. O classificador não
decide se um item é bom; decide **que tipo de mercado é** e, por isso, que âncora
de preço faz sentido.

**A classificação é por aparência (o look), não por item.** As variantes que
partilham a mesma aparência ficam no mesmo grupo *mesmo que só uma delas tenha
preço*: o look é descrito por um *dador* (a variante com mais movimento regional
e, em empate, a maior venda realizada) e as outras variantes herdam os seus
números. É isto que tira os itens do Base Group: o TSM trata um item de sufixo
aleatório (ex. “Greenweave Bracers of the Fireflash”) pelo **id base** (`i:9768`),
que já está no grupo. O CLI tem `--items`: `all` (default, todos os 127 155 itens
com aparência; os looks sem preço ficam `unknown`), `variants` (só variantes de
aparências com preço, 38 746) e `priced` (só itens com preço regional, 14 813 —
o comportamento antigo).

---

## A. Recomendação executiva

**Não uses uma string para tudo. Usa operações por comportamento de mercado; estes grupos são rótulos, não instruções para vendor:**

1. **Liquid** (612 mogs): movimento estimado ≥0,1/dia; estar no valor regional e deixar o undercut de 1c ganhar a parede; 24 h.
2. **Collector** (10 891): audiência/valor de coleccionador; segurar o valor (sem desconto, undercut 0c); 48 h; sem cancel scan automático.
3. **Slow** (9 023): movimento positivo 0,01–0,1/dia; desconto pequeno (−10 % sobre o valor regional); 48 h; não cortar por baixa velocidade.
4. **Very slow** (11 516): movimento positivo <0,01/dia e sem perfil de collector; desconto maior (−40 %) para mover; 48 h; desistir ao fim de 3 expirações.
5. **Unproven** (5 864): preço existe, movimento TSM observado = 0; sonda cautelosa a −50 %, 24 h, desistir ao fim de 3 expirações.
6. **Unknown** (89 249): sem aparência com preço útil; não se posta (`postCap 0`).

**Conflict** (8 210) continua como flag que atravessa os grupos, não como classe de procura. **Vendoring fica separado:** deixa tentar até 5 falhas a 24 h; só candidata a vendor se o ask local estiver ≤ 5 depósitos estimados (`150% vendorsell`) e se destroy não valer mais. Rever manualmente antes de vender.

O grupo **Conflict** (8 210 mogs) é um override: a parede e o
histórico/mercado discordam por mais de 2×; ancora no valor regional/histórico,
não na parede.

**Nenhuma operação ancora no `dbmarket`/`dbminbuyout` do reino.** Num reino sem
concorrência, o teu próprio anúncio *é* o mercado do reino: postar em cima de
`dbmarket` fecha o ciclo (baixas o teu preço → o `dbmarket` baixa → postas mais
baixo), e o TSM nunca te undercuta a ti próprio
(`AuctioningOperation.MakePostDecision` iguala quando o leilão mais baixo é
`isPlayer`), pelo que o preço escolhido fica congelado. A âncora é o **mercado
(ask) regional** — `dbregionmarketavg`, depois `dbregionhistorical` e o
`dbhistorical` (60 dias) do reino como último recurso —, com o piso
`1.5 * vendorsell`. O `dbregionsaleavg` fica **de fora**: é a média de *vendas* da
região e está contaminada por snipes (secção B).

**Melhor reino medido: Silvermoon** — o melhor ouro total por catálogo no modelo
de `probabilidade_venda.md` (0,7 %/mês na listagem mediana, 421 coleccionadores
na amostra). Draenor e Kazzak vêm logo atrás; Ravencrest e Howling Fjord são os
melhores para **prateleira vazia** (7–64 % vazia), não para ouro total. Tens
personagem em Silvermoon, Draenor, Kazzak, Twisting Nether, Auchindoun, Argent
Dawn, Blackrock e Dragonmaw (Journalator).

**Mas não movas as 107 peças de coleccionador para Silvermoon às cegas.** O ganho
de compradores é de 10–20×, a concorrência é 2×, e as peças estão no Dragonmaw;
só compensa se as conseguires transferir (warband/transferência). A recomendação
provisória é: **colector novo/flips em Silvermoon; stock actual no Dragonmaw**,
a não ser que a transferência seja trivial. Testa 10 peças, não 107.

**A maior vitória não é o reino, é o grupo:** 107 dos 140 itens do inventário são
de procura lenta com valor de parede; a string actual tenta adivinhar o preço
pelo `avgSale` e falha. A string de colector lista na parede e deixa o comprador
aparecer. O segundo ganho é controlar depósitos por ask e vendor value; falhas e `spd` baixo não provam que um mog está morto (ver `vendoring_operation.md`).

---

## B. Auditoria da estratégia actual

O que a string actual faz bem:

- **O piso de vendor** (`max(valor, 1.5 * vendorsell)`) evita vender abaixo do
  depósito; nos teus dados não morde em nenhum item, mas é seguro.
- **A regra dos 75 %** do mínimo é razoável num reino cheio: recusa os casos em
  que as fontes discordam (dump ou anúncio isolado).
- **O `dbminbuyout` é a única fonte local**; o resto é regional ou histórico.

Onde falha:

- **A normal `avg(dbmarket, 400%dbregionsaleavg)` está acima da parede em 45,6 %
  dos itens antigos e 80,8 % dos novos.** Nunca ganha a venda; o `avgSale` é uma
  média com snipes, não o preço de coleccionador. Nas 28 vendas com `avgSale`,
  a mediana foi **4,0× o avgSale**; **71,4 %** venderam acima de 175 % dele;
  **45,5 %** a ≥80 % da parede. Burning War Axe: 2 713 g (a parede) vs 482 g
  (175 % do avgSale); Runed Copper Belt: 65,7 g vs 0,35 g.
- **O mínimo antigo `iflt(dbminbuyout, floor, 1e9, floor)`** bloqueia 63/63
  candidatos de arbitragem e 115/224 (51,3 %) dos itens líquidos. A lógica está
  invertida: parede abaixo do floor é o estado normal de um item líquido.
- **`DBRegionSoldPerDay` é por reino (por AH), não o total regional.** O
  relatório anterior dividia por 92; o corte `spd < 0,1` era 10× agressivo. O
  corte correcto é **`< 0,01`**; `0,01–0,1` é espera na parede, não lixo.
- **`DBHistorical` é a média de 60 dias do `DBMarket` (ask), não das vendas.**
  É um fallback suavizado, não uma prova de transacção; num reino vazio pode ser
  preço de museu.

Assunções que não se sustentam: multiplicadores universais (175 %, 200 %, 400 %)
sem evidência; o `avgSale` como preço de coleccionador; o rótulo “High/Full” do
reino como sinal de mercado; a divisão por 92. O que se sustenta: **a parede
local é a referência de competição, o valor vem da região, e a liquidez é o
filtro.** (Esta secção audita a string antiga; a secção D substituiu-a.)

---

## C. Oportunidades no inventário

Os grupos gerados cobrem **127 155 itens** (a aparência inteira, com e sem preço); estes totais são a execução do classificador de 19/10 (Spinebreaker, `--items all`) e não os 140 itens do modelo de coleccionadores:

| grupo | itens | operação sugerida |
|---|---:|---|
| liquid | 612 | no valor regional; undercut 1c; 24 h; mínimo 70 % |
| collector | 10 891 | segurar valor (sem desconto, undercut 0c); 48 h; sem cancel scan |
| slow | 9 023 | −10 %; 48 h; undercut 1c; mínimo 70 % |
| very_slow | 11 516 | −40 %; 48 h; desistir ao fim de 3 expirações |
| unproven | 5 864 | sonda a −50 %; 24 h; desistir ao fim de 3 expirações |
| unknown | 89 249 | não se posta (`postCap 0`); evidência insuficiente |
| **conflict** (flag) | 8 210 | override no valor regional/histórico; 24 h; desistir ao fim de 3 expirações |

Exemplos:

- **Liquid:** Slippers of the Hissing Cult — parede 39 624, valor regional acima
  dela (o undercut de 1c ganha a parede); Tarnished Dawnlit Corsair's Boots —
  parede 293, `avgSale` 570, `spd` 4,2/dia; os três Dawnlit são `conflict`
  (parede/histórico fora de 2×), mas o volume é real.
- **Collector:** Pagan Vest — `avgSale` 1 422, sem parede; Evocator's Blade —
  `avgSale` 950, parede 14 951; Mystic's Robe — 585 / 9 966; Wrangler's Buckler —
  408 / 74 727 (histórico 205 223). A parede é o preço; o `avgSale` é o chão
  contaminado.
- **Slow:** Prospector's Buckler — 367, sem parede; Barbaric Cloth Cloak — 250,
  sem parede; Stout Battlehammer — 32 / 189 (−10 % sobre o valor regional).
- **Very slow / patient:** Grunt's Shield — 204 / 1 562; Brackwater Vest — 158 / 3 033; Headhunter's Cloak — 89 / 3 782. A baixa velocidade pode justificar limitar depósitos apenas quando o ask local se aproxima do custo acumulado; não os chamar mortos.
- **Conflict:** 35 itens; a parede e o histórico discordam. Exemplo: Wrangler's
  (parede 74 727, histórico 205 223) — listar no histórico seria museu; listar na
  parede é o preço. A string de conflict ancora no histórico **só quando a
  parede é um dump**; nos outros, a parede manda. revê um a um.

O **vendoring** é outra decisão, baseada no ask corrente vs depósitos perdidos: `Min number of expires = 5`, Auctioning `Max Expires = 5` apenas no grupo de teste; para 24 h o depósito padrão é cerca de `30% vendorsell`, então 5 posts equivalem a `150% vendorsell`. Candidata-se a vendor quando `dbminbuyout <= 150% vendorsell`, depois de rever manualmente `destroy` e preço realista. `avgSale ≤100g` ou `spd <0.01` isolados não são critérios para eliminar. Ver `vendoring_operation.md`.

---

## D. Política de preço — strings por grupo

As sete operações partilham uma âncora, um piso e a **mesma estrutura de bandas**;
diferem apenas no factor aplicado a cada banda:

```
V     = first(dbregionmarketavg, dbregionhistorical, dbhistorical)
floor = 1.5 * vendorsell
```

`V` lê a **região** (mercado regional → histórico regional → histórico de 60 dias
do reino) e nunca o `dbmarket`/`dbminbuyout` do reino — num reino sem
concorrência o teu próprio anúncio *é* o mercado, e ancorar nele congela o preço
no primeiro valor postado. Também **não** usa `dbregionsaleavg`: é a média de
*vendas* da região e está contaminada por snipes (secção B). `first()` escolhe a
primeira fonte válida, `min()/max()` ignoram fontes sem dados. **Testa no price
tester do TSM antes de aplicar**; o editor confirma tokens, não semântica.

### Bandas

`ifgt(a, b, x, y)` é "se `a > b` então `x` senão `y`". Cada preço é uma cadeia de
três `ifgt` sobre `V`, por isso uma só operação cobre o grupo inteiro sem tratar
50 g e 50 000 g da mesma maneira:

```
normal = max(ifgt(V, 50000g, 0,75*V,
              ifgt(V, 5000g, 0,65*V,
                ifgt(V, 500g, 0,55*V,
                  0,45*V))), floor)
```

Cada célula é `normal / minimum / maximum`:

| grupo | > 50 000 g | 5 000-50 000 g | 500-5 000 g | < 500 g |
|---|---|---|---|---|
| liquid | 0,75 / 0,40 / 1,20 | 0,65 / 0,35 / 1,10 | 0,55 / 0,30 / 1,00 | 0,45 / 0,25 / 0,90 |
| collector | 0,85 / 0,50 / 1,60 | 0,78 / 0,45 / 1,45 | 0,68 / 0,40 / 1,30 | 0,58 / 0,35 / 1,15 |
| slow | 0,80 / 0,45 / 1,35 | 0,72 / 0,40 / 1,25 | 0,62 / 0,35 / 1,15 | 0,52 / 0,30 / 1,05 |
| very_slow | 0,82 / 0,45 / 1,50 | 0,74 / 0,42 / 1,35 | 0,64 / 0,38 / 1,25 | 0,55 / 0,32 / 1,15 |
| unproven | 0,75 / 0,40 / 1,25 | 0,68 / 0,36 / 1,18 | 0,58 / 0,32 / 1,10 | 0,48 / 0,28 / 1,00 |
| conflict | 0,90 / 0,50 / 1,80 | 0,82 / 0,46 / 1,60 | 0,72 / 0,42 / 1,40 | 0,62 / 0,36 / 1,25 |

### Porque é que o factor sobe com a banda

- A rotação medida sobe com o preço: em Silvermoon, 2,8 %/dia na faixa 50-500 g
  contra 5,7 %/dia acima de 5 000 g. Item caro vende-se *mais* depressa, em
  proporção ao stock, e por isso aguenta factor mais alto.
- Dentro da mesma banda, os rótulos de comportamento quase não ordenam (slow
  2,6 %, very_slow 3,8 %, unproven 4,0 %, liquid 4,7 %, collector 6,1 %/dia): a
  banda explica mais do que o rótulo. Isto inverte a tabela anterior, que
  descontava mais quanto *menos* o item se mexia.
- O mercado regional é um **tecto**, não um piso: paredes acima de 120 % de `V`
  caem para ~0,75× em 27 h, paredes abaixo ficam paradas. Postar acima de `V`
  é escolher ser o próximo a descer.
- `liquid` é o único grupo cujo ask caiu ao longo de 78 dias (0,59×), e por isso
  leva os factores mais baixos e 24 h; `slow`, `very_slow`, `collector` e
  `unproven` estão planos (0,94-1,00×) e são segurados, não descontados.
- Os coeficientes são **provisórios**: a direcção é suportada pelos dados, a
  magnitude não tem amostra de vendas suficiente para ser calibrada.

### Notas

- **unknown** nunca posta (`postCap 0`): não há nenhuma variante com preço.
- Se `dbregionmarketavg`, `dbregionhistorical` e `dbhistorical` estiverem todos
  vazios, a expressão é inválida e o TSM não posta: é o comportamento seguro.
- São sete políticas distintas; antes `slow`/`very_slow` partilhavam a mesma
  (`thin`) e `unproven`/`unknown` a mesma recusa, que é a razão de as operações
  parecerem todas iguais.

### Operações

| campo | valor | motivo |
|---|---|---|
| `postCap` | 1 (0 em `unknown`) | um anúncio por item |
| `stackSize` | 1 | transmog não empilha |
| `priceReset` | **`none`** | parede abaixo do mínimo → **não postar**, em vez de saltar para o máximo |
| `aboveMax` | `maxPrice` | parede acima do tecto → postar no tecto, não a acompanhar |
| `cancelRepost` / `cancelUndercut` | `false` | sem cancelamentos automáticos |
| `maxExpires` | ver abaixo | expirações **desde a última venda** |

`maxExpires` não é uma duração: o TSM compara-o com a fonte `NumExpires`, que
conta as unidades expiradas **desde a última venda** desse item, e uma venda
repõe o contador. É, portanto, uma janela de retenção em ciclos de post, e é o
campo que muda com o **perfil do reino** (medido, não escolhido à mão):

| classe do reino | janela | `liquid` (12/24/48 h) | `collector`/`slow` (48 h) | export |
|---|---:|---:|---:|---|
| `thin` (pouco competitivo) | 60 dias | 119 / 59 / 29 | 29 | `mogs_groups_<reino>.tsm` |
| `balanced` (intermédio) | 30 dias | 59 / 29 / 14 | 14 | `mogs_groups_<reino>.tsm` |
| `deep` (competitivo) | 14 dias | 27 / 13 / 6 | 6 | `mogs_groups_<reino>.tsm` |

O `duration` de cada operação é o maior entre o do reino e o mínimo do grupo
(`collector`, `slow` e `very_slow` nunca são mais rápidos que 48 h: um item
lento não ganha por ser repetido). A tabela completa, com os factores por
reino e a medição que os sustenta, está em
[`mog_groups_realm_profiles.md`](mog_groups_realm_profiles.md); o export
correspondente sai de `python scripts/tsm_groups_export.py --write --realm
<reino>` (ou `--all-realms`).

### Um perfil por reino

**A string de import não leva perfil.** Os dois formatos que o `GroupImport`
aceita são:

- novo (`LibSerialize` + `LibDeflate`): `magic, versão, groupName, items, groups,
  groupOperations, operations, customSources`;
- antigo (`AceSerializer`): `groupExport`, `operations`, `groupOperations`.

Nenhum tem campo de perfil, e o import aplica sempre ao **perfil activo**. Também
não há comando que crie perfis: `/tsm profile <nome>` só muda para um perfil que
já exista (se não existir, imprime a lista dos válidos). A criação é na UI.

Receita por reino (uma vez por conta):

1. `TSM → Settings → General → Profiles`, escreve `mogs-<reino>` no
   campo *Enter profile name* e Enter. O TSM cria o perfil com os defaults e
   passa a usá-lo nessa personagem.
2. `Groups → Import`, cola `mogs_groups_<reino>.tsm` e importa.
3. Repete noutra personagem se precisares; o perfil é **por personagem**
   (`_currentProfile[char]` no `SettingsDB`), portanto um alt novo no mesmo reino
   começa em `Default` e precisa de `/tsm profile mogs-<reino>` uma vez.

Notas:

- `profile.userData.groups/items/operations` são do perfil: importar os 127 155
  itens em dois perfis guarda a lista duas vezes (a string são 1,1 MB; em
  SavedVariables são alguns MB por perfil). Se só usares um reino, basta
  `Default` + um perfil.
- Importar duas vezes no mesmo perfil **não** substitui: o grupo de topo já
  existe e o TSM cria `Liquid 1`, `Collector 1`, … Apaga os grupos antes de
  reimportar.
- O `maxExpires` é uma custom string (`AddCustomStringSetting`), por isso a
  alternativa a perfis é fazê-lo depender do item (ex.: `DBRegionSaleRate`) em
  vez do reino; não há nenhuma fonte TSM que devolva o reino, portanto um perfil
  por reino é a única forma de variar o número *pelo reino*.
- **"Share operations between all profiles" tem de estar desligado**
  (`globalOperations`, default `false`): ligado, todas as operações passam a ser
  partilhadas e o `maxExpires`/duração de um reino passa a valer em todos.
- Entre dois reinos com perfil mudam os **preços** (factor local medido ×
  postura), a **duração** e o **`maxExpires`**. Os grupos e os itens são
  idênticos.
- Reinos **conectados** (Dragonmaw, Spinebreaker, Stormreaver, Haomarush) são o
  mesmo mercado: o `tsm_market` guarda o mesmo vector AuctionDB sob os quatro
  nomes. O perfil é por **casa de leilão**, não por slug.
- Para a campanha de liquidação com prazo, o export é outro (ver
  [`../token_rush/token_rush.md`](../token_rush/token_rush.md)):
  `liquidity_mogs_groups_<reino>.tsm`, com um grupo e uma operação por item.


Vendoring é uma operação separada: 5 expirations e candidato só quando ask local está perto/abaixo de `5 × depósito de 24 h` (= `150% vendorsell` em regra padrão). Requer revisão manual; não transformar o grupo de movimento em vendor list.

O `Conflict` é um grupo filho com operação própria; o TSM usa a operação do grupo
mais específico. O `Dead` pode ir para a `Vendoring Operation` de
`vendoring_operation.md` (avgSale ≤100 g) ou para disenchant se o `destroy` for
alto.

---

## E. Backtest — o que os dados suportam

**Replay das 28 vendas com `avgSale` (22 com parede):**

| medida | valor |
|---|---:|
| venda mediana / avgSale | **4,0×** |
| vendas acima de 175 % do avgSale | **71,4 %** (20/28) |
| vendas a ≥80 % da parede | **45,5 %** (10/22) |
| Burning War Axe: venda vs 175 % avgSale | **2 713 g vs 482 g** |
| Runed Copper Belt: venda vs 175 % avgSale | **65,7 g vs 0,35 g** |

**Sobre 7 110 itens com preço (números da string anterior):** a normal antiga
ficava a 0,66× a parede na mediana (0,63× nos lentos, 1,56× nos antigos líquidos,
1,84× nos novos), e o cap de 90 % cortava os líquidos sem tocar nos lentos. A
secção D substituiu as strings; estes números servem só de baseline da auditoria.

**Journalator:** 204 vendas vs 259 expirações (sell-through 44 %); ~109 g de
depósito a 24 h, ~218 g a 48 h (83 % da receita registada); 92 % do depósito vem
de itens com retorno zero.

**O que este backtest não estabelece:** não é cronológico; não há o preço da
parede no momento de cada venda; a amostra são 28 vendas tuas, não o mercado;
não há out-of-sample; e uma venda que desapareceu não é uma venda confirmada.
O que ele estabelece é a ordem: **o avgSale está baixo, a parede está perto do
preço realizado, e o cap de 90 % bate a regra dos 175 %.**

---

## F. Ranking de reinos

Base: `probabilidade_venda.md` (modelo de 30 dias com 3 130 contas DfA; 90 dos
92 reinos) e `melhor_reino.md`.

| # | reino | porque | personagem |
|---|---|---|---|
| 1 | **Silvermoon** | melhor ouro total; 0,7 %/mês na mediana; 421 coleccionadores; 2 % de prateleira vazia | sim |
| 2 | **Draenor** | 104 compradores no Dustbringer; 2º no total | sim |
| 3 | **Kazzak** | bom para peças de preço médio/alto | sim |
| 4 | **Ravencrest** | prateleira vazia + 65 compradores no Primalist; melhor para listagem isolada | não vi no Journalator |
| 5 | **Howling Fjord** | 49 compradores no Primalist; prateleira vazia | não vi no Journalator |

A escolha não é “o reino com mais coleccionadores”: é **compradores × preço ×
probabilidade**. Silvermoon ganha no total; Ravencrest ganha numa listagem
isolada. A concorrência vale 2×; os compradores valem 10–20×. A amostra
700–800 k está subrepresentada (79 de 4 746 contas com bitmap), o que pode mudar
o ranking; não é uma certeza.

**Recomendação provisória:** Silvermoon para o colector novo; Dragonmaw para o
stock actual e para o líquido. Testa 10 peças em Silvermoon (se transferíveis)
antes de mover o catálogo.

---

## G. Plano de 7 dias

1. **Dia 1** — corre `python scripts/mog_groups.py` e `python scripts/mog_realm_profiles.py` (mede a concorrência e o nível local de cada reino) e depois `python scripts/tsm_groups_export.py --write --realm <reino>`; escolhe/cria o perfil TSM desse reino e importa `reports/mog_groups/mogs_groups_<reino>.tsm` (uma linha) para criares `Liquid`, `Collector`, `Slow`, `Very_Slow`, `Unproven`, `Unknown` e `Conflict` com as operações `Mogs ...` já atribuídas; não usar nenhum grupo como vendoring automático.
2. **Dia 2** — posta os 4 liquid e os 107 collector no Dragonmaw; registra a
   parede e o teu preço; não faças cancel scan.
3. **Dia 3** — leva 10 collector a Silvermoon (se transferíveis); compara a
   parede e os compradores com o Dragonmaw.
4. **Dia 4** — revê o grupo de cinco expirações: compara ask atual com depósito acumulado e `destroy`; vendor só as peças cuja parede já está perto/abaixo do custo perdido, protege as de vendor alto.
5. **Dia 5** — revê os 35 conflict um a um; o histórico é o default, mas a
   parede pode ser o preço.
6. **Dia 6** — mede vendas, expirações e depósito no Journalator; calcula o
   sell-through por grupo.
7. **Dia 7** — decide: collector fica em Silvermoon ou volta; ajusta os cortes
   (90 % / 80 % / 70 %) com os dados da semana.

---

## H. Repetir a análise

```
python scripts/mog_groups.py --realm spinebreaker --out reports/mog_groups
python scripts/mog_groups.py --items variants        # só looks com preço (38 746)
python -m pytest tests/test_mog_groups.py -q
```

Saída: `reports/mog_groups/mog_groups.csv` (127 155 itens, uma linha por id com `appearance_id` e `variants`), um CSV/TXT por grupo (`liquid`, `collector`, `slow`, `very_slow`, `unproven`, `unknown`, `conflict`) e resumo no terminal. O `.txt` tem uma linha `i:<id>,i:<id>,...` pronta a colar no Import do grupo do TSM 4; o CSV inclui preço, movimento estimado, parede, histórico, `P30`, buyers e razão.

`python scripts/tsm_groups_export.py --write` junta tudo numa única linha
AceSerializer em `reports/mog_groups/mogs_groups.tsm` (~1,10 MB, a referência
regional): os grupos com
nome (`Liquid`, `Collector`, `Slow`, `Very_Slow`, `Unproven`, `Unknown`,
`Conflict`), os itens e a Auctioning Operation de cada um (`Mogs ...`, strings da
secção D) num só `Groups -> Import`. `--realm <reino>` grava
`mogs_groups_<reino>.tsm` com o perfil **medido** desse reino (factor local,
postura, duração e `maxExpires`); `--all-realms` grava um ficheiro por reino com
perfil. Como o TSM só deixa um item num grupo, os
8 210 itens da flag `conflict` saem do grupo primário e ficam no grupo `Conflict`
(por isso o import mostra Liquid 351, Collector 7 639, Slow 5 977, Very_Slow
9 865, Unproven 5 864, Unknown 89 249, Conflict 8 210 = 127 155); com
`--conflict-inline` ficam onde estão e o grupo `Conflict` não é criado. Isto não
foi executado dentro do addon: importa primeiro num perfil de teste. O gerador apaga apenas os exports obsoletos `thin`/`dead` da pasta de saída para evitar listas antigas contraditórias.

Cada grupo leva `Auctioning = { 1 = "Mogs <Grupo>", override = true }`: o TSM só
liga a operação quando vê `override = true` na tabela do módulo (sem isso o grupo
herda a raiz do perfil e a operação é descartada). O TSM importa estes grupos
aninhados sob uma raiz (`Imported Group`). No editor de grupos, o painel
*Ungrouped Items* mostra as variantes (bonus id / item level) como desagrupadas
enquanto *Show ungrouped items as* estiver em **Specific Item** — os grupos usam o
id base (`i:9768`, a que os sufixos aleatórios “of the Fireflash” se ligam); muda
para **Base Item** para veres que já estão agrupados.

Para medir: vendas realizadas, sell-through, tempo até à venda, expirações por
venda, depósito perdido, idade do inventário e tempo de gestão. O script não
mede isto; o Journalator e o TSM Accounting medem.

---

## Limitações

- O classificador é uma **heurística**, não uma previsão: 0,1/0,01 separam rótulos de movimento estimado; `very_slow` continua positivo; zero é `unproven`; dado ausente é `unknown`. Não inferir decisão de vendor desses cortes.
- Os dados de coleccionador cobrem **140 itens** do inventário; os 127 155 mogs
  usam só TSM (região + parede local). `P30`/`buyers` só existem para os 140 (no
  nível do look agrega-se o melhor `P30`/buyers das variantes).
- A agregação por aparência usa um **dador** (a variante com mais movimento): um
  look com uma variante rara e caríssima herda-lhe o preço, e o `unknown` de
  89 249 itens são looks sem nenhuma variante com preço (por isso é grande).
- Os itens de sufixo aleatório do cliente **não têm id próprio** em
  `db2_item_appearance`: o TSM mostra-os pelo id base (`i:<id>`), que é o que os
  grupos usam. Não há id de sufixo a agrupar.
- `DBRegionSoldPerDay` é por reino (por AH); a calibração do Wool Cloth (97/dia
  vs 52 812/dia) ainda não encaixa e pede uma medição directa.
- A parede é o último snapshot (30+ min); um anúncio a 1 c pode entrar e sair.
- O `avgSale` é a média de vendas e compras da região (inclui snipes); o
  `DBHistorical` é a média de 60 dias do `DBMarket` (ask).
- O ranking de reinos é o modelo existente, com a falha de amostragem
  700–800 k; não é um teste comercial.
- Não corri as strings dentro do TSM; o editor confirma sintaxe, não
  comportamento. O preço real só se vê no post scan.
