# Day by Day — DfA

Comandos do dia a dia. Correr na raiz do repo (`C:\FarmAnalysis`).

Regra única que evita quase todos os problemas: **o jogo escreve, o Python lê depois**.
O addon só grava nas SavedVariables no logout (ou quando fazes `/reload`); por isso
`intel.py addon-import` corre **depois** de sair do jogo, nunca com o cliente aberto.

Desde 2026-10-21 isto é automático: `python scripts\watch.py` (DFA Sentinel) vê o
que mudou e corre só o que a mudança implica — import, journal, sync, treino,
export, snapshot da Blizzard e o undercut fora do jogo. Os comandos manuais abaixo
ficam como verificação e para quando quiseres forçar uma etapa.

## 0. O ciclo, numa tabela

| quando | o que o addon grava | o que corres depois |
|---|---|---|
| login / logout | eventos do Journalator, inventário (bags, banco, posted) | `intel.py addon-import` |
| postar | posting (via Journalator) | nada manual |
| abrir AH / `OWNED_AUCTIONS_UPDATED` | `posted` (leilões teus) legível | `intel.py addon-import` |
| abrir banco / banco de warband | `bank` (abas compradas) e `warband` (conta), este nunca reconciliado | `intel.py addon-import` |
| abrir banco de guilda | `guildbank` (abas visíveis), nunca reconciliado | `intel.py addon-import` |
| botão do HUD (`/dfaai hud`) | acção em `actions_jsonl`: snapshot, undercut, mail ou export | o daemon importa e corre o seguimento |
| `/dfaai undercut` ou cancel scan do Auctionator | sellers abaixo do teu preço, por item | `intel.py undercut`, `intel.py relist` |
| abrir correio / Open All | invoices com o nome do comprador | `intel.py addon-import` (liga o comprador à venda) |
| venda | sale com preço e quantidade | `dfa_ml.py train --refresh-journal` |

## 1. Todos os dias, uma vez

Com o Sentinel a correr, não há passo diário nenhum: ele importa quando o ficheiro
muda, treina no máximo de 6 em 6 horas, exporta quando o journal ou as referências
mudam e vai buscar o retrato da Blizzard de hora a hora.

```powershell
# deixa isto numa janela (ou no Task Scheduler, ao login)
python scripts\watch.py

# uma passagem para ver o plano e o resultado
python scripts\watch.py --once --verbose
```

Se preferires o ciclo manual (ou para verificar o que o daemon fez):

```powershell
# 1. mercado TSM está fresco?
python scripts/sync_market.py --check
python scripts/sync_market.py                     # se estiver velho

# 2. opcional: snapshots por reino (demora ~45 min com delay 10)
python scripts/sync_market.py --snapshots --snapshot-kinds realm_items --snapshot-workers 1 --snapshot-delay 10
```

```powershell
# 3. treino (só faz sentido com vendas novas; demora alguns minutos)
python scripts/dfa_ml.py train --refresh-journal
# relatório: reports/dfa_ml/dfa_ml_report.md

# 4. dataset do addon in-game
python scripts/dfa_pricing_export.py
$src="C:\FarmAnalysis\DFA_Estimate"; $dst="C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns\DFA_Estimate"; robocopy $src $dst /MIR /NFL /NDL /NJH /NJS /NP
```

In-game: `/reload`.

```powershell
# 5. depois de sair do jogo (ou de /reload): importar tudo o que o addon gravou
python scripts\intel.py addon-import

# 6. compradores das vendas sem counterparty (dry-run primeiro)
python scripts\intel.py mail-link --realm Dragonmaw --dry-run
python scripts\intel.py mail-link --realm Dragonmaw

# 7. quem está a cortar os teus preços
python scripts\intel.py undercut --realm Dragonmaw --days 30
```

Se mudaste o addon DFA_AuctionIntelligence no repo, espelha-o antes do `/reload`:

```powershell
$src="C:\FarmAnalysis\DFA_AuctionIntelligence"; $dst="C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns\DFA_AuctionIntelligence"; robocopy $src $dst /MIR /NFL /NDL /NJH /NJS /NP
```

## 2. Sempre que vou postar

Checklist in-game, por esta ordem:

1. `/dfaai status` — confirmar antes de confiar nos dados:
   - `auctionator hook=true` e `open-all hooked=true`;
   - `Mail invoices` > 0 (se abriste correio nesta sessão);
   - `Undercut observations` > 0 (se já correste um cancel scan).
