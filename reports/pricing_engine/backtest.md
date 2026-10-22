# Walk-forward do motor de preço

Para cada par de fetches consecutivos de cada reino, o motor estima o preço **com os dados disponíveis nesse momento** (o estado local reconstruído do log de alterações até *t*, a referência regional e o factor de clearance do snapshot da região em vigor) e compara-se com o nível e com a parede do fetch seguinte. **Não entra nenhum dado futuro.**

Observações: 517,917.

| estimador | alvo | n | erro mediano | p25 | p75 |
|---|---|---:|---:|---:|---:|
| historico regional | nivel local | 517,917 | 0.800 | 0.287 | 3.610 |
| historico regional | parede seguinte | 430,444 | 0.896 | 0.313 | 5.170 |
| local (dbmarket) | nivel local | 516,529 | 0.007 | 0.000 | 0.036 |
| local (dbmarket) | parede seguinte | 429,115 | 0.110 | 0.007 | 0.472 |
| media de venda regional | nivel local | 492,475 | 0.876 | 0.583 | 0.970 |
| media de venda regional | parede seguinte | 412,713 | 0.865 | 0.538 | 0.969 |
| modelo do motor | nivel local | 517,917 | 0.885 | 0.600 | 0.970 |
| modelo do motor | parede seguinte | 430,444 | 0.874 | 0.556 | 0.969 |
| regional (market) | nivel local | 517,917 | 0.768 | 0.251 | 3.356 |
| regional (market) | parede seguinte | 430,444 | 0.844 | 0.274 | 4.757 |

**O que isto é:** uma avaliação da **exactidão do nível** (o erro relativo entre o que cada estimador diz e o que o mercado fez). **Não** é uma avaliação de venda realizada: o alvo é um ask, não uma transacção.

**As duas validações medem coisas diferentes, e é essencial não as confundir:**

- O walk-forward mede **previsibilidade do nível de mercado**. Aí o `dbmarket` local é o melhor (erro mediano 0,110 contra a parede seguinte): é um ask médio estável, e o próximo ask sai dele.
- A calibração mede **viés contra o preço realizado**. Aí o `dbmarket` local é o **pior** (3,9× o preço realizado) e a média de venda regional é a melhor (0,91×).

Ou seja: uma fonte pode ser uma **previsão excelente do mercado** e ao mesmo tempo um **estimator péssimo do valor realizável**. O motor ancora-se nas transacções justamente por isso, e por isso o seu erro de nível é maior do que o do `dbmarket`: ele não está a tentar adivinhar o ask, está a estimar o que se recebe.

**O que não é:** prova de que o motor vende mais, mais depressa ou com mais lucro. Para isso é preciso o journal datado, que não existe.
