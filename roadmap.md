# Roadmap — DfA ML decide realms e estrategia

## Fase 0 — Dados (ja em curso)
- Snapshots TSM por reino: `fetch_tsm_snapshots.py` (todos os grupos EU)
- Historico regional: `sync_market.py` (AppData + CSVs publicos)
- Journalator: vendas/compras reais por item e reino
- In-game: eventos de leilao (DFA_AuctionIntelligence, 7.691 eventos)

## Fase 1 — Selecção de realms (ML)
**Objectivo:** o motor ranqueia realms por net gold/dia por capital, nao por heuristica.

- [ ] `best_realm.py` -> modelo de score por realm (demanda x preco x venda P30)
- [ ] Adicionar custos (deposito, house cut 5%, tempo) ao score
- [ ] `mog_realm_profiles.py` -> clusters de realms com perfil similar
- [ ] Decisao: quais realms manter, quais abandonar, quais expandir
- [ ] Comando: `python scripts/best_realm.py --basket inventory --csv reports/melhor_reino.csv`

## Fase 2 — Precos por realm
**Objectivo:** o modelo de precos usa referencias per-realm quando existem.

- [x] Shipped model (regional) — ja treinado
- [x] Per-realm shipped model — treinado por reino e exportável com `--realm-model` (Dragonmaw hoje)
- [ ] `live_references()` ja mistura local + regional
- [ ] Fallback: regional quando o reino nao tem dados

## Fase 3 — Inventario e capital
**Objectivo:** saber quanto capital esta imobilizado e onde.

- [ ] `inventory_prob.py` — probabilidade de venda por item/reino
- [ ] `compare_inventory.py --realms` — comparar stock entre realms
- [ ] Calcular capital imobilizado por reino
- [ ] Regra: nao expandir para um realm se o capital ja esta concentrado

## Fase 4 — Episodios de listagem (recorder/linker)
**Objectivo:** ligar cada listing ao seu outcome (venda/cancel/expire).

- [ ] Linker de `listing_id` em DFA_AuctionIntelligence
- [ ] Registar: preco pedido, tempo ate venda, gold recebido, custos (deposito, house cut)
- [ ] Agrupar por categoria/liquidez
- [ ] Evitar conclusoes de uma unica venda

## Fase 5 — Modelo de hazard (tempo ate venda)
**Objectivo:** prever tempo ate venda e probabilidade de venda por ask.

- [ ] Survival/hazard hierarquico por item/categoria/realm
- [ ] Censura (listings nao vendidos)
- [ ] Net gold/dia por capital como metrica de decisao
- [ ] Comparar estrategias: preco base, +10%, -10%, etc.

## Fase 6 — Regras de decisao (analysis only)
**Objectivo:** recomendar accoes, nunca executar.

- [ ] Manter vs vender vs cortar perdas (por item)
- [ ] Take-profit: se ROI > X%, recomendar vender
- [ ] Stop-loss: se perda > Y% ou dias > Z, recomendar cortar
- [ ] Reprice: se tempo ate venda > threshold, sugerir ajuste de preco
- [ ] Nunca auto-post, cancel, undercut ou reprice

## Fase 7 — In-game (ja em curso)
- [x] Tooltip com estimate (Auctionator mechanism)
- [x] TSM source `dfaestimate`
- [x] LootAppraiser: armor/weapon/recipe usam dfaestimate (frozen table)
- [x] Auctionator price fill (one-shot, gross-up 5%)
- [x] Tooltip com `Best realm` e `Babysit risk` (medido, omitido quando não há dados)
- [x] HUD com botões (Snapshot, Undercut scan, Mail, Export) e fila de acções
- [x] Inventário in-game: bags, banco por abas (11.2), warband, posted com preço, guild bank
- [ ] Mostrar net gold/day no tooltip (futuro)

## Fase 8 — Continuous learning
**Objectivo:** o motor aprende com os outcomes reais.

- [x] Re-treino periodico com novos dados (`scripts/watch.py`, 6h)
- [x] Export do dataset por reino e para todos os reinos (factor + undercut share)
- [x] Alertas (desktop + webhook Discord) por undercut, reset e erro
- [ ] A/B de estrategias de preco (10% up/down)
- [ ] Comparar net gold/day e imobilizacao de inventario
- [ ] Alertar quando a estrategia deixa de funcionar (drift)

