# DFA Sentinel — a lógica toda, num sítio

Este relatório descreve o que passa a correr sozinho, porquê, e o que cada
número afirma (e não afirma). A regra do projeto mantém-se: **um facto
observado não é uma venda, e um número em falta não é zero.**

## 1. O problema que isto resolve

O pipeline tinha sete comandos que só corriam quando alguém se lembrava:
importar o addon, importar o Journalator, sincronizar o TSM, treinar por
reino, exportar o dataset, ler os undercuts e decidir o que repostar. Cada
esquecimento custa dados: um `posted` que nunca foi lido é uma venda que
nunca é reconciliada, e um undercut que ninguém viu é um anúncio que expira.

O `scripts/watch.py` (DFA Sentinel) troca "correr tudo" por **correr o que
mudou**. Não é um cron de comandos: cada fonte tem uma implicação, e só essa
corre.

## 2. Fontes e o que cada mudança implica

| Fonte | Como é detectada | Tarefa | Porquê |
|---|---|---|---|
| SavedVariables do addon | mtime + tamanho, depois de 30 s estável | `intel.py addon-import` | eventos, inventário, mails, sellers e undercuts |
| Journalator | mtime + tamanho | `journal.py import` | vendas reais (o único facto de venda) |
| TSM `AppData.lua` | mtime + tamanho | `sync_market.py --quiet --no-csv` | referências de mercado por reino |
| CSV públicos | mtime, opcional (`--with-csv`) | `fetch_csv.py` | catálogo/regional, 1×/dia |
| API Blizzard (AH) | relógio, 1×/hora | `intel.py ah-fetch` | retrato completo do AH, sem jogo |
| Journalator novo e ≥6 h desde o último fit | consequência | `intel.py realm-ml` | um modelo por reino |
| ≥24 h desde o último Sentinel | relógio, com `--if-needed` | `dfa_ml.py sentinel --if-needed` | P(venda), preço realizado e risco calibrados; só refita com labels novas suficientes |
| Journal/TSM/training/Sentinel mudaram | consequência, coalescida: no máximo 1×/30 min (`--export-min-gap-min`) | `dfa_pricing_export.py --realm-model --all-realms` | o dataset que o jogo lê |

O ficheiro de SavedVariables é escrito de uma vez no `/reload`/logout, mas
um cliente aberto pode estar a escrevê-lo quando o daemon olha. Por isso a
assinatura só conta como mudança depois de 30 s sem alterações
(`--stable-s`): ler metade de uma tabela Lua é um erro, não é dados.

O estado (assinaturas, último treino, último fetch, cooldowns de alertas)
fica em `data/watch_state.json`. O log em `logs/watch.log`.

O export é a etapa pesada (minutos) e o TSM escreve o `AppData.lua` de poucos
em poucos minutos, por isso ele é coalescido: uma mudança dentro da janela de
30 min não dispara outro export, fica em fila (`export_pending`) e corre no
primeiro tick depois da janela, mesmo sem uma mudança nova. Um export que
falha mantém a fila em vez de deixar o dataset parado até o fallback de 6 h.

O deployment (`woweconomy/pricing/deploy.py`) leva o dataset para **duas**
cópias, ambas vindas da configuração e nunca de uma pesquisa de diretórios:
`PricingConfig.dataset_path` (cópia canónica do projeto) e
`PricingConfig.addons_dir` / `--addons-dir` (o `Interface/AddOns` do jogo).
Só escreve quando os bytes diferem, valida o ficheiro antes de o aceitar,
escreve de forma atómica (temporário + `os.replace`) e guarda a cópia
substituída como `*.lua.bak`, mas só quando essa cópia também valida. Uma
pasta que não exista é reportada como `missing`, não é criada; o `.toc` e o
resto do addon nunca são tocados. Uma escrita presa (o jogo a ler) é
retentada; se um destino continuar a falhar, o deployment é `ok=false`.
Se um export novo aterrar a meio da cópia, o passe é repetido com os bytes
novos; se nunca estabilizar, o resultado diz `superseded` e o
`export_pending` volta a ficar em fila. Como o ficheiro canónico também é
uma fonte vigiada, um export corrido à mão chega ao jogo no tick seguinte.

