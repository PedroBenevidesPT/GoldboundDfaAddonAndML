# Token Rush — guard de preço mínimo e política de publicação

Alvo: **Dragonmaw** (cluster Spinebreaker), os mesmos 328 itens do inventário, os mesmos grupos, os mesmos coeficientes e as mesmas durações. **Só o preço mínimo (e o normal, quando a validação do TSM o obriga) muda.** Nada é importado; as duas propostas ficam intactas.

## 1. Semântica real da operação (lida no TSM instalado)

| momento | condição | resultado | fonte |
|---|---|---|---|
| validação | sem itens suficientes | não posta | `ValidateForPosting` |
| validação | expirações desde a última venda > `maxExpires` | não posta (`NOT_POSTING.MAX_EXPIRES`) e vai para o banco | `ValidateForPosting:363` |
| validação | `normalPrice < minPrice` | **operação inválida, não posta nunca** (`INVALID.NORMAL_BELOW_MIN`) | `AuctioningOperation:389` |
| validação | `maxPrice < minPrice` | operação inválida | `AuctioningOperation:392` |
| decisão | sem concorrência | posta a `normalPrice` (piso no `minPrice`) | `MakePostDecision:426,501` |
| decisão | `parede − undercut < minPrice` e `priceReset = none` | **não posta** (`NOT_POSTING.BELOW_MIN`) | `MakePostDecision:436-459` |
| decisão | `parede − undercut > maxPrice` | posta ao tecto (`aboveMax = maxPrice`) | `MakePostDecision:477` |
| decisão | caso normal | posta a `parede − undercut` | `MakePostDecision:492` |
| cancelamento | `cancelUndercut = false` e `cancelRepost = false` | **nunca cancela** (`WONT_CANCEL.DISABLED`) | `ValidateForCanceling:558` |
| repost | — | **não existe repost automático**: o anúncio expira ao fim da `duration` e o *próximo* post scan re-avalia o item do zero | `PostScan.lua:529` |

**Consequências, demonstradas e não inferidas:**

1. **Um `minPrice` mais baixo não provoca um repost.** Não há repost automático; o que muda é que o *próximo* scan pode postar um item que antes devolvia `NOT_POSTING.BELOW_MIN`. O número de ciclos é limitado por `maxExpires` (expirações desde a última venda).
2. **Um `minPrice` mais baixo nunca cancela nada** — com `cancelUndercut`/`cancelRepost` a false, o TSM não cancela listings vivos.
3. **Um `minPrice` mais alto pode parar o item de ser posted** (a parede fica abaixo do piso) e **pode tornar a operação inválida** se o `normalPrice` ficar abaixo do `minPrice` — nesse caso o item deixa de ser posted *de todo*, e não é um preço mais alto: é nenhum preço.
4. O `minPrice` **não** define o preço de venda: com concorrência o TSM posta `parede − undercut`. O piso é uma **porta** (postar ou não), não um preço.

## 2. As alternativas, item a item

| alternativa | o que muda | bloqueados | inválidos | min ≠ original |
|---|---|---:|---:|---:|
| A original | nada | 4 | 0 | 0 |
| B ponderada | a referência (min, normal e max) | 5 | 0 | 176 |
| C ponderada com piso do min original | min e normal sobem ao piso original | 6 | 0 | 105 |
| D ponderada com piso só na evidência fraca | min e normal sobem só nos itens fracos | 5 | 0 | 118 |

### 2.1 Como o mínimo se mexe

| alternativa | min sobe | min desce | min igual |
|---|---:|---:|---:|
| A original | 0 | 0 | 328 |
| B ponderada | 105 | 71 | 152 |
| C ponderada com piso do min original | 105 | 0 | 223 |
| D ponderada com piso só na evidência fraca | 105 | 13 | 210 |

> O mínimo da proposta original **não** é economicamente correcto por ser mais alto: ele é o resultado de um coeficiente de banda aplicado a uma referência que, nos itens em causa, é um ask isolado ou um outlier. Por isso o guard é avaliado por **efeito** (quantos itens ficam bloqueados, quantos param de ser posted, quanto capital fica preso), não por altura do número.

**A armadilha que o guard tem de evitar:** se o piso original fosse aplicado sem tocar no `normalPrice`, os itens em que o piso original ficaria **acima** do normal ponderado (13 de 328) teriam `normalPrice < minPrice` → `INVALID.NORMAL_BELOW_MIN` → a operação **deixava de postar esse item de todo**, nem a preço alto nem baixo. Por isso o guard eleva também o `normalPrice` para o piso: é a validação do TSM que obriga, não uma escolha de política de preço.

