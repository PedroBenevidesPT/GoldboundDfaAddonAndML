# Preço de referência ponderado pela evidência — proposta

Gerado por `python scripts/pricing_groups.py --write`. Alvo: **Spinebreaker**. É uma **proposta**: o ficheiro `pricing_groups_weighted_proposal.tsm` **não foi importado** e nada foi alterado no TSM. A proposta original (`pricing_groups_proposal.tsm`) fica intacta.

## 1. O que muda

| | original | candidato |
|---|---|---|
| grupos | 31 | 31 (os mesmos) |
| bandas | 9 | 9 (as mesmas) |
| coeficientes de banda | status quo | status quo (inalterados) |
| **referência** | `first(dbmarket, dbregionmarketavg, dbregionhistorical)` | mistura ponderada pela evidência local (método: **geometric**) |

O que muda é **uma coisa**: a referência. Os grupos, as bandas, os coeficientes, a duração, o `maxExpires` e o `priceReset` são os mesmos. Nenhum factor de reino é aplicado duas vezes: a âncora continua a ser local (`dbmarket`) e o factor do reino vive no export por reino e na camada de campanha.

## 2. A expressão no TSM

```
referência = ifgt(local_valido,
              ifgt(regional_valido,
                 <mistura ponderada>,
                 local),
              ifgt(regional_valido, regional, historico_regional))
```

Com `w` = peso local e `1-w` = peso regional, a mistura **geometric** é:

```
geométrica:  dbmarket ^ w * dbregionmarketavg ^ (1 - w)
aritmética:  w * dbmarket + (1 - w) * dbregionmarketavg
```

O `ifgt` treats a sentinel da TSM (`99999999900`) como ausente, e a escada de fallbacks é explícita: sem local → regional; sem regional → local; sem nenhum → histórico regional (confiança baixa).

## 3. De onde vêm os pesos

O peso local é o produto de três medidas, e só de três:

| factor | medida | limiares |
|---|---|---|
| profundidade | nº de anúncios do item no reino | 0→0.55, 1→0.65, 2→0.80, 3→0.90, 5→1.00 |
| dispersão | \|ask médio ÷ parede − 1\| | ≤ 0.50 → 1,00; decaimento até 0.50 → 0,25 |
| frescura | idade da observação local vs regional | o reino é descarregado de 6 em 6 h e a região de 1 em 1 dia: o local é mais fresco |

O peso é limitado a **[0.50, 1.00]**: a referência regional **nunca** domina. Isto não é arbitrário — o walk-forward (`scripts/reference_backtest.py`) mostra que a referência regional erra o nível local por uma ordem de grandeza (erro mediano >100 %) contra <1 % da local.

Os limiares saem da distribuição medida: a mediana do nº de anúncios é 1 em quase todas as bandas, e só 13-38 % dos itens chegam a três; a dispersãoask/parede tem p50 1,06-1,62 e p90 até 5,6.

**A divergência local/regional não decide o preço.** Ela produz um diagnóstico e baixa a confiança; o preço só muda por causa da profundidade, da frescura e da dispersão.

## 4. Os pesos medidos

| peso local | células |
|---:|---:|
| 0.5 | 13 |
| 0.6 | 6 |
| 0.7 | 3 |
| 0.8 | 2 |
| 0.9 | 1 |

### 4.1 Peso por grupo e banda

