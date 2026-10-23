# Limitações Conhecidas e Requisitos de Logging Adicional

## 1. Limitações Conhecidas

1. **Associação Heurística entre Publicação e Venda (FIFO Matching)**:
   - A API da Blizzard não devolve um `auctionId` persistente na factura de correio que case com o anúncio colocado.
   - Quando um jogador publica múltiplas unidades do mesmo item em momentos diferentes no mesmo reino, a atribuição de qual anúncio vendeu é resolvida por ordem cronológica (FIFO) dentro de uma janela de 72 horas.
   - Para itens com rotação rápida de múltiplos stacks, o tempo até à venda deve ser interpretado com intervalo de tolerância.

2. **Invisibilidade das Transacções Alheias (Restrição do Cliente)**:
   - O cliente de World of Warcraft apenas recebe notificações de transacções onde a personagem do jogador é o comprador ou o vendedor.
   - A liquidez do mercado geral continua a ser estimada por snapshots periódicos da casa de leilões (`tsm_market` e `tsm_snapshot`) observando o desaparecimento de listings entre snapshots.

3. **Custos de Aquisição de Itens Farmados**:
   - Para itens obtidos por saque em masmorras ou mundo aberto, o custo financeiro directo de aquisição é zero em cobre, mas envolve tempo de jogo.
   - Para itens revendidos (flipping), o custo de aquisição só é capturado se a compra ocorreu na mesma máquina com o monitor ligado.

## 2. Requisitos de Logging Adicional Recomendados

1. **Captura de Preço Concorrente no Momento da Publicação**:
   - Quando o jogador clica para publicar um item, registar adicionalmente:
     - Preço mais baixo concorrente visível no painel da AH no momento do clique.
     - Operação activa do TSM (ex.: `Mogs Dragonmaw Normal`, `Token Rush 12d`).
     - Decisão de pricing (se foi undercut de 1c ou reset para o preço normal).
   - Implementação: Já estruturado em `AuctionEvents.lua` via `marketRefCopper` e `raw.operation`.

2. **Registo de Depósitos Fornecedor / Base**:
   - Guardar o valor de venda ao vendedor (`vendor_sell`) no payload do anúncio para calcular a taxa de queima percentual do depósito base.
