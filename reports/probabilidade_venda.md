# Probabilidade de venda: o que os dados dizem, sem a janela das 48 h

Escrito a 19-10-2026. Base: `tsm_markets.db` (92 reinos, 1 180 279 linhas de preço,
snapshot de 19-10 às 05:36 UTC, descarregado às 07:02 UTC),
`C:\economics\woweconomy\dfa\dfa.db` (3 130 contas com bitmap de aparências e score
≥ 600 mil) e `reports/item_demand_realms.csv`. Reproduz com:

    python scripts/sale_probability.py --bands --top 25
    python scripts/sale_probability.py --items Silvermoon --items-top 15

---

## 1. A pergunta mudou, e com ela o modelo

O modelo anterior perguntava "quanto ouro isto rende em 48 horas" e para isso precisava
de uma taxa de venda por reino, que a TSM não publica — só publica por região. Todas as
conversões eram, por isso, um palpite com uma janela arbitrária colada por cima.

Este modelo parte do outro lado do balcão. Para cada item e cada reino sabe-se **quantos
colecionadores locais ainda lhe faltam**, porque o `dfa.db` guarda um bitmap por conta
scanneada. A probabilidade de que uma listagem venda em 30 dias é

    P = 1 − exp( −30 · λ_região · (compradores_reino / compradores_EU) / (N + 1) )

onde `N` é 1 se o reino já tem aquela peça à venda e 0 se a prateleira está vazia.
`λ_região` é a taxa de venda diária da TSM para o item em toda a Europa. A razão
`compradores_reino / compradores_EU` é uma proporção entre duas contagens da **mesma
amostra**, por isso é uma estimativa válida mesmo que a amostra seja uma fração da
população — o que não seria válido era usar os números absolutos como se fossem a
população inteira.

## 2. A probabilidade não é do reino, é do item

Em Silvermoon, o melhor dos 92 reinos, a listagem mediana tem **0,7 % de probabilidade
de vender num mês**. Apenas 2 % dos itens do catálogo passam de 20 %. Ou seja: escolher
bem o reino não transforma um catálogo aleatório num negócio; no máximo move a agulha
de 0,3 % para 0,7 %.

Dentro do mesmo reino, a diferença entre itens é de duas ordens de grandeza:

| item | compradores EU | compradores Silvermoon | vendas/dia na região | P(30 dias) | preço |
|---|---:|---:|---:|---:|---:|
| Tarnished Crown | 837 | 54 | 1,938 | **97,7 %** | 4 243 g |
| Bracers of Valor | 1 950 | 125 | 0,032 | 6,0 % | 1,34 M g |
| Dustbringer | 3 013 | 184 | 0,042 | 7,4 % | 5,5 M g |

O `Tarnished Crown` não é caro — 4 243 g — mas vende quase de certeza, porque é barato o
suficiente para 837 pessoas o quererem e a região já o mover a duas unidades por dia. O
`Dustbringer` é querido por praticamente toda a amostra (3 013 de 3 130 contas) e mesmo
assim vende 7,4 %/mês, porque a 5,5 milhões de ouro quase ninguém o compra. É o mesmo
reino, a mesma prateleira, resultados opostos.

## 3. A concorrência vale 2×; o comprador vale 10 a 20×

Postar num reino onde a peça não está à venda duplica a probabilidade (o divisor passa
de 2 para 1). É o único efeito que a escolha de reino controla diretamente — e é pequeno
quando comparado com a diferença de compradores entre reinos.

O caso que mostra isto com clareza: Aggramar tem **59 % da prateleira vazia** e um
rendimento de 69 g por listagem e por mês; Silvermoon tem **2 % vazia** e rende 342 g.
A prateleira vazia do Aggramar não compensa o facto de a amostra DfA lhe conhecer 39
colecionadores (11 com bitmap) contra 421 em Silvermoon (193 com bitmap). Khaz Modan (43 % vazia, 67 g), Wildhammer (64 % vazia, 39 g)
e Eredar (30 % vazia, 167 g) contam a mesma história em escalas diferentes.