| grupo | banda | peso local | n |
|---|---|---:|---:|
| demand_falling | 100-500 | 0.50 | 40 |
| demand_falling | 2k-5k | 0.50 | 11 |
| demand_falling | 500-2k | 0.55 | 16 |
| demand_falling | 50k-150k | 0.50 | 1 |
| demand_falling | <100 | 0.60 | 5 |
| demand_stable | 100-500 | 0.55 | 40 |
| demand_stable | 15k-50k | 0.55 | 3 |
| demand_stable | 2k-5k | 0.50 | 11 |
| demand_stable | 500-2k | 0.53 | 12 |
| demand_stable | 50k-150k | 0.50 | 2 |
| demand_stable | 5k-15k | 0.80 | 5 |
| demand_stable | <100 | 0.50 | 7 |
| no_demand_falling | 100-500 | 0.65 | 1 |
| no_demand_falling | 2k-5k | 0.50 | 1 |
| no_demand_falling | 500-2k | 0.65 | 1 |
| no_demand_stable | 100-500 | 0.50 | 1 |
| no_demand_stable | 500-2k | 0.50 | 1 |
| unknown_falling | 100-500 | 0.80 | 1 |
| unknown_falling | 2k-5k | 0.50 | 1 |
| unknown_falling | <100 | 0.85 | 6 |
| unknown_stable | 100-500 | 0.50 | 1 |
| unknown_stable | 15k-50k | 0.55 | 1 |
| unknown_stable | 2k-5k | 0.65 | 3 |
| unknown_stable | 500-2k | 0.50 | 4 |
| unknown_stable | 50k-150k | 0.55 | 1 |

## 5. Preços que mudam materialmente

111 itens do inventário mudam **10 % ou mais** face à referência original (`local` quando existe, `regional` quando não).

