# Inventário e AH — análise refeita (19/10/2026)

Export novo: [inventory.csv](../inventory.csv) (19/10 15:26) — **328 entradas, 5 291 unidades**.
Snapshot TSM: 19/10 14:28, 92 clusters. Journalator importado às 14:51 de hoje.

> Esta análise substitui a de 18/10. O export anterior tinha 205 linhas e não incluía
> os dois itens que decidem tudo (ver §4).

## 1. O que mudou com o import completo do Journalator

| | antes (import de 16/10) | agora (import de 19/10) |
|---|---:|---:|
| linhas item×reino | 225 | **931** |
| vendas realizadas | 204 | **6 344** (6 322 com itemId) |
| expirações | 259 | **5 390** (4 784 com itemId) |
| ouro realizado | 261 g | **24 559 g** |

A diferença de 22 vendas e 606 expirações são entradas do addon cujo nome não
resolve para um `itemId` no catálogo `items`; o import salta-as ([journal.py:175](../scripts/journal.py#L175)),
por isso não entram em nenhum cálculo por item.

O import anterior era um fragmento. O histórico real mostra 6344 vendas e 24,5 k g —
quase tudo **material**, a preços de 1–2 g por unidade. O maior negócio isolado de
sempre é uma Burning War Axe por 2 713 g.

Restringindo aos itens que ainda estão na bolsa (225 linhas): **2 576 vendas, 3 884 g,
812 expirações**. Dessas expirações, as maiores são Linen Cloth (134), Light Leather (75),
Ruined Leather Scraps (29), Large Venom Sac (28) e Bruiseweed (27) — **material em
Dragonmaw**, não transmog.

Vendas de BoE entre os itens da bolsa: **3**, todas baratas, todas em Auchindoun —
Canvas Pants 427 g, Canvas Shoes 231 g, Cedar Walking Stick 227 g. Nenhuma peça de
transmog caro saiu.

## 2. O valor do livro, por quatro medidas (cluster Spinebreaker)

| medida | total | o que é |
|---|---:|---|
| min buyout | 520 051 g | o anúncio mais barato de cada item |
| DBMarket | 688 627 g | média ponderada dos anúncios — o que o TSM mostra |
| recente | 521 842 g | últimos 3 dias |
| histórico 60 d | 904 446 g | o que se pediu nos últimos 60 dias |

**1,5 M não reproduz de nenhuma das quatro.** Neste export nenhuma soma passa de 905 k.
O 1,5 M vem ou de um export antigo, ou de somar os teus próprios pedidos inflacionados
(o top 5 dos pedidos sozinho vale 308 k).

## 3. A distribuição é o problema

Modelo `inventory_prob.py` sobre este export: 140 itens com preço e procura, 136 itens
de material (5 076 un), 52 sem preço (receitas, sacos, caixas).

- esperado a 30 dias, melhor reino de cada item: **40 501 g**
- Slippers of the Hissing Cult, sozinho: **33 660 g** (83 % do total)
- 63 itens com P30 < 2 %: **3 332 g somados**

Ou seja: fora dos dois Slippers, o resto do inventário vale ~7 k g em 30 dias, mesmo
escolhendo o melhor reino item a item. Era este o número que faltava — e ele confirma
que o stock antigo é peso morto.

## 4. O único activo real: 2× Slippers of the Hissing Cult

Item 271435, BoE de raid (Venomous Abyss, Midnight), qualidade épica, tier actual.

| | Spinebreaker (casa) |
|---|---:|
| anúncio mais barato | 39 624 g |
| DBMarket | 25 976 g |
| recente | 39 849 g |
| histórico 60 d | 30 001 g |
| compradores locais (segmento ≥900 k) | 118 |
| P30 | 84,9 % |
| esperado 30 d | 33 660 g |

Região: mercado 26 818 g, preço médio de venda 45 001 g, taxa de venda 13,2 %,
**3,17 vendas/dia**. É o único item do inventário com velocidade de venda a sério.

O melhor reino para ele é **o teu**: Spinebreaker 33 660 g, Auchindoun 14 509 g,
Draenor 12 173 g. Ficar em Dragonmaw não custa nada *para este item* — pelo contrário.

Preço sugerido: **29 999–31 999 g**, um de cada vez. O anúncio a 39 624 g está 52 %
acima do mercado (25 976 g) e acima do histórico (30 001 g); a 30 k ficas o mais barato
do livro e ainda acima da média de venda.

## 5. O resto, em casa

| item | compradores | P30 | pedido | esperado |
|---|---:|---:|---:|---:|
| Tarnished Dawnlit Corsair's Boots | 8 | 99,7 % | 293 g | 292 g |
| Tarnished Dawnlit Warglaive | 18 | 90,4 % | 86 g | 77 g |
| Tarnished Dawnlit Sentinel's Girdle | 8 | 98,2 % | 50 g | 49 g |
| Pagan Vest | 111 | 0,4 % | 52 697 g | 222 g |
| Wrangler's Buckler | 88 | 0,4 % | 35 037 g | 155 g |
| Bristlebark Blouse | 89 | 0,3 % | 44 960 g | 151 g |
| Antipodean Rod | 95 | 1,8 % | 8 170 g | 144 g |
| Mystic's Robe | 104 | 1,2 % | 9 966 g | 122 g |

As três Tarnished Dawnlit vendem-se em casa por ~418 g. As outras cinco têm centenas
de compradores *no segmento* e mesmo assim 0,3–1,8 % — o preço pedido é 30–100× o que
o mercado paga por elas.

## 6. Materiais: o único mercado que funciona e não depende de reino

136 itens, 5 076 unidades, **64 969 g** a preço regional. É mercado único na EU: o
reino não importa. O que importa é o ciclo de reposição — 134 expirações de Linen
Cloth e 75 de Light Leather dizem que o problema ali é preço e volume, não sítio.

## 7. Escolha de reino

Não muda a decisão: **ficar**. O ranking de cabaz (`best_realm.py`, 92 clusters) dá
1,5–2,9 k g/30 d no topo, e o `inventory_prob` atribui 35 306 g de vantagem ao melhor
reino — mas essa ordenação correlaciona 0,79 com o número de contas lidas por reino,
logo mede cobertura da amostra, não população. Para o item que realmente vende, casa
ganha por 2,3×.

## 8. Bug de dados encontrado (afecta todas as análises anteriores)

`db2_items.binding`: **1 = ON_ACQUIRE (BoP), 2 = ON_EQUIP (BoE)**, confirmado em
[tests/test_db2_items.py](../tests/test_db2_items.py#L70).

Os Slippers estão gravados como `binding=1, era=2`, apesar de serem um BoE da
expansão actual. Consequências:

- [scripts/item_demand.py:129](../scripts/item_demand.py#L129) filtra `AND i.binding=1`
  — selecciona BoP, não BoE. Toda a lista de "aparências vendáveis" estava errada.
- [scripts/hungry_buyers.py:96](../scripts/hungry_buyers.py#L96) usa o mesmo filtro.
- `best_realm.build_baskets` exige `binding==2` **e** `era=0`, pelo que os BoE da
  expansão actual (era=2) ficam de fora do cabaz — 160 itens BoE era=2 não entram.

É por isto que a análise anterior concluiu "0 vendas de BoE": o filtro apontava para
itens que não podem ser vendidos, e escondia exactamente os que podem.

## 9. Plano de 7 dias

1. **Vender os 2 Slippers** a 29 999–31 999 g, um de cada vez. Vale ~60 k g — mais do
   que todo o resto do inventário em 30 dias.
2. **Tarnished Dawnlit** (3 peças) a 293/86/50 g. São 418 g com 90–99 % de probabilidade
   e zero risco de transfer.
3. **Parar de repostar** os 63 itens com P30 < 2 %. Cada repost é depósito pago para
   repetir um resultado que já falhou 812 vezes.
4. **Materiais**: vender em stacks; parar o ciclo de undercut em Linen Cloth e
   Light Leather, que só produz expirações.
5. **Não mudar de reino.** Dragonmaw/Spinebreaker é o melhor sítio para o único item
   que vende.

## Como reproduzir

```
python scripts/journal.py import
python scripts/inventory_prob.py --inventory inventory.csv --csv reports/inventory_prob_novo.csv
python scripts/inventory_prob.py --inventory inventory.csv --realm Spinebreaker --per-item --top 12
python scripts/best_realm.py
```

## Limitações

- `P30` é modelo (contas em falta no segmento × velocidade regional), não sell-through
  medido deste item no teu reino.
- `sold_per_day` regional da TSM é uma estimativa; 3,17/dia num BoE de 26 k g é alto e
  não foi confirmado por histórico de vendas próprio.
- O segmento é o topo ≥ 900 k (1 904 contas), não a população do reino.
- Não houve teste da suite nesta volta: só markdown e dados foram tocados, nenhum `.py`.
