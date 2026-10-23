# Relatório de Cobertura de Eventos — DFA Auction Intelligence

Este relatório categoriza formalmente quais eventos e variáveis são **implementados**, **verificados empiricamente**, **inferidos**, ou **não suportados** pelas APIs do cliente World of Warcraft Retail.

## Matriz de Cobertura

| Evento / Métrica | Estado | Fonte de Dados | Observações e Limitações |
|---|:---:|---|---|
| **Vendas da Própria Conta (`sale`)** | **Verificado** | `GetInboxInvoiceInfo` | Factura directa com valor, quantidade, taxa e comprador. |
| **Compras da Própria Conta (`purchase`)** | **Verificado** | `GetInboxInvoiceInfo` | Factura de correio ao recolher item ganho. |
| **Publicações (`listing`)** | **Verificado** | `C_AuctionHouse.PostItem` / `PostCommodity` | Preço pedido, duração, quantidade e depósito calculado. |
| **Expirações (`expire`)** | **Verificado** | Mailbox (`GetInboxHeaderInfo`) | Aviso de devolução por expiração de tempo. |
| **Cancelamentos (`cancel`)** | **Verificado** | Mailbox (`GetInboxHeaderInfo`) | Aviso de devolução por cancelamento voluntário. |
| **Taxas de Leilão (Consignment 5%)** | **Verificado** | Campo `consignment` do invoice | Registado exacto em cobre na factura. |
| **Depósitos de Leilão** | **Verificado** | `CalculateItemDeposit` / `CalculateCommodityDeposit` | Reembolsado na venda, perdido na expiração. |
| **Tempo até à Venda (`time_to_sale`)** | **Inferido / Linkado** | Pareamento `listing` -> `sale` na mesma janela | Alta confiança quando o item só teve 1 anúncio antes da venda; aproximado se houver múltiplos reposts simultâneos. |
| **Clearance Empírica E2** | **Verificado** | `sale_price / local_wall` no timestamp da venda | Medido contra o estado histórico da parede em `tsm_price_change`. |
| **Vendas de Outros Jogadores** | **Não Suportado** | API Blizzard Não Expõe | A Blizzard não envia o stream de vendas alheias; apenas snapshots de anúncios activos (`tsm_market`). |
| **Intenção de Compra de Itens em Falta** | **Não Suportado** | DfA `missing` | DfA mede coleccionadores que não têm a aparência; é sinal de potencial de procura, nunca garantia de intenção de compra. |
| **Custo de Aquisição (Histórico Antigo)** | **Não Suportado** | Sem registo contabilístico | Só itens comprados via leilão após instalação do monitor têm custo de aquisição gravado. |

## Diagnóstico das 5.992 Observações na Base

- **Eventos com Timestamp Válido**: 5.992 (100.0%)
- **Eventos com ID de Item Numérico**: 5.865 (97.9%) — os restantes 2.1% são resolvidos por matching exacto de nome.
- **Transacções com Preço e Quantidade Positivos**: 5.992 (100.0%)
- **Comissões Registadas em Vendas**: 438 vendas têm a taxa exacta de 5% discriminada.
