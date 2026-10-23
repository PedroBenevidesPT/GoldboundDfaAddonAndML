# Especificação do Schema de Eventos de Leilão — DFA

Versão do Schema: `1`

Cada evento representa um facto económico observável e atómico na casa de leilões, com carimbo temporal e metadados de auditoria.

## Definição dos Campos

| Campo | Tipo | Obrigatório | Descrição |
|---|---|:---:|---|
| `event_id` | `TEXT` | Sim | Assinatura única determinística: `tipo:itemId:ts:qtd:preço:reino:contraparte` |
| `schema_version` | `INTEGER` | Sim | Versão do schema (actual: `1`) |
| `event_type` | `TEXT` | Sim | Um de: `sale`, `purchase`, `listing`, `cancel`, `expire` |
| `ts` | `TEXT` | Sim | Timestamp UTC formatado em ISO 8601 (`YYYY-MM-DDTHH:MM:SSZ`) |
| `timestamp_unix` | `INTEGER` | Sim | Segundos epoch inteiros (função `time()` do cliente WoW) |
| `item_id` | `INTEGER` | Não | ID numérico do item WoW (nulo se não identificável) |
| `item_name` | `TEXT` | Sim | Nome textual do item em jogo |
| `item_link` | `TEXT` | Não | Hyperlink completo do WoW (`|cnIQ1:|Hitem:...`) |
| `qty` | `INTEGER` | Sim | Unidades transaccionadas ou listadas (mínimo 1) |
| `unit_price_copper` | `INTEGER` | Sim | Preço por unidade em cobre (1 gold = 10,000 copper) |
| `total_price_copper` | `INTEGER` | Sim | Preço bruto total da transacção em cobre |
| `deposit_copper` | `INTEGER` | Sim | Depósito cobrado ou reembolsado em cobre |
| `fee_copper` | `INTEGER` | Sim | Comissão cobrada pelo leilão (consignment cut 5%) |
| `net_proceeds_copper` | `INTEGER` | Sim | Valor líquido creditado/debitado na carteira |
| `realm` | `TEXT` | Sim | Nome do reino WoW onde a acção ocorreu |
| `character` | `TEXT` | Não | Nome da personagem jogadora |
| `faction` | `TEXT` | Não | Facção (`Alliance`, `Horde`, `Neutral`) |
| `counterparty` | `TEXT` | Não | Nome do comprador ou vendedor (quando disponível na factura) |
| `source` | `TEXT` | Sim | Identificador do subsistema (`journalator_archive`, `dfa_addon`, etc.) |
| `provenance` | `TEXT` | Sim | Ficheiro e secção de origem |
| `confidence` | `TEXT` | Sim | Grau de certeza: `verified` (factura/API directa) ou `inferred` |
| `market_ref_copper` | `INTEGER` | Não | Referência `dbmarket` da TSM capturada no momento da postagem |
| `raw_json` | `TEXT` | Não | Payload JSON original preservado para auditoria |
| `imported_at` | `TEXT` | Sim | Timestamp UTC de inserção na base de dados DFA |

## Exemplo de Linha JSONL

```json
{
  "event_id": "sale:2299:1792249652:1:27130000:Dragonmaw:Ggphjopibiln",
  "schema_version": 1,
  "event_type": "sale",
  "ts": "2026-10-19T22:07:32Z",
  "timestamp_unix": 1792249652,
  "item_id": 2299,
  "item_name": "Burning War Axe",
  "item_link": "|cnIQ1:|Hitem:2299::::::::80:103:::::::::|h[Burning War Axe]|h|r",
  "qty": 1,
  "unit_price_copper": 27130000,
  "total_price_copper": 27130000,
  "deposit_copper": 500,
  "fee_copper": 1356500,
  "net_proceeds_copper": 25774000,
  "realm": "Dragonmaw",
  "character": "Bullbound",
  "faction": "Horde",
  "counterparty": "Ggphjopibiln",
  "source": "journalator_archive",
  "provenance": "Journalator.lua:Invoices",
  "confidence": "verified",
  "market_ref_copper": 28000000
}
```