**A escala real do efeito é pequena.** O `minPrice` é 0,25-0,50 × a referência, ou seja, muito abaixo da parede da maioria dos itens listados: só 114 dos 328 itens têm parede, e entre eles só uma mão está bloqueada em qualquer das alternativas. A maior parte das diferenças de `minPrice` **não muda nenhuma decisão de postagem hoje** — muda o preço a que se postaria se o item estivesse sem concorrência, e o tecto (`maxPrice`) que decide se aceitamos o pedido de um Competidor acima do nosso referência.

## 3. Efeito no capital e nas vendas

| alternativa | itens sem postar | valor parado na bolsa (g) | valor listado (g) | depósito por ciclo, 5 % (g) |
|---|---:|---:|---:|---:|
| A original | 4 | 1,851 | 518,199 | 25,910 |
| B ponderada | 5 | 3,377 | 516,673 | 25,834 |
| C ponderada com piso do min original | 6 | 3,463 | 516,588 | 25,829 |
| D ponderada com piso só na evidência fraca | 5 | 3,377 | 516,673 | 25,834 |

*Valor parado na bolsa* = o valor dos itens que a operação deixa de postar (ficam no inventário, sem depósito queimado e sem venda). *Valor listado* = o valor dos itens que passam a estar no AH. *Depósito por ciclo* = 5 % do valor listado, devolvido se o item vender e perdido se expirar (a taxa de AH de 5 % incide sobre a venda). Com 12 dias e durações de 12-48 h um item pode queimar 3-6 depósitos: é esse o custo de insistir, e é o que o `maxExpires` limita.

## 4. Classificação de liquidação do Token Rush

| classe | itens | o que é |
|---|---:|---|
| **liquidar** | 106 | procura medida (DfA) ou venda provada pela conta, e preço não contraditório: priorizar a venda |
| **manter preço** | 117 | evidência insuficiente para descontar com segurança: manter o preço |
| **não repôr automaticamente** | 105 | procura fraca com divergência forte, expirações repetidas, ou o piso acima da parede: não repôr automaticamente |

> Os limiares (`5 %` da amostra DfA, `50 %` de sell-through, `2` expirações, `75 %` de divergência) são **heurísticas** e estão em `token_rush_guard.py`. Os factos que entram neles (profundidade, dispersão, frescura, divergência, histórico da conta) são **medidos**. Nenhum valor de mercado é tratado como receita.

**manter preço** — 117 itens

- Heavy Stone (2838) — observação local com 29 h
- Greased Cogwheel (244700) — observação local com 29 h
- Copper Bar (2840) — observação local com 29 h
- Crystal Vial (3371) — observação local com 29 h
- Tin Bar (3576) — observação local com 29 h
- Bolt of Linen Cloth (2996) — observação local com 29 h
- Bolt of Woolen Cloth (2997) — observação local com 29 h
- Rugged Hide (8171) — observação local com 29 h
- Light Illusion Dust (16204) — observação local com 29 h
- Lesser Moonstone (1705) — observação local com 29 h

**liquidar** — 106 itens

- Coal (3857) — a conta vendeu 1 de 1 publicações (100%) | observação local com 29 h
- Wool Cloth (2592) — a conta vendeu 352 de 352 publicações (100%) | observação local com 29 h
- Tin Ore (2771) — a conta vendeu 26 de 26 publicações (100%) | observação local com 29 h
- Linen Cloth (2589) — a conta vendeu 586 de 720 publicações (81%) | observação local com 29 h
- Light Leather (2318) — a conta vendeu 576 de 651 publicações (88%) | observação local com 29 h
- Rugged Leather (8170) — a conta vendeu 18 de 18 publicações (100%) | observação local com 29 h
- Malachite (774) — a conta vendeu 1 de 2 publicações (50%) | observação local com 29 h
- Deepsea Scale (52982) — a conta vendeu 66 de 66 publicações (100%) | observação local com 29 h
- Medium Leather (2319) — a conta vendeu 196 de 217 publicações (90%) | observação local com 29 h
- Kingsblood (3356) — a conta vendeu 6 de 8 publicações (75%) | observação local com 29 h

**não repôr automaticamente** — 105 itens

- Rough Grinding Stone (3470) — 16 expirações sem venda: repetir o ciclo não produz nada
- Ornate Bronze Lockbox (4632) — sem preço de referência: não há o que postar
- Elementium Lockbox (68729) — sem preço de referência: não há o que postar
- Quarter Staff (854) — 3 expirações sem venda: repetir o ciclo não produz nada
- War Knife (4571) — 4 expirações sem venda: repetir o ciclo não produz nada
- Arclight Spanner (6219) — a parede actual (16 g) está abaixo do piso (1,132 g): publicar abaixo do piso não é aceitável
- Wooden Maul (1820) — 10 expirações sem venda: repetir o ciclo não produz nada
- Gorestained Garb (120166) — 13 expirações sem venda: repetir o ciclo não produz nada
- Ritual Amice (14126) — 3 expirações sem venda: repetir o ciclo não produz nada
- Loch Modan Bearskin Rug (242948) — 3 expirações sem venda: repetir o ciclo não produz nada