| item | grupo | banda | local | regional | nova referência | variação | confiança | motivo |
|---|---|---|---:|---:|---:|---:|---|---|
| Reinforced Chain Shoulderpad (1760) | demand_falling | <100 | 49 g | 816 g | 173 g | +255% | baixa | preço local assenta em 0 anúncio(s); divergência local/regional de -94%: confiança reduzida, o preço não muda por isto |
| Plans: Mirrored Boots (69969) | unknown_stable | 500-2k | 1,764 g | 7,927 g | 3,739 g | +112% | baixa | peso limitado a 0.50 (bruto 0.49); preço local assenta em 1 anúncio(s); divergência local/regional de -78%: confiança reduzida, o preço não muda por isto |
| Scorching Wand (5213) | demand_stable | <100 | 75 g | 296 g | 149 g | +98% | baixa | peso limitado a 0.50 (bruto 0.41); preço local assenta em 0 anúncio(s) |
| Blacksmith Hammer (5956) | demand_falling | 2k-5k | 4,197 g | 58 g | 493 g | -88% | baixa | peso limitado a 0.50 (bruto 0.23); divergência local/regional de +7142%: confiança reduzida, o preço não muda por isto |
| Arclight Spanner (6219) | unknown_falling | 2k-5k | 2,830 g | 91 g | 507 g | -82% | baixa | peso limitado a 0.50 (bruto 0.23); divergência local/regional de +3019%: confiança reduzida, o preço não muda por isto |
| Gorestained Garb (120166) | demand_falling | 100-500 | 106 g | 330 g | 187 g | +76% | baixa | peso limitado a 0.50 (bruto 0.23) |
| Dilapidated Cloth Gloves (25344) | demand_falling | <100 | 24 g | 100 g | 43 g | +76% | média | divergência local/regional de -76%: confiança reduzida, o preço não muda por isto |
| Green Woolen Vest (2582) | demand_stable | <100 | 100 g | 299 g | 173 g | +73% | baixa | peso limitado a 0.50 (bruto 0.49); preço local assenta em 1 anúncio(s) |
| Linked Chain Pants (1751) | demand_stable | <100 | 42 g | 221 g | 72 g | +72% | média | divergência local/regional de -81%: confiança reduzida, o preço não muda por isto |
| Pattern: Heavy Woolen Cloak (4346) | unknown_stable | 100-500 | 125 g | 367 g | 214 g | +72% | baixa | peso limitado a 0.50 (bruto 0.20) |
| Defender Bracers of the Quic (6574) | demand_stable | 2k-5k | 3,075 g | 252 g | 881 g | -71% | baixa | peso limitado a 0.50 (bruto 0.16); preço local assenta em 1 anúncio(s); divergência local/regional de +1119%: confiança reduzida, o preço não muda por isto |
| Grunt's Shield of the Aurora (15512) | demand_falling | 2k-5k | 2,192 g | 9,527 g | 3,666 g | +67% | média | preço local assenta em 1 anúncio(s); divergência local/regional de -77%: confiança reduzida, o preço não muda por isto |
| Woolen Boots (2583) | demand_stable | 100-500 | 286 g | 715 g | 452 g | +58% | baixa | peso limitado a 0.50 (bruto 0.49); preço local assenta em 1 anúncio(s) |
| Bright Belt (4708) | no_demand_stable | 500-2k | 1,948 g | 351 g | 827 g | -58% | baixa | peso limitado a 0.50 (bruto 0.14); preço local assenta em 0 anúncio(s); divergência local/regional de +455%: confiança reduzida, o preço não muda por isto |
| Wrangler's Buckler of the Qu (15332) | demand_stable | 50k-150k | 131,650 g | 25,957 g | 58,457 g | -56% | baixa | peso limitado a 0.50 (bruto 0.20); divergência local/regional de +407%: confiança reduzida, o preço não muda por isto |
| Short Cutlass (1829) | demand_falling | 2k-5k | 4,033 g | 797 g | 1,792 g | -56% | baixa | peso limitado a 0.50 (bruto 0.14); preço local assenta em 0 anúncio(s); divergência local/regional de +406%: confiança reduzida, o preço não muda por isto |
| Linked Chain Gloves (1750) | demand_falling | 100-500 | 127 g | 306 g | 197 g | +55% | baixa | peso limitado a 0.50 (bruto 0.49); preço local assenta em 1 anúncio(s) |
| Defender Tunic of the Quickb (6580) | demand_stable | 500-2k | 937 g | 197 g | 430 g | -54% | baixa | peso limitado a 0.50 (bruto 0.16); preço local assenta em 1 anúncio(s); divergência local/regional de +374%: confiança reduzida, o preço não muda por isto |
| Crochet Gloves (3940) | demand_falling | 500-2k | 692 g | 147 g | 319 g | -54% | baixa | peso limitado a 0.50 (bruto 0.49); preço local assenta em 1 anúncio(s); divergência local/regional de +369%: confiança reduzida, o preço não muda por isto |
| Simple Linen Boots (10046) | demand_stable | 100-500 | 268 g | 619 g | 407 g | +52% | baixa | peso limitado a 0.50 (bruto 0.49); preço local assenta em 1 anúncio(s) |
| Gloom Reaper of the Fireflas (863) | demand_stable | <100 | 94 g | 308 g | 142 g | +52% | média | preço local assenta em 1 anúncio(s) |
| Pattern: Hands of Darkness (7092) | unknown_stable | 15k-50k | 17,098 g | 3,507 g | 8,381 g | -51% | baixa | preço local assenta em 0 anúncio(s); divergência local/regional de +388%: confiança reduzida, o preço não muda por isto |
| Schematic: Portable Bronze M (4414) | unknown_stable | 500-2k | 1,239 g | 2,821 g | 1,870 g | +51% | baixa | peso limitado a 0.50 (bruto 0.41); preço local assenta em 0 anúncio(s) |
| Brown Linen Pants (4343) | demand_falling | 100-500 | 109 g | 246 g | 163 g | +50% | baixa | peso limitado a 0.50 (bruto 0.49); preço local assenta em 1 anúncio(s) |
| Brackwater Vest (3306) | demand_falling | 2k-5k | 4,679 g | 1,171 g | 2,341 g | -50% | baixa | peso limitado a 0.50 (bruto 0.14); preço local assenta em 0 anúncio(s); divergência local/regional de +300%: confiança reduzida, o preço não muda por isto |
| Tarnished Dawnlit Sentinel's (258934) | demand_falling | 100-500 | 131 g | 293 g | 196 g | +49% | baixa | peso limitado a 0.50 (bruto 0.16); preço local assenta em 1 anúncio(s) |
| Ivycloth Boots of the Aurora (9792) | demand_stable | 100-500 | 101 g | 223 g | 150 g | +49% | baixa | peso limitado a 0.50 (bruto 0.16); preço local assenta em 1 anúncio(s) |
| Brown Linen Vest (2568) | demand_stable | 100-500 | 118 g | 317 g | 175 g | +48% | média | — |
| Plans: Green Iron Gauntlets (3612) | unknown_stable | 500-2k | 1,623 g | 3,574 g | 2,409 g | +48% | baixa | peso limitado a 0.50 (bruto 0.16); preço local assenta em 1 anúncio(s) |
| Woolen Cape (2584) | demand_stable | 100-500 | 134 g | 293 g | 198 g | +48% | baixa | peso limitado a 0.50 (bruto 0.49); preço local assenta em 1 anúncio(s) |
| Cuirboulli Boots (2143) | demand_stable | 100-500 | 114 g | 250 g | 169 g | +48% | baixa | peso limitado a 0.50 (bruto 0.14); preço local assenta em 0 anúncio(s) |
| Battle Knife (15241) | demand_stable | <100 | 100 g | 211 g | 145 g | +45% | baixa | peso limitado a 0.50 (bruto 0.41); preço local assenta em 0 anúncio(s) |
| Ritual Amice (14126) | demand_falling | 2k-5k | 4,777 g | 10,097 g | 6,945 g | +45% | baixa | peso limitado a 0.50 (bruto 0.20) |
| Cadet Leggings of the Aurora (9763) | demand_falling | 100-500 | 287 g | 75 g | 157 g | -45% | baixa | preço local assenta em 0 anúncio(s); divergência local/regional de +283%: confiança reduzida, o preço não muda por isto |
| Tough Leather Armor (1810) | no_demand_stable | 100-500 | 370 g | 778 g | 536 g | +45% | baixa | peso limitado a 0.50 (bruto 0.49); preço local assenta em 1 anúncio(s) |
| Bladefist Axe of the Feverfl (55287) | no_demand_falling | 2k-5k | 3,124 g | 963 g | 1,734 g | -44% | baixa | peso limitado a 0.50 (bruto 0.20); divergência local/regional de +225%: confiança reduzida, o preço não muda por isto |
| Green Linen Bracers (4308) | demand_stable | <100 | 97 g | 197 g | 139 g | +42% | baixa | peso limitado a 0.50 (bruto 0.49); preço local assenta em 1 anúncio(s) |
| Prospector's Mitts (14564) | demand_falling | 2k-5k | 4,149 g | 10,971 g | 5,831 g | +41% | média | preço local assenta em 1 anúncio(s) |
| Canvas Gloves (1767) | demand_falling | 2k-5k | 2,720 g | 1,013 g | 1,660 g | -39% | baixa | peso limitado a 0.50 (bruto 0.20); divergência local/regional de +169%: confiança reduzida, o preço não muda por isto |
| Sooty Mail Armor (62132) | demand_stable | 100-500 | 232 g | 422 g | 313 g | +35% | baixa | peso limitado a 0.50 (bruto 0.49); preço local assenta em 1 anúncio(s) |