## Estado actual do ML vs este roadmap (2026-10-21)

Rotina operacional: [daybyday.md](daybyday.md).

Fonte: `reports/dfa_sentinel/sentinel_report.json` (artifact
`20261021T130057Z-e9f9e41c`, camada calibrada sklearn+CatBoost),
`reports/dfa_ml/dfa_ml_report.md` (modelos originais em `models/dfa_ml/`),
`data/intel.sqlite` e `reports/dragonmaw_realm_ml.json`. Nada aqui é uma
promessa: cada linha diz o que foi medido e onde falha. O relatório detalhado
da camada nova está em [reports/dfa_sentinel_ml.md](reports/dfa_sentinel_ml.md).

| fase | estado | evidência | o que falta |
|---|---|---|---|
| 0 — Dados | **em curso** | 4.357 listings, 10 reinos; 465 vendas ligadas, 2.253 expirações, 274 cancelamentos, ~1.300 sem outcome; ligação listing→venda 99,8% exacta ao preço | janela de ~5 dias (16–21 Out); commodities ausentes; o inventário agora inclui `posted` no AH e o banco de guilda (partilhado, fora da reconciliação) |
| 1 — Selecção de realms | **parcial, sem ML** | `best_realm.py` é analítico: demanda × preço × P30, não um modelo treinado | score aprendido por reino; custos (depósito, 5%, tempo) no score |
| 2 — Preços por reino | **regional shipped + factor por 267 reinos; preço realizado validado** | Sentinel: CatBoost sem features de ask, MAE log 0,402, MAE 3,68g, 44,0% dentro de 25%, 68,1% dentro de 50% em 116 vendas de teste; baseline mediana-por-item MAE log 0,773 / 7,49g. O export indexa 267 reinos: 5 com vendas próprias, os outros a 1,00 (baseline regional) | mais vendas nos outros reinos; o factor dos 259 sem amostra não é uma medição; o preço realizado ainda erra 56% fora de ±25% |
| 3 — Inventário e capital | **parcial** | snapshots bags/banco/posted com reconciliação; `inventory_prob.py` e `compare_inventory.py` existem | capital imobilizado agregado por reino; regra de não expandir com capital concentrado |
| 4 — Episódios de listing | **feito no dataset, não no addon** | 4.204 listings com outcome: 465 vendas, 2.251 expirações, 274 cancelamentos, 1.260 sem outcome; ligação 99,8% exacta ao buyout | `listing_id` estável dentro do DFA_AuctionIntelligence; hoje a ligação é reconstruída no Python |
| 5 — Hazard / tempo até venda | **probabilidade 24/48/168h validada e calibrada; tempo não** | P(venda) log loss 0,382/0,386/0,393, Brier 0,121/0,122/0,123, ECE 0,031/0,039/0,040; baseline item-rate Brier 0,169. Tempo: C-index 0,403 — abaixo do limite 0,60, por isso não é exportado | features de competição (undercut) no hazard; mais história; o C-index do tempo continua sem sinal |
| 6 — Regras de decisão | **parcial, agora com cancel/expire** | Modelo D escolhe o ask por EV/probabilidade/prazo; ask mediano 0,28x o usado; nos listings que venderam pede 1,48x; gold esperado por listing 0,206 → 0,599 (+190,5%), sobre 1.620 listings. `intel.py relist` decide entre `hold`, `cancel_and_relist`, `let_expire` e `watch` por gap, fila e horas restantes | take-profit, stop-loss e reprice por dias; o `relist` usa regras, não o modelo; nada disto executa ordens (por desenho) |
| 7 — In-game | **feito, menos o net gold/dia** | tooltip DFA_Estimate, fonte TSM, LootAppraiser, price fill do Auctionator | net gold/dia e banda de confiança no tooltip |
| 8 — Continuous learning | **automático, com promoção atómica** | `watch.py` importa o que muda, treina realm-ml (6h), corre `sentinel --if-needed` (24h) e exporta; artifacts versionados com checksums, `CURRENT` só depois de validar. Alerta (desktop/Discord); `logs/watch.log` e `data/watch_state.json` dizem o que correu | A/B de estratégia, detecção de drift, calibração por reino com mais dados |

