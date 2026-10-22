# Token Rush — grupos TSM por reino

Gerado por `python scripts/token_rush_profiles.py --write`. **Nada foi importado**: cada ficheiro é uma proposta para um perfil TSM **desse** reino, criado à mão (ver §4).

## 1. O que há em cada export

- **Os mesmos grupos e os mesmos itens do export mog** (`tsm_groups_export.py --all-realms`): os 127 155 itens de transmog nos sete grupos de comportamento, com o `conflict` no grupo próprio. Um drop novo de transmog cai logo num grupo com preço.
- **O preço é o do motor de evidência** (`pricing_engine`), escrito como fórmula viva: `ifgt(dbmarket, banda, r × dbmarket, …)`, com `r` a **razão medida** (preço recomendado ÷ mercado local) dos itens desse grupo e banda. O preço acompanha o mercado local; a calibragem vem do motor, não de uma constante.
- **A operação é de rush**: 12 h de duração, 7 dias de retenção (`maxExpires` 13), `priceReset = none`, `aboveMax = maxPrice`, sem cancelamento automático.
- O piso é `max(razão × dbmarket, 1.5 × vendorsell)`: nunca abaixo do que o fornecedor paga.

> **Atenção operacional:** com concorrência, o TSM posta `parede − undercut` limitado por `[minPrice, maxPrice]`, ou seja, quem decide o preço é a **parede** (o anúncio mais barato do reino), não a nossa fórmula. A fórmula só entra quando **não há** concorrência — e nesse caso a razão medida pode estar muito abaixo da parede, porque o ask médio é inflacionado. Aí estamos a vender abaixo do mercado visível, de propósito (é uma liquidação), mas vale a pena confirmar item a item no *Price Tester* antes de deixar o TSM postar sozinho.

## 2. Os reinos

| reino | cluster | perfil TSM | grupos | itens | com preço | ficheiro |
|---|---|---|---:|---:|---:|---|
| Aggra (Português) | Grim Batol | `mogs-grim-batol` | 7 | 127,155 | 15,000 | `token_rush_grim-batol.tsm` |
| Antonidas | Antonidas | `mogs-antonidas` | 7 | 127,155 | 17,200 | `token_rush_antonidas.tsm` |
| Argent Dawn | Argent Dawn | `mogs-argent-dawn` | 7 | 127,155 | 18,339 | `token_rush_argent-dawn.tsm` |
| Auchindoun | Auchindoun | `mogs-auchindoun` | 7 | 127,155 | 16,265 | `token_rush_auchindoun.tsm` |
| Blackrock | Blackrock | `mogs-blackrock` | 7 | 127,155 | 16,816 | `token_rush_blackrock.tsm` |
| Draenor | Draenor | `mogs-draenor` | 7 | 127,155 | 18,286 | `token_rush_draenor.tsm` |
| Dragonmaw | Spinebreaker | `mogs-spinebreaker` | 7 | 127,155 | 13,373 | `token_rush_spinebreaker.tsm` |
| Kazzak | Kazzak | `mogs-kazzak` | 7 | 127,155 | 17,755 | `token_rush_kazzak.tsm` |
| Silvermoon | Silvermoon | `mogs-silvermoon` | 7 | 127,155 | 18,526 | `token_rush_silvermoon.tsm` |
| Twisting Nether | Twisting Nether | `mogs-twisting-nether` | 7 | 127,155 | 17,422 | `token_rush_twisting-nether.tsm` |

## 3. Os grupos e as suas razões

