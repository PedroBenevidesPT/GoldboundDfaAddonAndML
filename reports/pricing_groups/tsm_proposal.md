# Proposta de grupos e operações para o TSM

Alvo: **Spinebreaker**. Gerado por `scripts/pricing_groups.py`; **nada foi importado** e nada foi alterado no addon. A string está em `pricing_groups_proposal.tsm` (uma linha AceSerializer, para `Groups → Import` num **perfil de teste**).

## Como ler

- O **nome do grupo** é `PG <grupo> <banda>`: o comportamento e a banda de preço. São dimensões independentes, e por isso o grupo é a união das duas.
- As **preços** são portáteis: uma cadeia `ifgt` sobre o valor, com o coeficiente da banda. Não é preciso nenhuma fonte customizada.
- `priceReset = none`: se a parede estiver abaixo do mínimo, **não posta** (não salta para o máximo).
- `aboveMax = maxPrice`: se a parede estiver acima do tecto, posta no tecto.
- `maxExpires` é a janela de retenção **desde a última venda**, em ciclos de post.

## Os grupos propostos

| grupo TSM | grupo | banda | itens | normal | min | max | duração | maxExpires | undercut | ajuste | confiança |
|---|---|---|---:|---|---|---|---:|---:|---:|---:|---|
| `PG demand_falling <100` | demand_falling | <100 | 5 | `0.450 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.250 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.900 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | 12 h | 27 | 0.15-0.25 | 1.0 | nenhuma |
| `PG demand_stable <100` | demand_stable | <100 | 7 | `0.450 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.250 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.900 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | 48 h | 14 | 0.05-0.15 | 1.0 | nenhuma |
| `PG unknown_falling <100` | unknown_falling | <100 | 7 | `0.450 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.250 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.900 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | 12 h | 13 | 0.15-0.25 | 1.0 | nenhuma |
| `PG commodity <100` | commodity | <100 | 129 | `0.450 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.250 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.900 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | 24 h | 13 | 0.00-0.05 | 1.0 | nenhuma |
| `PG demand_falling 100-500` | demand_falling | 100-500 | 41 | `0.550 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.300 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `1.000 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | 12 h | 27 | 0.15-0.25 | 1.0 | nenhuma |
| `PG demand_stable 100-500` | demand_stable | 100-500 | 40 | `0.550 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.300 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `1.000 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | 48 h | 14 | 0.05-0.15 | 1.0 | nenhuma |
| `PG no_demand_falling 100-500` | no_demand_falling | 100-500 | 1 | `0.550 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.300 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `1.000 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | 12 h | 13 | 0.20-0.30 | 1.0 | nenhuma |
| `PG no_demand_stable 100-500` | no_demand_stable | 100-500 | 1 | `0.550 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.300 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `1.000 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | 24 h | 13 | 0.10-0.20 | 1.0 | nenhuma |
| `PG unknown_falling 100-500` | unknown_falling | 100-500 | 1 | `0.550 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.300 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `1.000 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | 12 h | 13 | 0.15-0.25 | 1.0 | nenhuma |
| `PG unknown_stable 100-500` | unknown_stable | 100-500 | 1 | `0.550 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.300 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `1.000 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | 24 h | 13 | 0.10-0.20 | 1.0 | nenhuma |
| `PG commodity 100-500` | commodity | 100-500 | 5 | `0.550 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.300 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `1.000 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | 24 h | 13 | 0.00-0.05 | 1.0 | nenhuma |
| `PG demand_falling 500-2k` | demand_falling | 500-2k | 16 | `0.620 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.350 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `1.150 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | 12 h | 27 | 0.15-0.25 | 1.0 | nenhuma |
| `PG demand_stable 500-2k` | demand_stable | 500-2k | 12 | `0.620 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.350 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `1.150 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | 48 h | 14 | 0.05-0.15 | 1.0 | nenhuma |
| `PG no_demand_falling 500-2k` | no_demand_falling | 500-2k | 1 | `0.620 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.350 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `1.150 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | 12 h | 13 | 0.20-0.30 | 1.0 | nenhuma |
| `PG no_demand_stable 500-2k` | no_demand_stable | 500-2k | 1 | `0.620 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.350 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `1.150 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | 24 h | 13 | 0.10-0.20 | 1.0 | nenhuma |
| `PG unknown_stable 500-2k` | unknown_stable | 500-2k | 4 | `0.620 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.350 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `1.150 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | 24 h | 13 | 0.10-0.20 | 1.0 | nenhuma |
| `PG demand_falling 2k-5k` | demand_falling | 2k-5k | 11 | `0.680 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.400 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `1.300 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | 12 h | 27 | 0.15-0.25 | 1.0 | nenhuma |
| `PG demand_stable 2k-5k` | demand_stable | 2k-5k | 11 | `0.680 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.400 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `1.300 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | 48 h | 14 | 0.05-0.15 | 1.0 | nenhuma |
| `PG no_demand_falling 2k-5k` | no_demand_falling | 2k-5k | 1 | `0.680 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.400 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `1.300 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | 12 h | 13 | 0.20-0.30 | 1.0 | nenhuma |
| `PG no_demand_stable 2k-5k` | no_demand_stable | 2k-5k | 1 | `0.680 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.400 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `1.300 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | 24 h | 13 | 0.10-0.20 | 1.0 | nenhuma |
| `PG unknown_falling 2k-5k` | unknown_falling | 2k-5k | 1 | `0.680 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.400 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `1.300 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | 12 h | 13 | 0.15-0.25 | 1.0 | nenhuma |
| `PG unknown_stable 2k-5k` | unknown_stable | 2k-5k | 3 | `0.680 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.400 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `1.300 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | 24 h | 13 | 0.10-0.20 | 1.0 | nenhuma |
| `PG commodity 2k-5k` | commodity | 2k-5k | 2 | `0.680 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.400 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `1.300 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | 24 h | 13 | 0.00-0.05 | 1.0 | nenhuma |
| `PG demand_stable 5k-15k` | demand_stable | 5k-15k | 5 | `0.780 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.450 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `1.450 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | 48 h | 14 | 0.05-0.15 | 1.0 | nenhuma |
| `PG unknown_falling 5k-15k` | unknown_falling | 5k-15k | 1 | `0.780 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.450 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `1.450 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | 12 h | 13 | 0.15-0.25 | 1.0 | nenhuma |
| `PG demand_falling 15k-50k` | demand_falling | 15k-50k | 1 | `0.850 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.500 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `1.600 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | 12 h | 27 | 0.15-0.25 | 1.0 | nenhuma |
| `PG demand_stable 15k-50k` | demand_stable | 15k-50k | 3 | `0.850 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.500 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `1.600 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | 48 h | 14 | 0.05-0.15 | 1.0 | nenhuma |
| `PG unknown_stable 15k-50k` | unknown_stable | 15k-50k | 1 | `0.850 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.500 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `1.600 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | 24 h | 13 | 0.10-0.20 | 1.0 | nenhuma |
| `PG demand_falling 50k-150k` | demand_falling | 50k-150k | 1 | `0.850 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.500 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `1.600 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | 12 h | 27 | 0.15-0.25 | 1.0 | nenhuma |
| `PG demand_stable 50k-150k` | demand_stable | 50k-150k | 2 | `0.850 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.500 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `1.600 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | 48 h | 14 | 0.05-0.15 | 1.0 | nenhuma |
| `PG unknown_stable 50k-150k` | unknown_stable | 50k-150k | 1 | `0.850 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `0.500 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | `1.600 * first(dbmarket, dbregionmarketavg, dbregionhistorical)` | 24 h | 13 | 0.10-0.20 | 1.0 | nenhuma |

## As fórmulas

```
normalPrice = banda_normal(ajuste) * first(dbmarket, dbregionmarketavg, dbregionhistorical)
minPrice   = banda_min(ajuste)   * first(dbmarket, dbregionmarketavg, dbregionhistorical)
maxPrice   = banda_max(ajuste)   * first(dbmarket, dbregionmarketavg, dbregionhistorical)
```

A âncora é **local** (`dbmarket`): por isso o factor do reino (local ÷ regional) **não** é aplicado aqui — contaria o mesmo ajuste duas vezes. O factor do reino entra no export por reino (`mogs_groups_<reino>.tsm`) e na camada de campanha, uma vez só.

O **alvo de liquidação** é relativo à parede quando existe um comparável válido (a parede é o ask mais barato do mercado; undercutê-la é como a venda acontece), e volta à fórmula normal quando não há comparável. O ajuste do grupo **não** é aplicado à parede: foi estimado sobre o ask.

**A string tem 19,721 caracteres** e 31 grupos.

> Importa primeiro num perfil de teste. Esta proposta nunca foi executada dentro do addon.