## 5. Os 25 itens mais susceptibles de mudar o preço mínimo

Ordenados pela magnitude da redução do mínimo (o `minPrice` mais baixo é o que pode abrir a porta a vender abaixo do que a proposta original aceitava).

| item | banda | min original | min ponderado | variação | parede | divergência | evidência | decisão sob D |
|---|---|---:|---:|---:|---:|---:|---|---|
| Wrangler's Buckler of the  (15332) | 50k-150k | 65,825 g | 29,228 g | -36,597 g | 74,727 g | 4.072 | baixa / 2 anúncios | liquidar |
| Bristlebark Blouse (14570) | 50k-150k | 42,377 g | 31,044 g | -11,333 g | 44,960 g | 0.863 | baixa / 2 anúncios | liquidar |
| Overseer's Mandate (174790) | 50k-150k | 28,050 g | 20,534 g | -7,516 g | 56,100 g | 1.0 | baixa / 0 anúncios | não repôr automaticamente |
| Pattern: Hands of Darkness (7092) | 15k-50k | 8,549 g | 4,191 g | -4,358 g | 18,228 g | 3.876 | baixa / 0 anúncios | não repôr automaticamente |
| Pagan Vest of the Quickbla (14158) | 50k-150k | 40,016 g | 42,215 g | 2,199 g | 52,697 g | -0.101 | baixa / 0 anúncios | liquidar |
| Antipodean Rod (2879) | 5k-15k | 6,534 g | 4,685 g | -1,848 g | 19,070 g | 4.274 | média / 3 anúncios | não repôr automaticamente |
| Blacksmith Hammer (5956) | 2k-5k | 1,679 g | 197 g | -1,482 g | 2,187 g | 71.418 | baixa / 3 anúncios | liquidar |
| Prospector's Mitts (14564) | 2k-5k | 1,659 g | 2,699 g | 1,039 g | 3,625 g | -0.622 | média / 1 anúncios | não repôr automaticamente |
| Evocator's Blade (2567) | 15k-50k | 10,755 g | 11,749 g | 994 g | 14,951 g | -0.178 | baixa / 0 anúncios | liquidar |
| Grunt's Shield of the Auro (15512) | 2k-5k | 877 g | 1,828 g | 951 g | 1,562 g | -0.77 | média / 1 anúncios | não repôr automaticamente |
| Brackwater Vest (3306) | 2k-5k | 1,871 g | 936 g | -935 g | 3,033 g | 2.995 | baixa / 0 anúncios | não repôr automaticamente |
| Arclight Spanner (6219) | 2k-5k | 1,132 g | 203 g | -929 g | 16 g | 30.185 | baixa / 3 anúncios | não repôr automaticamente |
| Short Cutlass (1829) | 2k-5k | 1,613 g | 717 g | -896 g | 2,207 g | 4.061 | baixa / 0 anúncios | liquidar |
| Defender Bracers of the Qu (6574) | 2k-5k | 1,230 g | 352 g | -878 g | 1,770 g | 11.188 | baixa / 1 anúncios | liquidar |
| Ritual Amice (14126) | 2k-5k | 1,911 g | 2,778 g | 867 g | 859 g | -0.527 | baixa / 2 anúncios | não repôr automaticamente |
| Cross-Stitched Gloves (1783) | 2k-5k | 1,720 g | 900 g | -820 g | 3,071 g | 2.655 | média / 2 anúncios | não repôr automaticamente |
| Plans: Mirrored Boots (69969) | 500-2k | 617 g | 1,309 g | 691 g | 0 g | -0.778 | baixa / 1 anúncios | não repôr automaticamente |
| Bladefist Axe of the Fever (55287) | 2k-5k | 1,250 g | 694 g | -556 g | 1,741 g | 2.245 | baixa / 2 anúncios | manter preço |
| Aboriginal Rod of the Quic (15971) | 2k-5k | 1,759 g | 1,205 g | -554 g | 2,578 g | 1.13 | baixa / 1 anúncios | liquidar |
| Spiked Chain Cloak of the  (15519) | 2k-5k | 1,820 g | 2,325 g | 505 g | 3,074 g | -0.387 | alta / 2 anúncios | liquidar |
| Headhunter's Cloak of the  (15354) | 2k-5k | 1,336 g | 850 g | -487 g | 3,782 g | 1.475 | média / 4 anúncios | liquidar |
| Long Bastard Sword (1830) | 2k-5k | 1,248 g | 771 g | -477 g | 2,175 g | 1.619 | média / 2 anúncios | não repôr automaticamente |
| Hulking Gauntlets (14747) | 5k-15k | 4,202 g | 3,743 g | -459 g | 10,130 g | 0.783 | média / 1 anúncios | não repôr automaticamente |
| Canvas Gloves (1767) | 2k-5k | 1,088 g | 664 g | -424 g | 1,649 g | 1.686 | baixa / 2 anúncios | não repôr automaticamente |
| Bright Belt (4708) | 500-2k | 682 g | 290 g | -392 g | 1,234 g | 4.546 | baixa / 0 anúncios | manter preço |