2. `/dfaai inventory` — fotografar o stock/`posted` antes de postar. É esta imagem que
   o reconciliador compara com a próxima.
3. Preço: usar a estimativa DfA, nunca `dbmarket`/`dbregionmarketavg` como preço final.
   O relatório do ML mostra que o teu listing mediano pede **4,06x** a média regional de
   venda e que a faixa com melhor gold esperado por listing é **1,0x–1,15x** essa média
   (até 1,4x ainda vende); acima de 3x a taxa de venda cai para 2,2%.
   Regra prática: se tiveres de escolher à mão, fica **≤1,4x** a média regional de venda.
4. Postar normalmente (Auctionator/TSM). O addon regista o posting pelo Journalator; não
   há nada para escrever à mão.
5. Não abras mail nenhum para "registar" a venda: o próximo logout trata disso.

## 3. Sempre que faço cancel scan (undercut scan)

Há **dois** caminhos, e o nosso já não depende do Auctionator:

1. **`/dfaai undercut` (recomendado, funciona com a UI da Blizzard).** Abre o AH,
   corre o comando e deixa o AH aberto até terminar. O addon pede a lista dos teus
   leilões (`C_AuctionHouse.QueryOwnedAuctions`) e uma pesquisa por item; de cada
   resultado lê `owners` — o vendedor que a UI da Blizzard não mostra. Uma query por
   item, com timeout de 6 s, e o resumo no fim (`read`, `skipped`, `no results`,
   `timed out`). O scan ocupa a janela de resultados do AH enquanto corre.
2. **Scan do Auctionator (passivo).** Se preferires o cancel scan do Auctionator, o
   addon lê os resultados que ele já pediu, sem uma query extra. Não é preciso
   correr os dois.

Em qualquer dos casos:

1. In-game: `/dfaai undercut` (ou o botão **Undercut scan** do HUD) — se já houver
   scan a correr, mostra o progresso.
2. Antes de sair: `/dfaai export` (o logout também grava). O HUD faz o mesmo.
3. Python:

```powershell
python scripts\intel.py addon-import
python scripts\intel.py undercut --realm Dragonmaw --days 30 --limit 20
python scripts\intel.py relist --realm Dragonmaw --days 14
```

Como ler o resultado:

- `sellers[].observations` — quantas vezes aquele seller apareceu abaixo de ti;
- `avg_undercut_pct` / `worst_example` — agressividade e pior caso;
- `items[].sellers` — quem está em cada item e com que frequência;
- `items_ahead` — quantas unidades estão à frente do teu leilão.

Isto é um facto de **preço**, não uma venda: o listing mais barato pode nunca ter vendido.
O alcance são os teus leilões com buyout e sem licitações; commodities não têm seller e
ficam de fora (no scan do Auctionator valem também os filtros dele).

`relist` responde à pergunta seguinte: **cancelar e repor, manter, ou deixar expirar?**

- gap ≤2% e poucas unidades à frente → `hold` (é a tua vez a seguir);
- gap ≥10% ou fila > 2× o teu stack e ≥4h por acabar → `cancel_and_relist`, com o ask
  sugerido 1 cobre abaixo do mais barato, **só com uma referência verificada**
  (scan in-game com `itemKey` e seller);
- menos de 4h por acabar → `let_expire` (não dá tempo de recuperar o novo depósito);
- entre 4h e 12h → `watch`;
- referência `price-only` da API, `variant-ambiguous`, velha ou sem preço
  utilizável → `watch` com `review_required` (vê o item no AH ou corre um scan
  in-game); nunca inventa um ask.

O depósito é tratado como perdido nos dois casos (cancelar e expirar perdem-no; só a
venda o devolve), por isso a decisão é sobre tempo e posição na fila. Isto é análise:
não cancela, não repõe, não faz undercut.

## 4. Sempre que abro o leilão, o banco ou o banco de guilda

- **Auctions:** ao abrir o AH (ou ao correr o cancel scan), o `OWNED_AUCTIONS_UPDATED`
  faz um snapshot com `posted` legível. No login `posted` é desconhecido — não é zero.