| reino | grupo TSM | itens | com preço | razão (preço/ask local) | preço mediano | liquidez |
|---|---|---:|---:|---:|---:|---|
| Grim Batol | `Liquid` | 351 | 188 | 0.181 | 160 g | baixa, desconhecida, média |
| Grim Batol | `Collector` | 7,639 | 2,794 | 0.028 | 264 g | baixa, desconhecida, média |
| Grim Batol | `Slow` | 5,977 | 2,342 | 0.042 | 47 g | baixa, desconhecida |
| Grim Batol | `Very_Slow` | 9,865 | 2,679 | 0.026 | 75 g | baixa, desconhecida |
| Grim Batol | `Unproven` | 5,864 | 1,069 | 0.025 | 95 g | baixa, desconhecida |
| Grim Batol | `Unknown` | 89,249 | 3,135 | 0.056 | 356 g | baixa, desconhecida |
| Grim Batol | `Conflict` | 8,210 | 2,793 | 0.040 | 72 g | baixa, desconhecida, média |
| Antonidas | `Liquid` | 351 | 193 | 0.303 | 166 g | baixa, desconhecida, média |
| Antonidas | `Collector` | 7,639 | 3,263 | 0.048 | 278 g | baixa, desconhecida, média |
| Antonidas | `Slow` | 5,977 | 2,641 | 0.067 | 48 g | baixa, desconhecida |
| Antonidas | `Very_Slow` | 9,865 | 3,132 | 0.032 | 79 g | baixa, desconhecida |
| Antonidas | `Unproven` | 5,864 | 1,374 | 0.023 | 90 g | baixa, desconhecida |
| Antonidas | `Unknown` | 89,249 | 3,564 | 0.092 | 451 g | baixa, desconhecida |
| Antonidas | `Conflict` | 8,210 | 3,033 | 0.071 | 73 g | baixa, desconhecida, média |
| Argent Dawn | `Liquid` | 351 | 192 | 0.426 | 166 g | baixa, desconhecida, média |
| Argent Dawn | `Collector` | 7,639 | 3,419 | 0.195 | 285 g | baixa, desconhecida, média |
| Argent Dawn | `Slow` | 5,977 | 2,712 | 0.291 | 48 g | baixa, desconhecida, média |
| Argent Dawn | `Very_Slow` | 9,865 | 3,343 | 0.164 | 79 g | baixa, desconhecida, média |
| Argent Dawn | `Unproven` | 5,864 | 1,482 | 0.060 | 87 g | baixa, desconhecida |
| Argent Dawn | `Unknown` | 89,249 | 4,026 | 0.163 | 451 g | baixa, desconhecida |
| Argent Dawn | `Conflict` | 8,210 | 3,165 | 0.274 | 74 g | baixa, desconhecida, média |
| Auchindoun | `Liquid` | 351 | 191 | 0.251 | 166 g | baixa, desconhecida, média |
| Auchindoun | `Collector` | 7,639 | 3,111 | 0.034 | 282 g | baixa, desconhecida, média |
| Auchindoun | `Slow` | 5,977 | 2,508 | 0.050 | 48 g | baixa, desconhecida, média |
| Auchindoun | `Very_Slow` | 9,865 | 2,943 | 0.027 | 79 g | baixa, desconhecida, média |
| Auchindoun | `Unproven` | 5,864 | 1,205 | 0.021 | 95 g | baixa, desconhecida |
| Auchindoun | `Unknown` | 89,249 | 3,378 | 0.073 | 404 g | baixa, desconhecida |
| Auchindoun | `Conflict` | 8,210 | 2,929 | 0.050 | 75 g | baixa, desconhecida, média |
| Blackrock | `Liquid` | 351 | 192 | 0.195 | 166 g | baixa, desconhecida, média |
| Blackrock | `Collector` | 7,639 | 3,251 | 0.028 | 272 g | baixa, desconhecida, média |
| Blackrock | `Slow` | 5,977 | 2,601 | 0.051 | 48 g | baixa, desconhecida, média |
| Blackrock | `Very_Slow` | 9,865 | 3,115 | 0.024 | 78 g | baixa, desconhecida |
| Blackrock | `Unproven` | 5,864 | 1,290 | 0.020 | 95 g | baixa, desconhecida |
| Blackrock | `Unknown` | 89,249 | 3,341 | 0.061 | 474 g | baixa, desconhecida |
| Blackrock | `Conflict` | 8,210 | 3,026 | 0.046 | 72 g | baixa, desconhecida, média |
| Draenor | `Liquid` | 351 | 194 | 0.551 | 166 g | baixa, desconhecida, média |
| Draenor | `Collector` | 7,639 | 3,412 | 0.146 | 285 g | baixa, desconhecida, média |
| Draenor | `Slow` | 5,977 | 2,705 | 0.194 | 48 g | baixa, desconhecida, média |
| Draenor | `Very_Slow` | 9,865 | 3,322 | 0.097 | 79 g | baixa, desconhecida, média |
| Draenor | `Unproven` | 5,864 | 1,464 | 0.042 | 84 g | baixa, desconhecida |
| Draenor | `Unknown` | 89,249 | 4,036 | 0.126 | 462 g | baixa, desconhecida |
| Draenor | `Conflict` | 8,210 | 3,153 | 0.195 | 73 g | baixa, desconhecida, média |
| Spinebreaker | `Liquid` | 351 | 188 | 0.201 | 158 g | baixa, desconhecida, média |
| Spinebreaker | `Collector` | 7,639 | 2,746 | 0.015 | 251 g | baixa, desconhecida, média |
| Spinebreaker | `Slow` | 5,977 | 2,009 | 0.046 | 46 g | baixa, desconhecida |
| Spinebreaker | `Very_Slow` | 9,865 | 2,007 | 0.030 | 50 g | baixa, desconhecida |
| Spinebreaker | `Unproven` | 5,864 | 953 | 0.029 | 90 g | baixa, desconhecida |
| Spinebreaker | `Unknown` | 89,249 | 2,792 | 0.062 | 323 g | baixa, desconhecida |
| Spinebreaker | `Conflict` | 8,210 | 2,678 | 0.034 | 73 g | baixa, desconhecida, média |
| Kazzak | `Liquid` | 351 | 193 | 0.482 | 166 g | baixa, desconhecida, média |
| Kazzak | `Collector` | 7,639 | 3,353 | 0.085 | 285 g | baixa, desconhecida, média |
| Kazzak | `Slow` | 5,977 | 2,680 | 0.121 | 48 g | baixa, desconhecida, média |
| Kazzak | `Very_Slow` | 9,865 | 3,263 | 0.049 | 79 g | baixa, desconhecida |
| Kazzak | `Unproven` | 5,864 | 1,410 | 0.028 | 94 g | baixa, desconhecida |
| Kazzak | `Unknown` | 89,249 | 3,763 | 0.110 | 470 g | baixa, desconhecida |
| Kazzak | `Conflict` | 8,210 | 3,093 | 0.115 | 73 g | baixa, desconhecida, média |
| Silvermoon | `Liquid` | 351 | 194 | 0.621 | 166 g | baixa, desconhecida, média |
| Silvermoon | `Collector` | 7,639 | 3,433 | 0.151 | 287 g | baixa, desconhecida, média |
| Silvermoon | `Slow` | 5,977 | 2,721 | 0.230 | 49 g | baixa, desconhecida, média |
| Silvermoon | `Very_Slow` | 9,865 | 3,359 | 0.102 | 80 g | baixa, desconhecida, média |
| Silvermoon | `Unproven` | 5,864 | 1,487 | 0.048 | 90 g | baixa, desconhecida, média |
| Silvermoon | `Unknown` | 89,249 | 4,160 | 0.122 | 470 g | baixa, desconhecida, média |
| Silvermoon | `Conflict` | 8,210 | 3,172 | 0.231 | 74 g | baixa, desconhecida, média |
| Twisting Nether | `Liquid` | 351 | 192 | 0.429 | 166 g | baixa, desconhecida, média |
| Twisting Nether | `Collector` | 7,639 | 3,299 | 0.059 | 285 g | baixa, desconhecida, média |
| Twisting Nether | `Slow` | 5,977 | 2,640 | 0.080 | 48 g | baixa, desconhecida, média |
| Twisting Nether | `Very_Slow` | 9,865 | 3,217 | 0.041 | 78 g | baixa, desconhecida, média |
| Twisting Nether | `Unproven` | 5,864 | 1,410 | 0.025 | 81 g | baixa, desconhecida |
| Twisting Nether | `Unknown` | 89,249 | 3,599 | 0.080 | 440 g | baixa, desconhecida, média |
| Twisting Nether | `Conflict` | 8,210 | 3,065 | 0.079 | 73 g | baixa, desconhecida, média |