## 6. Configuração de teste e reversão

1. Criar um perfil de teste: `TSM → Settings → General → Profiles` → `teste-rush` (ou `/tsm profile teste-rush`).
2. `Groups → Import` → colar `reports/pricing_groups/pricing_groups_weighted_proposal.tsm` (alternativa D) **num perfil limpo**.
3. `Operations → Auctioning` → confirmar `minPrice`, `normalPrice` e `maxPrice` por operação, e `priceReset = none`, `aboveMax = maxPrice`, `cancelUndercut = false`, `cancelRepost = false`.
4. *Post Scan* num item de cada banda e comparar com o que a proposta original pediria no mesmo dia (a coluna `wall` do CSV tem a parede actual de cada item).
5. `maxExpires` conta as expirações **desde a última venda**: se um item já expirou `maxExpires` vezes, deixa de ser posted e vai para o banco.

**Reversão:** nada foi alterado no perfil vivo, logo não há nada a reverter. Se a candidata for importada num perfil de teste, apagar esse perfil (ou os grupos importados) e voltar a importar `pricing_groups_proposal.tsm`. Importar as duas no mesmo perfil **não** substitui: o TSM cria `PG ... 1`, `PG ... 2`.

## 7. Recomendação

**Adopt a alternativa D (guard estreito) num perfil de teste; não no perfil vivo.**

Porquê:

1. O `minPrice` é uma **porta**, não um preço: a auditoria do addon mostra que uma redução não cancela nem repõe nada — só deixa o *próximo* scan postar o que antes recusava. O guard tem, por isso, de ser pequeno e reversível.
2. **D** repõe o comportamento de bloqueio da original (5 itens bloqueados, contra 4 da original) para os itens cuja mudança de referência assenta em evidência fraca, e mantém a referência ponderada onde a evidência a sustenta.
3. **D** evita a armadilha da validação: nos 13 itens em que o piso original ficaria acima do normal ponderado, o `normalPrice` sobe com o piso e a operação continua válida. Sem isso o item deixava de ser posted de todo.
4. **B** é a mais agressiva e a que mais destrava capital (é a que tem o `minPrice` mais baixo em 71 itens). Se a campanha precisa de vender  depressa, é a escolha coerente — mas é também onde o preço realizado pode piorar, e sem vendas datadas **não dá para justificar**. Fica para o perfil de teste.
5. **C** é a mais conservadora: aplica o piso original a todos os itens e perde o benefício da referência ponderada nos itens com evidência boa. É a escolha se a prioridade for não arriscar uma redução de preço mínima.

### 7.1 A política de listagem prática

| classe | o que se faz | como se decide |
|---|---|---|
| **liquidar** (106 itens) | postar já, undercut mínimo, e repor se expirar | procura medida na amostra DfA ou venda provada pela conta |
| **manter preço** (117 itens) | postar ao preço de mercado, sem desconto, e não insistir | a evidência não sustenta descontar |
| **não repôr automaticamente** (105 itens) | não voltar a postar: vendor, destruição ou venda manual | expirações repetidas, procura fraca com divergência forte, ou o piso acima da parede |

Os itens de **material** (mercado de commodities, um só mercado em toda a região) e os **sem qualquer referência** não entram na classificação de liquidação: os primeiros seguem o mercado de materials, os segundos não têm preço nenhum para postar.

## 8. O que a base de dados não decide

Não há vendas datadas, nem parede histórica, nem custo de aquisição: **nenhuma das quatro alternativas pode ser declarada vencedora em ouro realizado**. O que a comparação mostra é o efeito mecânico (quantos itens ficam bloqueados, quantos param de ser posted, quanto capital fica preso) — e isso é tudo o que se pode afirmar com honestidade.
