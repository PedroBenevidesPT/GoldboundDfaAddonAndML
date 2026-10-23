# Vendoring: deposit versus asked price

Objectivo: deixar os mogs lentos tentarem vender — uma expiração de 1–2 g pode ser um gamble aceitável — e só parar quando o **ask já não justifica o depósito repetido**. Vendor value alto é a excepção: aí protege-se o item de ser liquidado por pouco.

## Regra inicial

- **24 h:** depósito padrão estimado = `30% * vendorsell` por tentativa.
- **48 h:** aproximadamente `60% * vendorsell` por tentativa.
- Para asks até **1,000 g**, permite até **5 expirações** antes de parar/rever, desde que o depósito por tentativa seja pequeno relativamente ao ask.
- Para asks acima de **1,000 g**, não vendas só por low `avgSale`, `spd` baixo, ou várias expirações. Deixa como listing paciente e revê o ask, a concorrência e o custo acumulado.
- **Excepção protectora:** se o depósito unitário já está perto do ask (regra de partida: `deposit >= 80% * ask`), evita o ciclo longo; após falhar, compara vendor/desenchant e considera vendor.

Os 1,000 g e 5 tentativas são parâmetros iniciais para gerir risco, não prova de liquidez. `DBRegionSaleAvg` é regional e sujeito a ruído; não é preço de venda garantido no teu reino. A parede local (`dbminbuyout`) é um ask, não uma venda. “Pedir mais que o depósito” permite aceitar o gamble, mas não garante lucro: a venda pode nunca acontecer. O depósito de item vendido é normalmente reembolsado; expirações/cancelamentos queimam depósito. Confirma a quantia no AH para o item concreto: commodities e alguns casos especiais podem diferir.

## TSM Auctioning operation — `Mog slow / 24h`

| Campo | Valor |
|---|---|
| Duration | `24 hours` |
| Don't post after this many expires / Max Expires | `5` (só no grupo de teste ≤1,000 g) |
| Undercut | `1c` para liquid; `0c`/sem cancel scan em slow collector conforme a operação de preço |
| Post at | `Normal` ou `Maximum` conforme a estratégia de slow mover; não baixar automaticamente só por expirations |

Para itens patient/collector acima de 1,000 g, usa operação separada com Max Expires `0` (sem limite) e revisão manual; não os mistures no grupo automático de cinco tentativas. O contador `NumExpires` inclui expirações e cancelamentos desde a última venda, portanto não faças cancel scan agressivo nesse grupo.

## Vendoring operation — fluxo seguro

TSM Vendoring **não** é disparado automaticamente pelo Max Expires. Quando `NumExpires` chega ao limite, Auctioning deixa de postar; depois abre Vendoring, inspeciona os candidatos e confirma manualmente.

| Campo | Valor inicial |
|---|---|
| Enable Selling | on |
| Enable Buying | off |
| Keep quantity | `0` |
| Min number of expires | `5` |
| Market value | `dbregionsaleavg` (usar apenas como referência; não como valor pedido) |
| Max market value | não usar um corte universal de `100g`; definir por preço/depósito no Price Tester ou deixar a operação em revisão manual |
| Destroy value | `first(destroy, 0c)` |
| Max destroy value | `vendorsell` (protege itens cujo disenchant supera o vendor; confirmar na UI) |
| Scope | grupo separado de itens candidatos; nunca Base Group |

**Importante:** O TSM expõe `NumExpires` como Value Source e `vendorsell`, `dbminbuyout` como Price Sources; os campos Market/Max Market Value aceitam expressões de preço. Contudo, se a sintaxe condicional ou a semântica da UI não for validada no teu TSM Price Tester, **não apliques vendoring automático**: gera a lista e faz revisão manual antes de vender.

Condição económica de referência (não é um campo TSM pronto):

```text
ask_local <= n * deposit_per_post
ask_local <= n * duration_rate * vendorsell
```