## 4. Perfil TSM por reino (uma vez por conta)

1. `TSM → Settings → General → Profiles` → escreve o nome do perfil (`mogs-<reino>` na tabela) e Enter. O TSM cria-o e passa a usá-lo.
2. `Groups → Import` → cola o conteúdo de `token_rush_<reino>.tsm` **num perfil limpo** (importar duas vezes no mesmo perfil cria `… 1`).
3. Repete por personagem: o perfil é por personagem (`_currentProfile`), logo um alt novo precisa de `/tsm profile mogs-<reino>`.
4. Confirma com *Post Scan* num item de cada banda: o preço proposto tem de estar entre o piso e o tecto, e o undercut tem de ser o esperado.

**Reversão:** nada foi alterado no perfil activo. Para desfazer, apaga o perfil de teste (ou os grupos importados) e importa a proposta original (`pricing_groups_proposal.tsm`). As propostas nunca devem coexistir no mesmo perfil.

## 5. Limites

- A razão por grupo é a mediana dos itens, por isso a variação **dentro** do grupo não é respeitada — um TSM group tem uma só operação. Para respeitar item a item há o export por item (`liquidity_mogs_groups_<reino>.tsm`).
- Os custos (taxa e depósito) continuam a ser os **medidos** pelo addon, quando há eventos; até lá, são os 5 % assumidos.
- A calibração do motor está feita contra as vendas **sem data**; o addon `DFA_AuctionIntelligence` é o que fecha essa falta.