### O que não corre sempre

- O **export** refita o modelo que o jogo corre e demora ~3–4 min. Corre
  quando o journal ou as referências mudam, ou de 6 em 6 horas — não a cada
  tick, e nunca por uma alteração só de inventário (não muda um preço).
- O **treino** (realm-ml) corre no máximo de 6 em 6 horas, e só quando há
  linhas novas. O **Sentinel** corre a cada 24h, mas o próprio comando
  recusa o fit quando não há labels confirmadas novas suficientes.
- O **AH** é horário porque a Blizzard publica o retrato de hora a hora; o
  `If-Modified-Since` evita reingerir o mesmo payload (304 → nada a fazer).

## 3. Undercut sem o jogo: o que a API pode e não pode dizer

A API pública devolve todos os leilões de um connected realm. **Não tem
seller.** Tem item, quantidade, preço unitário, `time_left` em categoria e
o id do leilão.

O addon agora exporta, por cada leilão postado, `auctionId`, `itemId`,
`quantity`, `unitPriceCopper` e `timeLeft` (`postedListings`). Com essas
duas metades, `woweconomy/intel/ah_undercut.py` responde, sem abrir o jogo:

- qual é o preço mais baixo **de outro leilão** para o item;
- quantas unidades estão à frente do meu preço;
- qual é o gap em copper e em percentagem;
- se o meu leilão ainda lá está (pelo id).

O que fica vazio, de propósito: `lowest_seller`. O resumo mostra
`(seller not in API)`. Inventar um nome envenenaria os perfis de
concorrente que o scan in-game constrói.

Uma observação derivada da API tem `source = blizzard_api` e
`confidence = price-only`. A do addon continua `verified`, com nome.
O `reference_status` distingue ainda `variant-ambiguous`: o listing mais
barato tem `item_context`, `bonus_lists`, `modifiers` ou espécie de pet, e o
nosso export de `posted` só guarda o `item_id` — não há como provar que é o
mesmo item level/suffix/variante. `price-only` e `variant-ambiguous` nunca
produzem `cancel_and_relist`; aparecem como **review**.

### O relógio em falta não expira nada

A API só diz `SHORT`/`MEDIUM`/`LONG`/`VERY_LONG`. Converter a categoria num
ponto médio seria inventar precisão. Quando o addon não trouxe segundos,
`time_left` fica 0 e o `relist` recebe `hours_left = None`: a regra "já é
tarde, deixa expirar" **não dispara**. Numa referência verificada, um gap
grande continua a justificar `cancel_and_relist`; um gap pequeno fica
`watch`. Numa referência `price-only`/`variant-ambiguous`, fica sempre
`watch`/review.

### Reset de mercado

Comparando dois snapshots consecutivos, um item cujo **preço mais baixo
subiu ≥30 %** e cuja **oferta caiu ≥40 %** (com pelo menos 2 listings em cada
lado) é assinalado como possível reset: buy-out e reposição, não trading
normal. Uma venda baixa a oferta sem subir o preço; um listing novo baixa o
preço. O primeiro par de snapshots do Dragonmaw já produziu um:
item 193371, 444 g → 999 g (+125 %), oferta 4 → 2 (−50 %).

O alerta diz "possible market reset", com o item e os dois números. Não
afirma quem o fez.

## 4. Cancelar e repor, ou deixar expirar

`woweconomy/intel/relist.py` aplica regras explícitas:

| Condição | Conselho |
|---|---|
| ≤4 h restantes | `let_expire` — não há tempo para o novo depósito se pagar |
| gap ≤2 % e fila curta | `hold` — já estás competitivo; cancelar só reinicia a fila |
| gap ≥10 % ou fila > 2× o meu stack, referência verificada | `cancel_and_relist`, ask = 1 copper abaixo do mais baixo |
| 4–12 h e gap médio | `watch` — repricar agora não se consegue provar |
| tempo restante desconhecido, referência verificada | `watch`/`cancel_and_relist` conforme o gap, nunca `let_expire` |
| `price-only`, `variant-ambiguous`, stale ou preço inválido | `watch` com `review_required`; sem ask e sem `cancel_and_relist` |