- **Banco de guilda:** ao abrir (`GUILDBANKFRAME_OPENED` / `GUILDBANK_UPDATE_TABS`), as
  abas visíveis são lidas para `guildbank`. É armazenamento partilhado: fica gravado,
  mas **nunca** entra na reconciliação do teu stock nem em `current_units`.
- Verificação: `/dfaai inventory` mostra `guild=` e `Readable: ... guild=true/false`.

## 5. Sempre que abro o correio / carrego em Open All

1. Usa o **Open All da Blizzard UI**. O botão está hookado e força um scan das invoices
   antes de recolher anexos; as mails não precisam de ser abertas uma a uma.
2. O TSM também tem Open All, mas **não é preciso**: o nosso scan não depende de abrir
   mail, e o `MAIL_SHOW`/`MAIL_INBOX_UPDATE` já captura tudo. Ignora o do TSM.
3. `/dfaai status` — `Mail invoices` deve subir, `open-all hooked=true`.
4. Depois de sair:

```powershell
python scripts\intel.py addon-import         # mail.parsed / mail.inserted / mail.link.linked
python scripts\intel.py mail-link --realm Dragonmaw --dry-run
```

Se o import disser `no mail_jsonl block`, o jogo ainda tem o addon antigo carregado:
`/reload` e repetir. As invoices vivem ~30 dias; vendas cujo mail já expirou não
recuperam comprador.

## 6. Antes de sair do jogo

```
/dfaai inventory     # última fotografia: bags, banco, warband, posted
/dfaai export        # JSONL atualizado (o logout também o faz)
/dfaai hud           # botões: Snapshot, Undercut scan, Mail, Export
```

O HUD faz a mesma coisa sem escrever comandos; cada pressão fica na fila
(`/dfaai actions`) e o daemon corre o seguimento depois do `/reload`.

Sair normalmente (não matar o processo): é no logout que as SavedVariables são escritas.

## 7. Semanal

```powershell
# dinâmica do reino (undercuts, liquidez, tempo de venda)
python scripts\intel.py diagnose --realm Dragonmaw --days 14

# sellers observados: factos vs inferências
python scripts\intel.py sellers --realm Dragonmaw --days 30

# modelo por reino (só treina com >=40 vendas; os outros ficam insufficient_data)
python scripts\intel.py realm-ml --min-rows 40 --iterations 60

# farms em retorno realizável
python scripts\intel.py farms --realm Dragonmaw --kpm 20 woweconomy\wowhead_zones\zone_16.json

# experiência registada com cadeia de hashes
python scripts\intel.py ledger-verify data\experiments\ledger.jsonl
```

## 8. Ordem resumida (copiar e colar)

```powershell
# o daemon faz tudo isto sozinho; abaixo é o equivalente manual
python scripts\watch.py --once --verbose

# ou, passo a passo
python scripts\sync_market.py --check
python scripts\dfa_ml.py train --refresh-journal
python scripts\dfa_pricing_export.py --realm-model --all-realms
python scripts\intel.py addon-import
python scripts\intel.py mail-link --realm Dragonmaw
python scripts\intel.py undercut --realm Dragonmaw
python scripts\intel.py blizzard-undercut --realm Dragonmaw
```

## 9. Saúde dos dados (o que olhar)

| onde | saudável | o que fazer se não |
|---|---|---|
| `addon-import` → `mail` | `parsed`/`inserted`, sem `error` | `/reload` com o addon 1.3.0; abrir correio |
| `addon-import` → `undercut` | sem `error` | `/dfaai undercut` com o AH aberto |
| `/dfaai undercut` | `read` sobe, `timed out` 0 | se `timed out` subir, correr outra vez com menos leilões ou usar o scan do Auctionator |
| `addon-import` → `inventory` | `available` inclui `posted` | abrir o AH uma vez (owned auctions) |
| `/dfaai status` | `warband` e `posted listings` com valores | abrir banco e AH com o addon 1.4.1 |
| `blizzard-undercut` | `posted_listings` > 0 e `inserted` | abrir o AH uma vez para exportar os preços dos teus leilões |
| `watch.log` | um tick por mudança, sem `error` | ver `data/watch_state.json` e a última linha |
| `/dfaai actions` | as últimas pressões com `ok`/`FAIL` | um `FAIL` diz o motivo (ex.: AH fechado) |
| `data/intel.sqlite` → `ah_snapshot` | uma entrada por hora, por reino | se ficar parado, o `ah-fetch` falhou (credenciais/429) |
| reconciliação | `unexplained_decrease_units` 0 | ver a nota: AH fechado deixa `posted` desconhecido, não vazio |
| `mail-link` | `linked` sobe, `ambiguous_*` pequeno | mail já usada noutra venda é ignorada de propósito |
| `undercut` | `observations` > 0 | `/dfaai status` → `auctionator hook`; ou `/dfaai undercut` |
| `relist` | `listings` > 0, `hold`/`cancel_and_relist` explicados | sem observações, correr o scan primeiro |
| `dfa_ml_report.md` | test window RMSE log ~0,66 | sem vendas novas, o número não muda |