Conclusão prática desta secção: não vale a pena trocar de reino por causa da concorrência.
Vale a pena trocar de item, e vale a pena garantir que o reino escolhido tem compradores.

## 4. O que a amostra esconde: a banda dos 700–800 mil

Aqui está a descoberta que muda a leitura de tudo o que foi escrito antes.

Os 13 453 colecionadores do `dfa.db` distribuem-se assim: 4 213 abaixo de 600 mil,
178 entre 600 e 700 mil, **4 746 entre 700 e 800 mil**, 2 412 entre 800 e 900 mil e
1 904 acima de 900 mil. A banda do meio é a segunda maior de todas.

E cada um desses colecionadores falta muito mais do que um colecionador do topo:

| banda | contas na amostra / no total | faltam por conta | massa de procura |
|---|---:|---:|---:|
| 600–700k | 178 / 178 (100 %) | 24 812 | 4,4 M |
| **700–800k** | **79 / 4 746 (1,7 %)** | **23 733** | **≈110 M** |
| 800–900k | 969 / 2 412 (40 %) | 19 985 | ≈47,9 M |
| 900k+ | 1 904 / 1 904 (100 %) | 15 798 | 29,1 M |

Um colecionador de 900 mil para cima possui 64 % do catálogo de aparências e ainda lhe
faltam 15 798 peças. Um colecionador de 700–800 mil falta 23 733 — **50 % mais
prateleira por comprador** — e há 2,5 vezes mais gente nessa banda. Extrapolando os 79
medidos para os 4 746, a massa de procura é de **cerca de 110 milhões de faltas**, contra
29 milhões da banda que está 100 % medida. Ou seja: **3,8 vezes mais procura no lado que
o modelo trata como quase zero.**

Isto não é um erro de leitura de dados; é um buraco de amostragem. Das 4 746 contas da
banda, 4 720 foram visitadas (`probed_at` preenchido) mas só 79 têm bitmap guardado. O
custo de tapar o buraco não é descobrir contas — é voltar a ler coleções que já foram
visitadas e não foram retidas.

Enquanto isso não acontecer, qualquer ranking de reinos está a descrever os 14 % mais
ricos do mercado e a chamar-lhe "a procura". É por isso que reinos como Aggramar,
Khaz Modan e Wildhammer aparecem fracos: têm a base certa (maioria em 700–800 mil) e a
prateleira mais vazia, mas o modelo atribui-lhes compradores do topo, que já têm quase
tudo.

## 4b. Onde, concretamente

`python scripts/sale_probability.py --where 20` lista os pares (reino, item) em que a peça
**não está à venda no reino** mas há compradores locais. São 138 166 pares, em 78 dos 90
reinos — "prateleira vazia" não é a exceção, é a regra. O que separa as oportunidades é
quantos compradores ficam desse lado do balcão.

Ordenado por probabilidade, com preço verificável (≥ 1 000 g, com mediana entre reinos):

| reino | item | compradores | P(30d) | preço | ouro esperado |
|---|---|---:|---:|---:|---:|
| Ravencrest | Primalist Warden's Bracers | 65 | 29,9 % | 118 256 g | 35 343 g |
| Howling Fjord | Primalist Warden's Bracers | 49 | 23,5 % | 118 256 g | 27 771 g |
| Gordunni | Primalist Warden's Bracers | 42 | 20,5 % | 118 256 g | 24 244 g |
| Drak'thul | Primalist Warden's Bracers | 34 | 16,9 % | 34 139 g | 5 786 g |
| Auchindoun | Primalist Warden's Bracers | 33 | 16,5 % | 118 256 g | 19 506 g |
| Thrall | Primalist Warden's Bracers | 32 | 16,0 % | 100 000 g | 16 037 g |
| Twisting Nether | Flintrock Shoulders | 77 | 15,3 % | 935 432 g | 142 750 g |
| Antonidas | Flintrock Shoulders | 77 | 15,3 % | 935 432 g | 142 750 g |