O depósito é **sunk**: cancelar e expirar perdem-no; só a venda o devolve.
Por isso a decisão é sobre tempo e posição na fila, não sobre a taxa. Nada
é cancelado nem reposto automaticamente.

## 5. Preço para todos os reinos

Antes: registos só para os 8 reinos onde a conta vendeu, embora 267 reinos
existam. O dataset agora tem três camadas:

1. **`prices`** — o preço regional do modelo para 31 184 itens (como antes);
2. **`realmIndex`** — uma linha por reino: `factor`, `sample`, `source`,
   `undercutShare`, `babysit24h`;
3. **`realms`** — os registos por reino, só onde há vendas próprias.

O factor é a mediana de `log(preço observado no reino / preço regional)`,
encolhida para 1 com um prior de 10 observações: um reino com 4 vendas não
declara um mercado 3× maior. Silvermoon ficou em 0,79 com 7 itens;
Dragonmaw em 1,13 com 174; Auchindoun em 1,22 com 17.

No jogo, `EstimateForRealm(item, realm)` usa o registo do reino se existir,
senão `preço regional × factor`, com a confiança reduzida pela amostra. O
`BestRealm(item)` ordena os 267 reinos por
`preço × (1 − 0,5 × undercutShare)` — é um ranking de preço e competição,
**não** uma probabilidade de venda. Não há modelo de venda por reino, e
inventar um seria um número mais bonito sem evidência.

## 6. "Babysit probability": o que é medido

Há duas medidas diferentes, e o tooltip não as confunde:

- **`undercutShare`** — dos meus leilões que o addon viu, quantos tinham
  alguém mais barato. O denominador é a lista de postados lida no AH (não a
  tabela de observações, onde tudo é undercut por construção). Sem essa
  lista, fica `None`, não 100 %.
- **`babysit24h`** — a probabilidade de o preço mais baixo descer nas
  próximas 24 h, estimada pela taxa de descidas por item-hora entre dois
  snapshots da API (`1 − exp(−taxa × 24)`). Com um snapshot só, é `None`.

O tooltip mostra `Babysit risk here: N% in the next 24h` quando há risco
medido, ou `Babysit here: N% of listings undercut (n=…)` quando só há a
partilha. Não medido é omitido, não é escrito como 0 %.

Isto responde ao caso do Silvermoon: se quase todos os teus leilões de lá
aparecerem sob alguém, a partilha sobe e o `BestRealm` desconta o preço
antes de te mandar para lá. Hoje ainda não há scans de Silvermoon, por isso
a linha não aparece — o primeiro `/dfaai undercut` lá preenche-a.

## 7. HUD e a fila de acções

O addon 1.4.1 tem um HUD (`/dfaai hud`) com quatro botões: **Snapshot**,
**Undercut scan**, **Mail**, **Export**. Cada pressão:

1. corre a acção e guarda `{name, at, ok, detail}` em
   `DFA_AUCTION_INTELLIGENCE_DB.actions` (últimas 100);
2. reescreve o export, incluindo `actions_jsonl`;
3. no `/reload`/logout o cliente grava o ficheiro;
4. o daemon importa, compara com `actions_seen` e **só corre o seguimento
   das novas**: um scan de undercut traz o conselho de `relist` e o alerta;
   um snapshot traz a reconciliação.

O HUD é construído dentro de `pcall`: um cliente com templates diferentes
perde a moldura, não perde a fila. O harness Lua corre as acções sem UI.

## 8. Alertas

Dois canais, uma regra: um alerta é uma **mudança**, não um batimento.

- **Desktop**: balão do Windows via `System.Windows.Forms.NotifyIcon`, sem
  módulo extra e sem administrador.
- **Discord**: webhook lido de `DFA_DISCORD_WEBHOOK` ou do `DISCORD_WEBHOOK`
  que o repo já usa no `.env` (o primeiro ganha), nunca escrito no log nem no
  estado. `redact()` mostra o URL sem token. O POST leva
  `User-Agent: DFA-Sentinel/1.0`: o Cloudflare à frente do Discord responde
  **403** ao agent por omissão do `urllib`, que parece um webhook inválido.
  Um post de teste ao webhook real devolveu 204.