## 6. Verificação do payload

A expressão de cada operação foi avaliada com os valores medianos da célula e bate com o modelo (a diferença é 0). A verificação está em `pricing_groups.verify_weighted_expression`.


## 7. Limites

- **Não há backtest de ouro realizado.** O journal do Journalator não guarda datas, logo não há vendas datadas nem parede histórica na data da venda. O que existe é um walk-forward do **nível** e uma calibração com 25 vendas — ver `reference_backtest.md`.
- **O nível não é o problema.** Os asks médios estão várias vezes acima do preço realizado; isso é trabalho do coeficiente de banda, não da referência.
- **O peso por item vive fora do TSM.** O TSM avalia a mesma expressão para todo o grupo, por isso a expressão leva o peso **mediano** do grupo e da banda. A referência por item (com o peso por item) fica no cálculo do DFA e neste relatório.
- **A expressão não tem fonte customizada** — só `dbmarket`, `dbregionmarketavg` e `dbregionhistorical`, que existem no TSM instalado.
- **Custos de casa de auction assumidos** (taxa 5 %, depósito 5 % limitado a 300 % do valor de venda ao fornecedor) e não verificados.

## 8. O que não foi feito

- Nada foi importado no TSM; a proposta original ficou intacta.
- Nenhuma operação, grupo ou fonte do TSM foi alterada.
- Os coeficientes de banda, os grupos e as durações não foram tocados.
- O factor do reino não foi aplicado dentro da referência.
