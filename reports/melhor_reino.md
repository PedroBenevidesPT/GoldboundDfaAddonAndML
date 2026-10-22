# Onde vender transmog — decisão para a operação (EU Retail)

Data: 2026-10-19.  Dados: `C:\economics\woweconomy\dfa\dfa.db` (contas, bitmaps,
escalões) e `tsm_markets.db` (preços TSM, snapshot de reino de 2026-10-19 07:02).
Modelo e ranking: [scripts/best_realm.py](../scripts/best_realm.py) (só leitura).
CSV por cabaz: `reports/melhor_reino_inv.csv`, `melhor_reino_farm.csv`,
`melhor_reino_rare.csv`; índice combinado: `melhor_reino_combined.csv`.

> **Correcção (19/10, mais tarde).** O import do Journalator usado aqui estava
> incompleto: os números reais são **6 344 vendas e 24 559 g**, não 204 vendas e
> 261 g. E o filtro `binding=1` usado para listar "BoE com aparência" selecciona
> BoP (o BoE é `binding=2`), pelo que "0 vendas de BoE, 70 expirações" não
> descreve as peças de transmog. Análise refeita com o export de inventário de
> 19/10: [analise_inventario_2026-10-19.md](analise_inventario_2026-10-19.md).

---

## A. Decisão executiva

**Vender no Antonidas.**  É a melhor combinação dos dois cabazes que dão
dinheiro — 2.º no de aquisição (45 383 g/30 d) e 2.º no de peças raras
(1 187 762 g/30 d) — e o menos concorrido dos cinco primeiros: **86,2 %** do
cabaz de aquisição já tem listagem (contra 89,6–94,7 % nos outros quatro), com
`minBuyout/market` = 1,00 e sem decaimento recente.  O teu stock actual rende lá pouco (640 g/30 d, 16.º),
mas esse stock vale menos de 1 000 g/mês em *qualquer* reino — não é ele que
decide a operação; o que decide é onde o catálogo que vais farmar encontra
compradores.

O cluster onde já tens personagens — **Spinebreaker/Dragonmaw** (Dragonmaw,
Haomarush, Spinebreaker, Stormreaver, Vashj) — fica em **52.º** no cabaz do
inventário, **41.º** no de aquisição e **45.º** no de raras.  Ficar não é uma
opção defensável: o próprio Journalator mostra 60 peças BoE de transmog listadas
aí com **0 vendas e 70 listagens expiradas**.

---

## B. Shortlist (5 candidatos)

`n` = contas com score ≥ 900 000 no cluster (censo do segmento, ver §C);
`d100` = score do 100.º melhor colector (profundidade da escada);
ouro/30d = valor esperado do cabaz no modelo; `lista%` = fracção do cabaz que já
tem listagem no cluster; `und` = mediana `minBuyout/market`; `dec` = mediana
`recent/historical`; confiança = cobertura de preços e tamanho da amostra.