Chaves e cooldown de 6 h: `undercut:<realm>:<item>`, `reset:<realm>:<item>`,
`inventory:<realm>`, `error:<tarefa>`, e `undercut-review:<realm>` para o
digest de referências que precisam de revisão. O mesmo undercut visto de hora
a hora é uma mensagem; um agravamento muda a chave porque a acção muda.

Falhas de canal são registadas e devolvidas, nunca lançadas: um webhook morto
não para o pipeline.

## 9. Inventário: de onde vem

- **bags** e **bank** (character) — lidos no login/logout e ao abrir o banco;
- **warband** — abas compradas (`C_Bank.FetchPurchasedBankTabIDs(Account)`,
  com fallback para os containers 13–17). É lido e guardado **fora** da
  reconciliação: um alt a mexer não é um movimento teu;
- **posted** — agregado + `postedListings` com preço, quando o cliente
  carregou a lista de leilões (`OWNED_AUCTIONS_UPDATED`);
- **guildbank** — só com o frame aberto e a aba visível; partilhado, nunca
  reconciliado.

O banco de personagem passou a usar as abas compradas de 11.2 em vez da
gama fixa 5–11 (que apanhava a reagent bag e falhava as abas novas). Sem
abas compradas, a gama antiga é o fallback e uma leitura falhada continua
`unknown`, não `0`.

## 10. Como correr

```bat
REM ciclo contínuo (deixa a janela aberta, ou usa o Task Scheduler)
python scripts\watch.py

REM uma passagem, para ver o plano e o resultado
python scripts\watch.py --once --verbose

REM sem rede (só ficheiros): útil quando o TSM/Blizzard estão lentos
python scripts\watch.py --once --offline

REM só os reinos que interessam, e sem notificações
python scripts\watch.py --realms Dragonmaw,Silvermoon --no-notify
```

Webhook:

```bat
setx DFA_DISCORD_WEBHOOK "https://discord.com/api/webhooks/..."
```

(`setx` só afecta processos novos; para já, `set` na sessão.)

Arranque automático no login (Task Scheduler, sem administrador):

```bat
schtasks /Create /SC ONLOGON /TN "DFA Sentinel" /TR "pythonw C:\FarmAnalysis\scripts\watch.py"
```

O daemon copia o dataset gerado para
`Interface\AddOns\DFA_Estimate\Data\` quando a pasta existe, para o jogo não
ficar com uma versão antiga.

## 11. Verificação feita

- `python -m pytest tests -q` → **678 passed**.
- `python scripts/intel_addon_harness.py` → **66/66 checks** num Lua real:
  posted listings com preço, banco por abas, warband separado, HUD, fila e
  `actions_jsonl`.
- `watch.py --once --offline` → import, journal, sync, treino e export
  executados; dataset instalado no jogo.
- `watch.py --once --realms Dragonmaw` → fetch da API (19 402 leilões),
  detecção de reset real, export; falha encontrada e corrigida
  (`item_name` não existe em `ah_auction`).
- `dfa_pricing_export.py --realm-model` → 267 reinos indexados, 5 com vendas
  próprias, dataset de ~696 KB validado (schema 2, 237 registos, 31 184
  preços).
- Painel do `DFA_Estimate`: `SetScrollChild` recebia uma font string e
  rebentava com "Wrong object type"; agora recebe um frame de conteúdo, com
  teste em Lua (`tests/test_dfa_estimate_panel.py`).

## 12. Limites, ditos por inteiro

- O seller continua a não existir na API: fora do jogo só há preço e fila.
- O `undercutShare` só aparece depois de um scan in-game, e o
  `babysit24h` só depois de dois snapshots da API (1 h+ de daemon).
- O factor por reino é uma mediana grosseira; 259 reinos estão a 1,00 por
  falta de vendas próprias. O ranking entre eles fica empatado — e diz isso,
  em vez de escolher um ao acaso.
- O export demora minutos; não é adequado a cada tick.
- O HUD não foi visto a correr num cliente real (o jogo não estava aberto
  quando foi testado); o Lua foi exercitado no harness com o mesmo código.
- Nada disto compra, vende, cancela ou reposta. O máximo que faz é dizer o
  que olhar primeiro.