### O que já existe e não estava no roadmap

- **DFA Sentinel**: um daemon (`scripts/watch.py`) que vê a assinatura do export do
  addon, do Journalator, do AppData e dos CSVs, e corre só a etapa que a mudança
  implica. O AH da Blizzard é lido de hora a hora com `If-Modified-Since`; o treino
  e o export têm cooldowns e nunca correm a cada tick.
- **Undercut fora do jogo**: `intel.py blizzard-undercut` cruza os teus leilões
  (item, auction id, preço, tempo) com o retrato da API, sem seller. É a fonte de
  competição que não exige o cliente aberto; `ah_undercut.detect_price_resets`
  assinala buy-out + reposição (preço ≥30 % acima e oferta ≥40 % abaixo).
- **Preço para todos os reinos**: `realmIndex` no dataset dá a cada reino um factor
  contra o regional e a competição medida; o tooltip mostra o melhor reino e o
  risco de babysit do reino actual.
- **HUD + fila de acções**: quatro botões in-game gravam `actions_jsonl`; o daemon
  importa as pressões novas e corre o seguimento (scan → relist + alerta).
- **Compradores por correio**: `mail_invoice` + `sale_buyer_link` ligam o nome do
  comprador à venda que já existe no journal, sem inserir vendas. 151 de 762 vendas
  Dragonmaw têm comprador; o resto espera uma invoice.
- **Sellers por undercut, sem Auctionator**: `resultInfo.owners` traz o vendedor que
  a UI da Blizzard não mostra. `/dfaai undercut` faz a sua própria query de owned
  auctions e uma pesquisa por item (uma por vez, com timeout de 6 s), e guarda item,
  o teu preço, o mais barato e o seller. O hook ao Auctionator fica como captura
  passiva, sem queries extra. É a primeira fonte de **profundidade de mercado** com
  identidade — a feature que faltava ao Modelo A/B.
- **Cancelar vs expirar**: `intel.py relist` responde com regras explícitas (gap,
  unidades à frente, horas restantes), com o ask sugerido 1 cobre abaixo do mais
  barato. O depósito é tratado como sunk; nada é cancelado automaticamente.
- **Inventário completo**: `posted` passa a legível quando o AH carrega a lista de
  owned auctions (não no login, onde é desconhecido), e o banco de guilda é lido ao
  abrir — gravado mas fora da reconciliação, porque é partilhado.
- **Modelo por reino** já treina e grava (`models/intel/eu/dragonmaw/price_model.cbm`),
  mas ainda não alimenta o preço mostrado in-game.
- **Reconciliação de inventário**: uma descida sem evento é `unexplained_decrease`, e
  `posted` com o AH fechado fica desconhecido em vez de zero.

### O que o roadmap assume e os dados não suportam

- **Tempo até à venda**: com concordância 0,376, qualquer número seria inventado. Fica
  fora até haver mais história e melhores features.
- **Horizonte > duração do leilão**: 7d/14d não é mais probabilidade, é a mesma pergunta
  com o leilão já terminado. O roadmap deve falar de 12/24/48h.
- **Pooling entre reinos**: os 9 reinos com 0–23 vendas não emprestam força ao
  Dragonmaw; `insufficient_data` é o resultado correcto, não um bug.

### Próximas prioridades (ordem sugerida)

1. **Histórico**: manter o import diário e deixar a janela crescer de 5 dias para
   semanas. É o limitante de todos os números acima — agora o Sentinel trata do
   import sozinho, portanto o limitante é o tempo, não o esquecimento.
2. **Fazer do `--realm-model` o default** para os reinos que ganham à mediana-por-item na
   janela intocada (Dragonmaw: 0,618 vs 1,078), mantendo o regional como fallback e
   dizendo no output qual modelo respondeu.
3. **Metade da Fase 5 que falta**: usar o undercut como feature de competição no
   P(venda) e no EV, em vez de só `sold_per_day` regional.