| # | cluster | acesso | n | d100 | inventário | aquisição (150) | raras (50) | lista% | und | dec | confiança |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | **Antonidas** | novo | 53 | 829 903 | 640 g (#16) | **45 383 g (#2)** | **1 187 762 g (#2)** | 86 % / 15 % | 1,00 | 1,00 | alta/baixa |
| 2 | Echsenkessel | novo | 64 | 832 532 | 620 g (#17) | 46 592 g (#1) | 920 141 g (#4) | 92 % / 63 % | 1,00 | 1,00 | alta/baixa |
| 3 | Draenor | novo | 71 | 868 009 | **1 093 g (#4)** | 42 283 g (#5) | 842 306 g (#5) | 95 % / 76 % | 0,999 | 1,00 | alta/baixa |
| 4 | Kazzak | novo | 67 | 842 700 | 553 g (#21) | 41 471 g (#7) | **1 473 582 g (#1)** | 92 % / 28 % | 1,00 | 1,00 | alta/baixa |
| 5 | Twisting Nether | novo | 57 | 827 794 | 408 g (#33) | 44 245 g (#4) | 836 066 g (#6) | 90 % / 80 % | 1,00 | 0,99 | alta/baixa |

(As duas percentagens são do cabaz de aquisição e do de raras, respectivamente.
`lista%` alto = mais prateleira ocupada; `und`/`dec` perto de 1,00 = preços
estáveis.  O acesso é inferido do Journalator, que só tem Dragonmaw: se tiveres
personagens num destes clusters, o custo de mudança desaparece e a decisão fica
só com o mérito de mercado.)

**Porque não os outros:**

- **Karazhan** é 1.º no inventário actual (1 715 g) mas 24.º na aquisição e 26.º
  nas raras, com 27 colectores — é o vencedor de um cabaz de 57 peças baratas,
  não de uma operação (e é frágil: muda com o tecto de preço, §F).
- **Marécage de Zangar** (3.º inventário, 12.º aquisição) e **Bronze
  Dragonflight** (10 colectores) são bons preços em amostras pequenas.
- **Silvermoon** tem a escada mais profunda (869 304) mas é o mercado mais
  saturado: 98,7 % do cabaz de aquisição já está listado e os preços do cabaz
  barato estão 6,6 % abaixo do market.
- **Kazzak** só ganha pelas raras, e essas são o cabaz menos fiável de todos
  (§F): 0,26 vendas/mês e preços de pedido, não transacções.

---

## C. Evidência de mercado

### Amostra (o que ela é e o que não é)

- 15 118 contas, 263 reinos, 92 clusters connected.  A escada regional `eu` tem
  10 749 contas ≥ 679 378; as varreduras locais cobriram 3 108 contas no cluster
  Dragonmaw (até 90k de score), 1 400 em Auchindoun e 199 em Silvermoon.
- Só **1 904 contas** têm score ≥ 900 000, e essas estão **100 % bitmapped**.
  Abaixo disso a cobertura cai a 40 % (800–900k) e 2 % (680–800k).  Por isso o
  modelo usa o segmento ≥ 900 000: é o único em que a amostra é um censo, não uma
  fracção enviesada.  Não é a população do reino — é o topo que compra transmog
  caro.
- Distribuição desse segmento nos candidatos (n, mediana, p90, máximo):
  Draenor 71 / 768 020 / 947 538 / 1 162 630; Silvermoon 76 / 776 240 / 934 315 /
  1 195 016; Kazzak 67 / 790 123 / 1 001 805 / 1 210 361; Echsenkessel 64 /
  773 792 / 965 836 / 1 170 507; Twisting Nether 57 / 776 062 / 992 312 /
  1 169 943; Antonidas 53 / 770 396 / 938 498 / 1 208 834; **Spinebreaker
  (Dragonmaw) 14 / 225 819 / 466 244 / 1 152 515**.
- Verificado: a escada do cluster Dragonmaw tem o 100.º melhor em 618 681, contra
  869 304 em Silvermoon e 829 903 em Antonidas.  O mercado onde estás é raso.

### Demanda pelos teus itens

- Inventário: 238 linhas, 65 itens com aparência, **57 BoE com aparência**, 56
  com preço (valor de catálogo regional ≈ 42 739 g; mediana 308 g; 6 peças entre
  1 000 e 10 000 g; 1 acima de 10 000 g).
- Em Antonidas, **43 dos 56** têm pelo menos um comprador no segmento de topo e
  **19** têm probabilidade de venda ≥ 1 %/30 d.  Os três `Tarnished Dawnlit`
  (258921, 258934, 258962) dominam o valor esperado: 95 %, 95 % e 81 % de venda
  em 30 dias, porque são peças de conteúdo actual com `sold_per_day` regional de
  4,2/dia — vendem em qualquer reino, a margem é que é pequena.
- Cabaz de aquisição (150 itens BoE com mais ouro em falta no topo): preços de
  cluster 60 000–200 000 g, EU 1 000–1 600 compradores por item, P30 0,1–0,8 %.
  É 1 venda em cada ~1,5 meses para o cabaz inteiro — a realidade do segmento.
- Cabaz de raras (> 100 000 g, 50 itens): 0,15–0,26 vendas/mês; é onde está o
  ouro esperado e também toda a variância.

### Mecânica (verificação externa)

- Reinos ligados partilham **uma só casa de leilão** para itens não-commodity;
  é por isso que a unidade de decisão é o cluster e não o reino
  ([Warcraft Wiki — Connected Realms](https://warcraft.wiki.gg/wiki/Connected_Realms)).
  As commodities são regionais; transmog não é, por isso o preço de reino da TSM
  é o preço relevante.
- O modelo desconta **5 %** de corte da casa em cada venda.  É a taxa standard do
  Retail; o relatório não a mediu no jogo — é uma hipótese, não uma observação.

### Concorrência (TSM)

- Preços de reino frescos (2026-10-19 07:02; 1 180 279 linhas em 92 clusters) e
  região de 2026-10-18 (31 184 linhas).  `tsm_market` (AppData, com
  `num_auctions`) só existe para 8 clusters e está **velho** — o mesmo item tem
  185 000 000 de cobre de `minBuyout` lá contra 2 926 500 no CSV fresco (18 500 g
  contra 293 g).  Serve para ordenar profundidade, nunca para preço.
- `minBuyout` é pedido, não transacção: não há histórico de vendas por item e
  reino em lado nenhum da base.  O que existe é `market_value` (média de
  listagens) e `recent/historical` (decaimento interno do reino).
- Evidência própria (Journalator, `journal_stats`, 92 linhas, só Dragonmaw):
  **204 vendas, 261 g no total, todas de materiais**; das 60 peças BoE com
  aparência listadas, **0 venderam e 70 listagens expiraram**.  É a prova
  directa de que o problema não é só o modelo.

---

## D. Oportunidades item a item (no Antonidas)

`EU` = contas do segmento que ainda não têm a aparência (de 1 904); `falta` = em
Antonidas; `P30` = probabilidade de venda em 30 dias do modelo.

### Primeiro: o valor na AH não é dinheiro

O TSM mostra o stock acima de 1,5 M porque soma as tuas próprias listagens ao
preço de mercado (`DBMarket` / region market) — são as duas únicas colunas que
produzem um número dessa ordem, e no `inventory_priced.csv` (205 linhas, `Qty`
incluída) valem 633 k e 853 k; a diferença para 1,5 M vem de listagens e
quantidades que esse ficheiro não cobre.  Nenhuma dessas colunas é dinheiro:

| medida | total do stock | o que é |
|---|---:|---|
| `posta` (recomendado) | 318 696 g | o que se pediria, com tecto local |
| `Min` | 220 257 g | listagem mais barata que existe |
| `market` (DBMarket) | 632 610 g | média das listagens, **é o que o TSM soma** |
| `regMarket` | 852 619 g | média regional |
| `saleavg` (DBRegionSaleAvg) | **44 775 g** | média do que **vende** |
| Journalator, realizado | **261 g** | 204 vendas, todas de material |

A distância entre 632 k e 45 k é a diferença entre "pedido" e "transacção" — e
o Journalator mostra o resultado: das 60 peças BoE com aparência já listadas em
Dragonmaw, **0 venderam, 70 listagens expiraram**.  Um preço alto não é um
activo; é uma tentativa que já falhou 70 vezes.

Isto não se resolve mudando de reino.  `62072`, `132532`, `4686`, `1515` e `4571`
têm entre **1 e 78 contas em toda a UE** a quem falte a aparência: o P30 é
0,0–0,2 % em qualquer cluster, incluindo o Antonidas.  O stock não está mal
localizado; está mal escolhido.  O dinheiro está no catálogo de aquisição (150
peças de queda mundial, 42–46 k g/30 d nos clusters do topo), não nestas 57.

### No teu cluster (Spinebreaker / Dragonmaw)

O cluster onde já tens personagens é o `Spinebreaker` (Dragonmaw, Haomarush,
Spinebreaker, Stormreaver, Vashj).  `inventory_prob.py --realm Spinebreaker`
responde item a item; `--realm Dragonmaw` não devolve nada porque os preços do
cluster estão guardados sob o slug canónico, não sob o nome do reino.

As únicas peças do stock com comprador **local** identificado:

| item | nome | compradores | preço | P30 | esperado |
|---:|---|---:|---:|---:|---:|
| 258921 | Tarnished Dawnlit Corsair's Boots | 8 | 293 g | 99,7 % | 292 g |
| 258962 | Tarnished Dawnlit Warglaive | 18 | 86 g | 90,4 % | 77 g |
| 258934 | Tarnished Dawnlit Sentinel's Girdle | 8 | 50 g | 98,2 % | 49 g |

~418 g em 3 listagens, sem transfer nem alt.  É pouco — mas é a única parte do
stock que vende **onde já estás**, e o preço local da bota (293 g) é acima do do
Antonidas (265 g).  O P30 de 99 % é escala de amostra (14 contas no cluster),
não uma certeza: lê-se como "há 8 compradores conhecidos e 1 listagem".

**Listar já no Antonidas — giro curto, preço transparente**

| item | nome | qty | EU | falta | preço | P30 |
|---:|---|---:|---:|---:|---:|---:|
| 258921 | Tarnished Dawnlit Corsair's Boots | 1 | 31 (2 %) | 2 | 265 g | 95,0 % |
| 258934 | Tarnished Dawnlit Sentinel's Girdle | 1 | 27 (1 %) | 2 | 158 g | 95,0 % |
| 258962 | Tarnished Dawnlit Warglaive | 1 | 79 (4 %) | 5 | 88 g | 80,7 % |
| 8182 | Pellet Rifle | 2 | 388 (20 %) | 15 | 299 g | 3,3 % |
| 2582 | Green Woolen Vest | 1 | 528 (28 %) | 18 | 375 g | 2,0 % |
| 4308 | Green Linen Bracers | 1 | 357 (19 %) | 10 | 215 g | 1,8 % |
| 2568 | Brown Linen Vest | 1 | 660 (35 %) | 19 | 293 g | 1,7 % |
| 6238 | Brown Linen Robe | 1 | 279 (15 %) | 9 | 203 g | 1,6 % |

**Segurar/preçar com paciência — procura alta, venda rara**

| item | nome | qty | EU | falta | preço | P30 |
|---:|---|---:|---:|---:|---:|---:|
| 16008 | Master Engineer's Goggles | 1 | 1 023 (54 %) | 26 | 5 194 g | 0,7 % |
| 15510 | Grunt's Belt | 1 | 941 (49 %) | 27 | 2 399 g | 0,4 % |
| 1801 | Rawhide Shoulderpads | 2 | 717 (38 %) | 17 | 1 581 g | 0,5 % |
| 1791 | Patched Leather Gloves | 1 | 703 (37 %) | 17 | 1 589 g | 0,4 % |
| 1760 | Reinforced Chain Shoulderpads | 1 | 319 (17 %) | 6 | 1 627 g | 0,3 % |
| 3018 | Hide of Lupos | 1 | 173 (9 %) | 0 | 769 g | 0,1 % |

(3018 e 4571 já não têm comprador no topo do Antonidas; ficam para liquidação
lenta ou para outro mercado.)

**Parar de comprar (ou despachar a preço de vendor)**

| item | nome | EU | falta | preço pedido | P30 |
|---:|---|---:|---:|---:|---:|
| 62072 | Robble's Wobbly Staff | 1 (0,1 %) | 0 | 2 224 g | 0,2 % |
| 132532 | Heavy Scale Pants | 1 (0,1 %) | 0 | 3 585 g | 0,0 % |
| 4686 | Barbaric Cloth Cloak | 7 (0,4 %) | 0 | 4 788 g | 0,1 % |
| 15329 | Wrangler's Belt | 78 (4 %) | 1 | 1 305 g | 0,1 % |
| 1515 | Rough Wooden Staff | 50 (3 %) | 0 | 941 g | 0,1 % |
| 1738 | Laced Mail Belt | 1 (0,1 %) | 0 | 910 g | 0,1 % |
| 9763 | Cadet Leggings | 3 (0,2 %) | 0 | 35 g | 0,1 % |
| 4571 | War Knife | 6 (0,3 %) | 0 | 282 g | 0,2 % |
| 2570 | Linen Cloak | 0 | 0 | 104 g | 0,0 % |

**Sem evidência suficiente**

- `246660` Ascension Arrestor's Bracers — BoE épico iLvl 400, sem preço regional
  na TSM e 0 compradores no segmento.  Antes de vender ou comprar, ver o
  `min_buyout` fresco e o `sold_per_day`; o modelo não tem opinião.
- Qualquer peça com aparência mas sem linha em `tsm_region_price` — não há preço,
  não há decisão.

**O que farmar (top do cabaz de aquisição, Antonidas)**

| item | nome | fonte | EU que faltam | falta no cluster | preço | P30 |
|---:|---|---|---:|---:|---:|---:|
| 10627 | Bludgeon of the Grinning Dog | World Drop | 1 429 | 43 | 168 465 g | 0,4 % |
| 870 | Fiery War Axe | World Drop | 1 315 | 43 | 166 399 g | 0,4 % |
| 15529 | Sentry's Leggings | World Drop | 1 435 | 43 | 158 826 g | 0,3 % |
| 2823 | Mo'grosh Can Opener | World Drop | 1 503 | 44 | 146 845 g | 0,2 % |
| 184802 | Jenrette's Jagged Warglaive | World Drop | 1 277 | 38 | 170 000 g | 0,2 % |
| 31221 | Illidari Vambraces | World Drop | 1 234 | 36 | 176 975 g | 0,3 % |
| 164293 | Plundered Horn Pummeler | World Drop | 1 381 | 42 | 150 000 g | 0,4 % |
| 25261 | Mighty Crossbow | World Drop | 1 159 | 34 | 184 538 g | 0,5 % |

São quedas mundiais: farmar é caçar estas peças, não comprá-las.

---

## E. Concorrência e carga operacional

| cluster | cabaz | lista% | `und` | `dec` | `num_auctions`/item | relistagens/mês (100 itens) |
|---|---|---:|---:|---:|---:|---:|
| **Antonidas** | aquisição | 86,2 % | 1,00 | 1,00 | — | 1 500 |
| Echsenkessel | aquisição | 92,4 % | 1,00 | 1,00 | — | 1 500 |
| Draenor | aquisição | 94,7 % | 0,999 | 1,00 | 3,09 | 1 500 |
| Silvermoon | aquisição | 98,7 % | 0,988 | 0,99 | 4,77 | 1 500 |
| Kazzak | aquisição | 91,8 % | 1,00 | 1,00 | — | 1 500 |

- A relistagem é fixa (o jogo expira em 48 h): 100 listagens = 1 500 posts/mês,
  gaste-se ou não.  O que o reino muda é a probabilidade de cada post virar
  venda; por isso o critério é ouro esperado por 100 listagens, não o número de
  posts.
- Onde há contagem de leilões (AppData velho, 8 clusters), Draenor e Silvermoon
  aparecem com 3–4,8 anúncios por item no cabaz — livros profundos.  Os alemães
  (Antonidas, Echsenkessel) não têm contagem; a única medida fresca é `und`/`dec`,
  e nesses o preço não está a escorregar.
- Kazzak tem 100 % do inventário barato listado e `recent/historical` = 0,859
  (preço a cair 14 %): é onde o teu stock barato apodrece mais depressa.

---

## F. Confiança e sensibilidade

**Vencedores por cabaz e por ajuste** (`shrink` 5/10/20 × tecto de preço
1,5/2,0/3,0 × mediana entre clusters):

| cabaz | vencedores possíveis | leitura |
|---|---|---|
| inventário (57) | Karazhan, Marécage de Zangar, Bronze Dragonflight | **frágil**: 3 vencedores em 9 ajustes |
| aquisição (150) | Echsenkessel (6/9), Antonidas (3/9) | estável no tecto conservador |
| raras (50) | Kazzak (9/9) | estável, mas o cabaz é pouco fiável |

- A escolha entre **Antonidas, Echsenkessel, Draenor e Kazzak** está dentro de
  ~5 %: `--combined` dá 0,741 / 0,732 / 0,712 / 0,707 (aquisição 60 %, raras 25 %
  com peso de confiança 0,5, inventário 15 %, cada cabaz normalizado pelo
  máximo).  Não há vencedor estatístico; o desempate operacional é a prateleira
  menos ocupada (Antonidas, 86 %) e o melhor equilíbrio aquisição/raras.
- **Limitações medidas:** (1) o segmento é o topo ≥ 900k, não a população — os
  compradores de peças baratas de nível baixo estão fora dele, pelo que os
  valores absolutos do inventário são um piso; (2) a amostra DfA não é aleatória
  (quem se regista no site), e as varreduras locais enviesam Dragonmaw/Auchindoun
  para *baixo* score; (3) não existe histórico de vendas por item/reino — P30 é
  um modelo com `sold_per_day` da TSM (que conta desaparecimento de listagens) ou
  a mediana da faixa quando a TSM não registou vendas; (4) `num_auctions` é velho;
  (5) preços de cluster são pedidos, não transacções.
- **O que mudaria o vencedor:** dados de venda reais (Journalator) por 21–30 dias
  no Antonidas e no Echsenkessel.  É a única medida que hoje não existe e que
  separa os dois.

---

## G. Plano de execução — 7 dias (com validação a 21)

1. **Dia 0.** Criar personagem no Antonidas e chegar à capital (AH).  O ouro
   atravessa reinos pelo **warband bank** (é o mecanismo que a Blizzard indica
   para mover ouro entre reinos no Retail) — não é preciso transfer pago nem
   personagem de nível alto.  Não apagar nada em Dragonmaw: o stock actual fica
   lá até haver decisão.
2. **Dia 1 — liquidar o barato.** Listar 258921, 258934 e 258962 ao
   `min_buyout` −1 cobre (ou `market` se a prateleira estiver vazia), 48 h,
   esquecer.  No mesmo dia, pôr em Dragonmaw os itens da lista "parar de
   comprar" a vendor/preço de custo — não vale transportá-los.
3. **Dia 1–2 — teste de aquisição.** Comprar/farmar **20 itens** do topo do cabaz
   (10627, 870, 15529, 2823, 184802, 31221, 164293, 25261, 1318, 164521 e mais
   10 semelhantes) e listar no Antonidas.  Preço: `min_buyout` −1 cobre; onde não
   houver listagem, 1,0× o `market` do cluster (nunca acima de 2× a mediana entre
   clusters).
4. **Dia 2–7 — recolher.** Registar por item: listado, vendido, expirado,
   `min_buyout` de 48 em 48 h e número de concorrentes abaixo do teu preço.
   Ferramenta: Journalator (vendas reais) + `py -m woweconomy.analytics.collector_demand item <id>`
   para a demanda por peça.
5. **Como distinguir venda de listagem.** Só conta o evento de venda do
   Journalator (`journal`/`journal_stats`) ou o desaparecimento do item com
   ouro a entrar; listagem expirada não é venda.
6. **Critério de validação (21 dias).**
   - ≥ 1 venda entre os 20 itens de teste, e ≥ 0,5 venda/mês no total do
     catálogo → Antonidas validado, escalar para 100–150 listagens.
   - `recent/historical` do cabaz a cair abaixo de 0,95 ou ≥ 3 concorrentes
     abaixo do teu preço em metade dos itens → mudar para **Echsenkessel**
     (é o 1.º no cabaz de aquisição com tecto de preço conservador).
   - Se preferires não separar stock antigo de catálogo novo, **Draenor** é o
     único que é 4.º no inventário actual e 5.º na aquisição, com dados de preço
     completos; é a escolha de menor risco, não a de maior valor esperado.
7. **O que não fazer.** Não mudar de reino por causa do inventário actual: o
   cabaz de 57 peças rende 620–1 715 g/mês em qualquer um dos candidatos e o
   vencedor muda com o tecto de preço.  O ganho está no catálogo, não no stock.
   E não voltar a listar as peças "parar de comprar": já expiraram 70 vezes;
   cada repost é depósito pago para repetir o mesmo resultado.

---

## Como reproduzir

```bat
python scripts/best_realm.py                                  REM inventário
python scripts/best_realm.py --basket farm --top 12           REM aquisição (150)
python scripts/best_realm.py --basket rare --sensitivity      REM raras + robustez
python scripts/best_realm.py --combined --top 8                REM índice dos 3 cabazes
python scripts/best_realm.py --explain Antonidas --items-csv reports/itens_antonidas.csv
python -m pytest tests/test_best_realm.py -q
```

Notas de manutenção: o script abre as duas bases em `mode=ro`; o `--explain`
quer o nome do cluster (`Antonidas`), enquanto no `inventory_prob.py` o `--realm`
quer o slug **canónico** (`Spinebreaker`, não `Dragonmaw`); o segmento
(`--min-score`, omissão 900 000) tem de continuar 100 % bitmapped ou o script
recusa correr; `--shrinkage` e `--price-cap` são os dois botões que a
sensibilidade varre.  O `item_demand.py` antigo continua a filtrar `binding=1`
(BoP) — report conhecido e não usado aqui; este script usa `binding=2`.