## 10. Limites que não se contornam com comandos

- Fora do jogo, a API da Blizzard dá preço e fila, **não dá seller**. O nome do
  concorrente só existe num scan in-game (`/dfaai undercut`).
- O `babysit24h` só aparece depois de dois snapshots da API (1 h+ de daemon); o
  `undercutShare` só depois de um scan in-game. Sem medição, o tooltip omite a linha.

- A janela de treino actual é de ~5 dias; mais histórico melhora tudo, e ainda não existe.
- O tempo até à venda **não é previsível** com estes dados (concordância 0,403, abaixo do
  acaso): o motor não dá esse número de propósito.
- O horizonte não pode exceder a duração do leilão: 7d/14d é a mesma probabilidade com
  mais tempo em que o leilão já acabou.
- Sem commodities, os itens de material ficam sem concorrência observada.

O estado do ML comparado com o roadmap, com os números medidos e o que os dados
ainda não suportam, está em [roadmap.md](roadmap.md).

## Sentinel validado (2026-10-21)

O motor antigo (`dfa_ml.py train`) continua a fazer a validação walk-forward
do CatBoost. A camada que o jogo consome é o **Sentinel**, treinado por:

```powershell
python scripts/dfa_ml.py sentinel --if-needed
python scripts/dfa_pricing_export.py
```

O primeiro comando só refita quando há labels novas suficientes ou o
artefacto está stale; o segundo reconstrói o dataset e valida o schema 2 antes
de escrever. O `watch.py` corre o primeiro a cada 24h e o segundo quando o
artefacto é promovido.

O que verificar depois de um treino:

1. `reports/dfa_sentinel/sentinel_report.json` → `targets` tem `status` e
   `metrics`; `time_to_sale` deve continuar `unsupported_signal` enquanto o
   C-index for < 0,60.
2. `models/dfa_sentinel/CURRENT` aponta para a versão nova; o directório
   antigo continua lá até ao limite de retenção.
3. `DFA_Estimate/Data/DFA_Estimate_Data.lua` tem `sentinelModel` e
   `psellByHorizon`; `ereal`/`undercut`/`decline` podem ser `nil` quando o
   target não existe — `nil` não é zero.
4. Copiar `DFA_Estimate` e `DFA_AuctionIntelligence` para
   `Interface/AddOns/` (o `watch.py` já copia o dataset; os `.lua` são
   copiados manualmente ou pelo instalador).
5. No jogo, `/reload`; `/dfaai status` deve mostrar a versão e não deve
   listar eventos saltados. O painel `/dfaestimate` deve abrir; se não abrir,
   ver `DFA_Estimate.panelError`.

O detalhe dos números, limitações e do bug de carregamento do
`DFA_AuctionIntelligence` está em
[reports/dfa_sentinel_ml.md](reports/dfa_sentinel_ml.md).

## Intervalos calibrados no tooltip (21 Oct 2026)

O tooltip deixou de mostrar um preço único. Cada preço passa a ter um
intervalo **split-conformal a 80%**, calibrado num bloco cronológico e
medido num bloco posterior. O addon imprime a largura e a cobertura lado a
lado:

```text
DFA estimate: 1.57g
  80% range: 0.83g - 2.99g (77% of 103 sales)
Expected realized: 1.57g
  80% range: 0.73g - 3.37g (85% of 119 sales)
```

Números do dataset actual: modelo de registo ±0,641 log / 77,7% de 103
vendas; modelo shipped ±0,751 log / 84,7% de 124 vendas; Sentinel
realizado ±0,764 log / 85,7% de 119 vendas. Um campo ausente fica `nil` e o
tooltip mostra só o ponto — nunca um intervalo de largura zero.
