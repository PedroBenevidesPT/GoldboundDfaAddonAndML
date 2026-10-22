# DFA Auction Intelligence — Arquitectura de Fluxo de Dados e Proveniência

Este documento define a cadeia de custódia, proveniência e integridade dos dados observados no sistema de leilão WoW pelo projecto DFA.

```mermaid
flowchart TD
    subgraph WoW_Client["Cliente WoW (_retail_)"]
        BLIZZ["Blizzard AH & Mailbox APIs"]
        JNR["Journalator v170+ (Archivist Stores)"]
        TSM["TradeSkillMaster (dbmarket, Ops)"]
        
        ADAPTER["JournalatorAdapter.lua"]
        NATIVE["AuctionEvents.lua (Native Hooks)"]
        
        STORE["DataStore.lua (Deduplicação & Assinaturas)"]
        EXPORT["Export.lua (SavedVariables)"]
        
        BLIZZ -->|PostItem/PostCommodity/Mailbox| JNR
        BLIZZ -->|Hooks directos fallback| NATIVE
        JNR -->|GetRange / AddToLogs| ADAPTER
        TSM -->|Snapshot dbmarket| NATIVE
        
        ADAPTER -->|Eventos normalizados| STORE
        NATIVE -->|Eventos normalizados| STORE
        STORE -->|JSONL serializado| EXPORT
    end

    subgraph DFA_Pipeline["Pipeline DFA / FarmAnalysis"]
        SV_FILE["DFA_AuctionIntelligence.lua / Journalator.lua"]
        INGEST["services/auction_intelligence.py"]
        DB[("tsm_markets.db : auction_events")]
        ANALYTICS["analytics/auction_intelligence.py"]
        PRICING["Pricing Groups / Token Rush"]
        REPORT["reports/auction_intelligence_report.md"]
        
        EXPORT -.->|WTF SavedVariables| SV_FILE
        SV_FILE -->|python scripts/auction_intelligence.py import| INGEST
        INGEST -->|Idempotente / Validação| DB
        DB --> ANALYTICS
        ANALYTICS --> PRICING
        ANALYTICS --> REPORT
    end
```

## 1. Proveniência por Tipo de Evento

| Evento | Fonte Primária | Gatilho | Proveniência / Campos Verificados | Confiança |
|---|---|---|---|---|
| **Venda Concluída (`sale`)** | Mailbox (`GetInboxInvoiceInfo`) | Recolha do correio com ouro | Ouro bruto, comissão da AH (5% consignment), reembolso de depósito, nome do comprador, timestamp UNIX | **Verificado** |
| **Compra Concluída (`purchase`)** | Mailbox (`GetInboxInvoiceInfo`) | Recolha do item comprado | Ouro pago, quantidade, nome do vendedor, timestamp UNIX | **Verificado** |
| **Publicação (`listing`)** | `C_AuctionHouse.PostItem` / `PostCommodity` | Momento em que o anúncio é submetido | Preço pedido (buyout/bid), quantidade, duração, depósito cobrado, timestamp UNIX | **Verificado** |
| **Cancelamento (`cancel`)** | Mailbox (`GetInboxHeaderInfo`) | Anúncio cancelado pelo jogador | Item devolvido, quantidade, depósito perdido, timestamp UNIX | **Verificado** |
| **Expiração (`expire`)** | Mailbox (`GetInboxHeaderInfo`) | Anúncio expirado após duração | Item devolvido, quantidade, duração decorrida, depósito perdido, timestamp UNIX | **Verificado** |
| **Referência de Mercado (`market_ref`)** | `TSM_API.GetCustomPriceValue` | No momento da publicação | `dbmarket` local no instante em que o item é listado | **Proxy Medido** |

## 2. Princípios de Separação de Conceitos

1. **Preço Pedido (Asking Price)**: Valor fixado na publicação pelo jogador ou concorrente. Não é receita nem liquidez.
2. **Referência de Mercado (`dbmarket`)**: Média local ponderada de anúncios. Não reflecte necessariamente transacções.
3. **Facturação Bruta (Gross Proceeds)**: Total em cobre transferido pelo comprador na factura da casa de leilões.
4. **Facturação Líquida (Net Proceeds)**: `Facturação Bruta - Comissão Consignment (5%) + Reembolso de Depósito`.
5. **Custo de Depósito Perdido**: Depósitos retidos pela Blizzard em leilões que expiraram sem venda.
6. **Sell-Through Rate Real**: Denominador restrito a desfechos definitivos: `Vendas / (Vendas + Expirações)`. Cancelamentos não são falhas de procura.