Com 5 tentativas a 24 h, o ponto bruto é `ask_local <= 5 * 0.30 * vendorsell` (`1.5 * vendorsell`). Isto não inclui o custo de aquisição, comissão de venda nem valor esperado do comprador. A regra protectora “depósito unitário ≥80% do ask” é mais conservadora para vendor value alto.

## Ciclo operacional

1. Separa dois grupos: **Slow Gamble ≤1,000 g** (24 h, Max Expires 5) e **Collector/Patient >1,000 g** (sem Max Expires automático).
2. Antes de anunciar, compara ask local e depósito mostrado no AH. Se `deposit >= 80% * ask`, não assumes cinco tentativas: considera o alto custo de vendor e decide manualmente.
3. Após cinco falhas no grupo Gamble, compara novamente ask atual, número de cópias, preço plausível, vendor value, destroy e custo de aquisição. Não vende automaticamente só porque falhou cinco vezes.
4. Vende apenas os itens cuja venda esperada no AH já não compensa o depósito/tempo e cujo vendor seja preferível a disenchant. Itens sem ask/dados vão para revisão, não para auto-vendor.

## Exemplos

| Ask | Vendor value | Depósito 24 h estimado | Decisão inicial |
|---:|---:|---:|---|
| 500 g | 3 g | 0.9 g | Até 5 tentativas razoáveis; depósito unitário é pequeno vs ask. Rever após falhas. |
| 1,000 g | 10 g | 3 g | Cinco tentativas podem ser um gamble aceitável (15 g total potencialmente perdido). |
| 100 g | 300 g | 90 g | Depósito equivale a 90% do ask: não fazer cinco posts cegos; alto vendor value, rever/vendor ou disenchant após primeira falha. |
| 20,000 g | 10 g | 3 g | Não vendor por movimento lento: ask muito maior que depósito, manter patient listing se o preço for plausível. |

Estes exemplos usam a taxa padrão 30% por 24 h, apenas para ilustrar. A UI do Auction House é a fonte final do depósito real. O depósito volta quando vende (a taxa AH não); quando expira/cancela é custo. Não confundir o ask pedido com o lucro realizado.

## Como se relaciona com MARKET_OPPORTUNITIES.md

- `0.01–0.1/day` = slow; `<0.01/day` positivo = very slow; zero = unproven; ausência = unknown. Nenhum destes rótulos é ordem para vendor.
- Manter listings de coleccionador pacientes quando o ask é defensável e custo de depósito é baixo; não descartar por vender talvez só uma vez em 100 dias.
- Usar Journalator/Accounting como evidência de venda, com item/reino/preço e número de expirations. `DBRegionSaleAvg` e a falta de aparência no DfA não são prova de comprador àquele preço.

## Limitações e validação

- Taxa padrão reportada para itens normais: 30% vendor value a 24 h, 60% a 48 h; confirma o depósito mostrado no jogo para aquele item. O depósito costuma ser reembolsado ao vender. Fontes: [WoW Auction House](https://warcraft.wiki.gg/wiki/Auction_House), [Blizzard Auction House Update](https://us.forums.blizzard.com/en/wow/t/auction-house-update/10385), [TSM Auctioning Operations](https://support.tradeskillmaster.com/en_US/tsm-addon-documentation/1072146-tsm-addon-auctioning-operations), [TSM Vendoring Operations](https://support.tradeskillmaster.com/en_US/tsm-addon-documentation/tsm-addon-vendoring-operations).
- `NumExpires` conta expirations/cancelamentos desde a última venda. Cancel scans podem empurrar o item até ao limite mais cedo.
- Price sources e fields do TSM têm versão/scope próprios. Testa strings na Price Tester; até validado, faz revisão manual e não auto-vende.
- A contagem antiga `101 itens com avgSale ≤100g` é apenas estatística descritiva; já não é uma lista automática de lixo.
- O depósito acumulado é custo realizado em cada falha; cinco tentativas a 24 h custam cerca de `1.5 * vendorsell` antes de outras variáveis. Uma venda pode cobrir esses custos, mas não é garantida.