Ordenado por ouro esperado, o topo é outro — e quase todo ele tem preço de fonte única
(sem mediana entre reinos, marcado com `!`), porque são peças que existem em menos de
oito reinos:

| reino | item | compradores | P(30d) | preço | ouro esperado |
|---|---|---:|---:|---:|---:|
| Silvermoon | Dustbringer | 184 | 7,4 % | 5 500 000 g | 407 334 g `!` |
| Kazzak | Blisterbane Wrap | 11 | 12,8 % | 2 276 172 g | 290 641 g `!` |
| Blackrock | Flintrock Shoulders | 53 | 10,8 % | 2 501 084 g | 269 416 g `!` |
| Draenor | Dustbringer | 104 | 4,3 % | 5 500 000 g | 234 076 g `!` |

A leitura conjunta responde à pergunta: **quem quer probabilidade com menos concorrência
não vai para Silvermoon.** Silvermoon tem 2 % de prateleira vazia — quase tudo o que se
pode vender ali já está à venda, e cada listagem divide a venda com outra. Os reinos onde
a prateleira está vazia e há compradores são Ravencrest, Howling Fjord, Gordunni,
Drak'thul, Thrall, Elune e Eredar (7 % a 64 % de prateleira vazia). Silvermoon continua a
ser o melhor em ouro total por catálogo, mas para uma listagem isolada vale menos.

Dois filtros entraram aqui e mudaram o resultado. Antes deles, o primeiro lugar de todos
os reinos era `Bloodthirsty Embersilk Gloves`: um item com `sale_rate = 1,0` na TSM, que
é o que a TSM escreve quando viu uma única listagem desaparecer — tratado como
"vende 1 por dia" dava 63 %/mês no Draenor e 597 mil g esperados. Falso. Agora caem
fora os 15 itens regionais com `sale_rate ≥ 1` e os itens com mercado em menos de cinco
reinos.

## 5. O que fazer

1. **Não escolher reino pela concorrência.** O ganho máximo é 2×; a diferença de
   compradores entre reinos é de 10 a 20×. Silvermoon continua a ser o melhor sítio
   medido, com Draenor e Kazzak logo atrás.
2. **Escolher item pelo produto compradores × preço × venda.** O `Tarnished Crown` a
   4 243 g vende quase sempre; o `Dustbringer` a 5,5 M g vende uma vez em cada treze
   meses, mas paga 407 mil g quando vende. São estratégias diferentes, ambas válidas;
   o que não é válido é esperar que a segunda se comporte como a primeira.
3. **Fechar o buraco dos 700–800 mil antes de decidir mais alguma coisa.** É a maior
   reserva de procura não medida e a única que pode mudar o ranking dos reinos.

## 6. Limitações

- A extrapolação de 3,8× assume que as 79 contas medidas representam as 4 746 da banda.
  Se forem atípicas (por exemplo, escolhidas por serem as mais completas do board), o
  número real é menor.
- A conversão de λ regional em λ de reino usa a partilha de compradores da amostra. Para
  itens raros a TSM mede um piso de vendas, não o valor exato.
- `P` é a probabilidade de **uma** listagem vender; não diz nada sobre o preço a que
  vende, nem sobre o tempo até à venda depois de passar.
- O modelo cobre 90 dos 92 reinos. Echsenkessel e Kor'gall não têm nenhuma conta na
  amostra de bitmaps.
- Os preços locais continuam a ter ruído: um anúncio parado a 3,46 M g numa peça que 51
  dos 64 reinos vendem entre 100 e 300 mil inflaciona qualquer média. O ouro por listagem
  usa o preço local cortado a 3× a mediana entre reinos.