4. **Compradores**: com mais invoices ligadas, medir recompra por comprador (quem volta
   ao mesmo item) — sinal de demanda que nenhum snapshot tem.
5. **Commodities**: ingestão regional marcada como regional, para os 977 itens de farm
   sem listings deixarem de ser um ponto cego.

### Fase 9 — Sentinel (feito em 2026-10-21)

- **Calibração:** isotonic/Platt ajustado num bloco cronológico intermédio,
  nunca no teste. A selecção usa log loss; Brier/ECE/AUC são reportados ao lado.
- **Baselines:** item-rate e item-median são treinados no mesmo bloco e
  avaliados no mesmo teste intocado. A vantagem só é declarada quando o
  ganho excede o limiar configurado.
- **Risco:** `undercut_pressure` (24/48h) e `price_decline` (24/48h), ambos a
  partir de snapshots com timestamps; o primeiro é pressão de mercado, não
  prova de que um leilão nosso foi undercut.
- **Cobertura:** modelo pooled com contexto de reino; 230 registos
  `realm (pooled model)`, 7 `region (pooled fallback)`, 0 `unknown`.
- **Tempo até venda:** continua **não operacional** (C-index 0,403).
- **Lifecycle:** `models/dfa_sentinel/<versão>/` + `CURRENT` atómico,
  checksums, schema de features, detecção de stale (14 dias) e fallback
  explícito. `watch.py` só refita quando há labels novas suficientes.
- **Dataset:** schema 2, 237 registos, 31 184 preços, ~696 KB; o leitor aceita
  schema 1 e 2. Campos novos: `psellByHorizon`, `ereal`, `elow`, `ehigh`,
  `undercut`, `decline`, `scope`, `model`, `status`, `fallback`, `dq`.

## Comandos chave

```powershell
# Dados
python scripts/sync_market.py --snapshots
python scripts/fetch_tsm_snapshots.py --kinds realm_items --workers 1 --delay 10

# Analise de realms
python scripts/best_realm.py --basket inventory --csv reports/melhor_reino.csv
python scripts/mog_realm_profiles.py --show
python scripts/compare_inventory.py --realms

# Treino
python scripts/dfa_ml.py train --refresh-journal

# Dataset in-game
python scripts/dfa_pricing_export.py
```

## Cron / automacao (Windows Task Scheduler)

Substituído pelo daemon: ele corre o que mudou, não uma lista fixa.

```powershell
# sessao: deixa a janela aberta
python scripts\watch.py

# ao login, sem janela
schtasks /Create /SC ONLOGON /TN "DFA Sentinel" /TR "pythonw C:\FarmAnalysis\scripts\watch.py"
```

O daemon cobre: import do addon, journal, sync do AppData, treino por reino (6h),
export do dataset (6h ou quando o journal/referências mudam), AH da Blizzard (1h),
undercut fora do jogo, reset de mercado e alertas. Os comandos manuais ficam para
verificação: `run_all.py --only sync,inventory,journal,pricing` e
`watch.py --once --verbose`.

Pipeline completo em sequencia (offline, sem browser), quando se quer forçar:

```powershell
python scripts/run_all.py --only sync,inventory,journal,pricing
```

Agendar no Task Scheduler:
1. Abrir `taskschd.msc`
2. "Create Task" -> Triggers: diario, hora conveniente (ex: 03:00)
3. Action: Start a program -> `powershell.exe`
   Arguments: `-ExecutionPolicy Bypass -File C:\FarmAnalysis\run_daily.ps1`
4. Conditions: "Start only if on AC power" (opcional)
5. Settings: "Run whether user is logged on or not" (se quiseres background)

Para correr manualmente:
```powershell
powershell -ExecutionPolicy Bypass -File C:\FarmAnalysis\run_daily.ps1
```

**Nota:** o `fetch_tsm_snapshots.py` demora ~45 min com delay 10s. Ajustar `--snapshot-delay` ou `--snapshot-workers` conforme o tempo disponivel. Usar `--snapshot-limit` para smoke tests.

## Regra de ouro
**Analysis only.** O motor recomenda, nunca executa. Nenhuma accao automatica sobre o leilao.
