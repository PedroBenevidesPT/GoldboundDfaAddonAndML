# Perfil de mercado por reino

Gerado por `python scripts/mog_realm_profiles.py`. Este ficheiro é o **resultado obrigatório por reino** (classificação, indicadores, estratégia, reatividade, regras, qualidade da evidência e critérios de revisão). Os números de política vivem em `woweconomy/analytics/mog_realm.py`; o export lê-os de lá, para o relatório e o `.tsm` não poderem divergir.

## O que é observado e em que janela

| dado | fonte | janela | cobertura |
|---|---|---|---|
| preço local (`market_value`) | `tsm_realm_price` | último valor por item, descarregado a 19/10/2026 | 92 reinos, 13k-18k itens cada |
| referência regional | `tsm_region_price` (último snapshot) | idem | 31 184 itens |
| nº de anúncios por item | `tsm_market` | última descarga AuctionDB (`tsm_meta`) | **12 reinos** |
| vendas realizadas | `journal_stats` | histórico da conta | só os reinos onde vendeste |

**Não observado:** velocidade de venda por reino, tempo até à venda, frequência de undercut e reaparecimento de anúncios. Os snapshots de `tsm_realm_price` guardam apenas as linhas que **mudaram** em cada descarga (é um log de alterações, não um retrato completo), portanto não servem para medir turnover; o desaparecimento de um anúncio não é uma venda confirmada. A classificação abaixo é estrutural (oferta presente), não uma medida de liquidez.

## Classificação

- **thin** (pouco competitivo): mediana de anúncios por item listado < 1.5 — a maior parte dos itens tem no máximo um concorrente.
- **deep** (competitivo): mediana ≥ 3.5 ou parede (≥3 anúncios) em ≥ 60% dos itens listados.
- **balanced** (intermédio): o resto; não é automaticamente líquido nem pouco competitivo — a estratégia é a da banda, sem prémio nem desconto de reino.

Sem contagem de anúncios (84 dos 92 reinos) a classe é **inferida** do nível de preço: ≥ 95% da região → thin, < 45% → deep. Nos oito reinos em que ambas são conhecidas a regra de preço acerta em sete (falha em Grim Batol, 1,04× com 2 anúncios por item).

## Tabela

| reino | grupo | classe | factor | itens | % listados | anúncios/item | parede ≥3 | solo (=1) | preço/região | amostra | confiança | vendas |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|
| Pozzo dell'Eternità | 1309 | thin | 1.24 | 10597 | 34.9% | 1.0 | 11.0% | 66.6% | 1.242 | 7995 | alta | — |
| Shattered Halls | 1598 | thin | 1.06 | 12901 | 46.6% | 1.0 | 15.3% | 64.1% | 1.058 | 10082 | alta | — |
| Spinebreaker | 3656 | thin | 1.08 | 13351 | 44.0% | 1.0 | 17.2% | 63.3% | 1.081 | 10610 | alta | 6015 |
| Grim Batol | 1303 | balanced | 1.04 | 14966 | 61.6% | 2.0 | 26.5% | 46.9% | 1.038 | 11912 | alta | — |
| Twisting Nether | 3674 | balanced | 0.74 | 17419 | 82.1% | 2.0 | 49.4% | 29.2% | 0.74 | 13997 | alta | 234 |
| Draenor | 1403 | deep | 0.30 | 18292 | 93.0% | 4.0 | 66.4% | 17.4% | 0.3 | 14494 | alta | — |
| Silvermoon | 3391 | deep | 0.27 | 18528 | 94.8% | 4.0 | 72.4% | 15.0% | 0.271 | 14633 | alta | 10 |
| Argent Dawn | 3702 | deep | 0.21 | 18343 | 96.6% | 5.0 | 78.7% | 11.5% | 0.207 | 14587 | alta | 21 |
| Aegwynn | 3679 | thin | 1.30 | 11170 | — | — | — | — | 1.424 | 8615 | inferida | — |
| Aggramar | 1325 | thin | 1.12 | 11091 | — | — | — | — | 1.118 | 8474 | inferida | — |
| Al'Akir | 3713 | thin | 1.02 | 16121 | — | — | — | — | 1.024 | 12987 | inferida | — |
| Anetheron | 1104 | thin | 1.07 | 8909 | — | — | — | — | 1.068 | 6365 | inferida | — |
| Antonidas | 3686 | balanced | 0.81 | 17195 | — | — | — | — | 0.811 | 13823 | inferida | — |
| Arak-arahm | 512 | thin | 1.30 | 7914 | — | — | — | — | 1.38 | 5653 | inferida | — |
| Archimonde | 1302 | thin | 1.23 | 14447 | — | — | — | — | 1.227 | 11563 | inferida | — |
| Arthas | 578 | thin | 1.00 | 12736 | — | — | — | — | 1.0 | 9941 | inferida | — |
| Arygos | 1406 | thin | 1.10 | 10302 | — | — | — | — | 1.099 | 7984 | inferida | — |
| Ashenvale | 1923 | thin | 1.00 | 3653 | — | — | — | — | 1.0 | 2221 | inferida | — |
| Auchindoun | 1597 | thin | 1.00 | 16257 | — | — | — | — | 0.997 | 12990 | inferida | 42 |
| Azuregos | 1922 | thin | 1.00 | 5012 | — | — | — | — | 1.0 | 3408 | inferida | — |
| Azuremyst | 1417 | thin | 1.05 | 10603 | — | — | — | — | 1.049 | 8097 | inferida | — |
| Blackmoore | 580 | balanced | 0.82 | 17595 | — | — | — | — | 0.82 | 14111 | inferida | — |
| Blackrock | 581 | thin | 1.01 | 16807 | — | — | — | — | 1.011 | 13657 | inferida | — |
| Blackscar | 1929 | balanced | 0.76 | 10694 | — | — | — | — | 0.761 | 8106 | inferida | — |
| Blade's Edge | 1416 | thin | 1.04 | 14155 | — | — | — | — | 1.039 | 11194 | inferida | — |
| Bronze Dragonflight | 1393 | thin | 1.00 | 11046 | — | — | — | — | 1.0 | 8498 | inferida | — |
| Chamber of Aspects | 1307 | thin | 1.03 | 11576 | — | — | — | — | 1.027 | 9072 | inferida | — |
| Confrérie du Thorium | 1127 | thin | 0.98 | 16125 | — | — | — | — | 0.98 | 12958 | inferida | — |
| Deathguard | 1605 | balanced | 0.84 | 4256 | — | — | — | — | 0.838 | 2812 | inferida | — |
| Dentarg | 1084 | balanced | 0.62 | 17889 | — | — | — | — | 0.616 | 14310 | inferida | — |
| Dethecus | 531 | thin | 1.14 | 10863 | — | — | — | — | 1.135 | 8263 | inferida | — |
| Die Aldor | 1618 | thin | 0.96 | 14680 | — | — | — | — | 0.962 | 11847 | inferida | — |
| Die Arguswacht | 1121 | thin | 1.00 | 14279 | — | — | — | — | 1.005 | 11457 | inferida | — |
| Drak'thul | 1092 | thin | 0.99 | 15612 | — | — | — | — | 0.993 | 12563 | inferida | — |
| Dun Modr | 1378 | thin | 1.00 | 14210 | — | — | — | — | 1.004 | 11345 | inferida | — |
| Echsenkessel | 3691 | balanced | 0.64 | 17527 | — | — | — | — | 0.639 | 14112 | inferida | — |
| Elune | 1315 | thin | 1.05 | 9911 | — | — | — | — | 1.052 | 7501 | inferida | — |
| Emeriss | 1091 | thin | 1.12 | 9783 | — | — | — | — | 1.117 | 7253 | inferida | — |
| Eredar | 3692 | thin | 1.11 | 15094 | — | — | — | — | 1.106 | 12168 | inferida | — |
| Eversong | 1925 | balanced | 0.89 | 11569 | — | — | — | — | 0.895 | 8878 | inferida | — |
| Exodar | 1385 | thin | 1.30 | 11718 | — | — | — | — | 1.5 | 9145 | inferida | — |
| Fordragon | 1623 | thin | 1.00 | 4717 | — | — | — | — | 1.0 | 3145 | inferida | — |
| Frostwolf | 3703 | thin | 1.00 | 10936 | — | — | — | — | 0.999 | 8504 | inferida | — |
| Galakrond | 1614 | balanced | 0.59 | 6668 | — | — | — | — | 0.592 | 4623 | inferida | — |
| Garona | 509 | thin | 1.30 | 7956 | — | — | — | — | 1.517 | 5810 | inferida | — |
| Garrosh | 1401 | thin | 1.00 | 14654 | — | — | — | — | 1.0 | 11692 | inferida | — |
| Genjuros | 3657 | thin | 1.10 | 12478 | — | — | — | — | 1.098 | 9773 | inferida | — |
| Goldrinn | 1928 | thin | 0.99 | 5965 | — | — | — | — | 0.987 | 3983 | inferida | — |
| Gordunni | 1602 | balanced | 0.64 | 15766 | — | — | — | — | 0.636 | 12660 | inferida | — |
| Hellfire | 1587 | balanced | 0.87 | 16181 | — | — | — | — | 0.874 | 12851 | inferida | — |
| Howling Fjord | 1615 | balanced | 0.85 | 15172 | — | — | — | — | 0.85 | 12160 | inferida | — |
| Hyjal | 1390 | balanced | 0.81 | 16551 | — | — | — | — | 0.809 | 13212 | inferida | — |
| Karazhan | 1596 | thin | 1.00 | 14540 | — | — | — | — | 1.0 | 11581 | inferida | — |
| Kazzak | 1305 | balanced | 0.57 | 17755 | — | — | — | — | 0.574 | 14210 | inferida | — |
| Khadgar | 1080 | thin | 1.26 | 9672 | — | — | — | — | 1.265 | 7282 | inferida | — |
| Khaz Modan | 3690 | thin | 1.00 | 12554 | — | — | — | — | 0.996 | 9765 | inferida | — |
| Kor'gall | 633 | thin | 1.05 | 14889 | — | — | — | — | 1.048 | 11899 | inferida | — |
| Kul Tiras | 1082 | thin | 1.30 | 10745 | — | — | — | — | 1.444 | 8160 | inferida | — |
| Lightbringer | 1388 | thin | 1.07 | 8747 | — | — | — | — | 1.069 | 6370 | inferida | — |
| Lothar | 570 | thin | 1.00 | 10464 | — | — | — | — | 1.0 | 8077 | inferida | — |
| Magtheridon | 3681 | thin | 1.12 | 11162 | — | — | — | — | 1.119 | 8528 | inferida | — |
| Malygos | 1098 | thin | 1.00 | 12001 | — | — | — | — | 1.0 | 9299 | inferida | — |
| Marécage de Zangar | 1621 | thin | 1.00 | 14360 | — | — | — | — | 1.0 | 11415 | inferida | — |
| Moonglade | 1085 | thin | 1.01 | 13069 | — | — | — | — | 1.012 | 10291 | inferida | — |
| Naxxramas | 1624 | thin | 1.30 | 9751 | — | — | — | — | 1.459 | 7422 | inferida | — |
| Nazjatar | 1105 | thin | 1.30 | 13030 | — | — | — | — | 1.313 | 10225 | inferida | — |
| Nefarian | 612 | thin | 1.00 | 12375 | — | — | — | — | 1.0 | 9625 | inferida | — |
| Nemesis | 1316 | thin | 1.07 | 10522 | — | — | — | — | 1.073 | 8159 | inferida | — |
| Nethersturm | 3696 | thin | 1.00 | 13494 | — | — | — | — | 1.002 | 10749 | inferida | — |
| Norgannon | 1408 | thin | 1.01 | 15518 | — | — | — | — | 1.013 | 12430 | inferida | — |
| Outland | 1301 | thin | 1.10 | 14033 | — | — | — | — | 1.102 | 11195 | inferida | — |
| Quel'Thalas | 1396 | thin | 1.00 | 13773 | — | — | — | — | 1.0 | 10901 | inferida | — |
| Ragnaros | 3682 | balanced | 0.91 | 16548 | — | — | — | — | 0.909 | 13320 | inferida | — |
| Ravencrest | 1329 | balanced | 0.72 | 17586 | — | — | — | — | 0.723 | 14122 | inferida | — |
| Rexxar | 1099 | balanced | 0.78 | 13691 | — | — | — | — | 0.784 | 10866 | inferida | — |
| Scarshield Legion | 1096 | thin | 0.97 | 16993 | — | — | — | — | 0.965 | 13688 | inferida | — |
| Shadowsong | 3666 | thin | 1.00 | 11945 | — | — | — | — | 1.0 | 9468 | inferida | — |
| Soulflayer | 1604 | thin | 0.99 | 11213 | — | — | — | — | 0.985 | 8783 | inferida | — |
| Stormscale | 2073 | thin | 1.02 | 16550 | — | — | — | — | 1.018 | 13291 | inferida | — |
| Suramar | 1331 | thin | 1.12 | 7104 | — | — | — | — | 1.121 | 5035 | inferida | — |
| Terenas | 2074 | thin | 1.08 | 10976 | — | — | — | — | 1.085 | 8409 | inferida | — |
| Thrall | 604 | thin | 0.96 | 16902 | — | — | — | — | 0.962 | 13643 | inferida | — |
| Todeswache | 1405 | balanced | 0.92 | 15885 | — | — | — | — | 0.917 | 12740 | inferida | — |
| Turalyon | 1402 | balanced | 0.71 | 17290 | — | — | — | — | 0.705 | 13832 | inferida | — |
| Tyrande | 1384 | thin | 1.24 | 9935 | — | — | — | — | 1.238 | 7571 | inferida | — |
| Uldaman | 1122 | thin | 1.00 | 9854 | — | — | — | — | 1.005 | 7374 | inferida | — |
| Un'Goro | 1400 | thin | 0.97 | 9941 | — | — | — | — | 0.971 | 7450 | inferida | — |
| Vol'jin | 510 | thin | 0.95 | 8829 | — | — | — | — | 0.954 | 6531 | inferida | — |
| Wildhammer | 1313 | thin | 1.03 | 9687 | — | — | — | — | 1.029 | 7315 | inferida | — |
| Ysera | 1097 | thin | 1.01 | 9248 | — | — | — | — | 1.006 | 6817 | inferida | — |
| Ysondre | 1335 | thin | 1.11 | 12774 | — | — | — | — | 1.113 | 10081 | inferida | — |
| Zul'jin | 1379 | thin | 1.03 | 15843 | — | — | — | — | 1.028 | 12760 | inferida | — |

`factor` = mediana de `mercado do reino / mercado regional`, já limitada a [0.20, 1.30]. É multiplicado por **todas** as bandas e **todos** os grupos: não há coeficientes reino×banda×grupo (não há amostra de vendas para os estimar). A dispersão da razão dentro do reino está em p25-p75:

| reino | p25 | mediana | p75 |
|---|---:|---:|---:|
| Pozzo dell'Eternità | 0.856 | 1.242 | 4.325 |
| Shattered Halls | 0.644 | 1.058 | 2.038 |
| Spinebreaker | 0.771 | 1.081 | 2.177 |
| Grim Batol | 0.78 | 1.038 | 1.634 |
| Twisting Nether | 0.311 | 0.74 | 1.087 |
| Draenor | 0.12 | 0.3 | 0.757 |
| Silvermoon | 0.113 | 0.271 | 0.672 |
| Argent Dawn | 0.085 | 0.207 | 0.582 |
| Aegwynn | 0.974 | 1.424 | 3.187 |
| Aggramar | 0.954 | 1.118 | 2.674 |
| Al'Akir | 0.638 | 1.024 | 1.689 |
| Anetheron | 0.837 | 1.068 | 1.993 |
| Antonidas | 0.338 | 0.811 | 1.398 |
| Arak-arahm | 1.0 | 1.38 | 2.843 |
| Archimonde | 0.856 | 1.227 | 2.237 |
| Arthas | 0.541 | 1.0 | 1.589 |
| Arygos | 0.975 | 1.099 | 1.432 |
| Ashenvale | 0.673 | 1.0 | 1.891 |
| Auchindoun | 0.588 | 0.997 | 1.372 |
| Azuregos | 0.516 | 1.0 | 1.544 |
| Azuremyst | 0.89 | 1.049 | 1.998 |
| Blackmoore | 0.447 | 0.82 | 1.19 |
| Blackrock | 0.605 | 1.011 | 1.512 |
| Blackscar | 0.213 | 0.761 | 1.551 |
| Blade's Edge | 0.799 | 1.039 | 2.006 |
| Bronze Dragonflight | 0.524 | 1.0 | 2.149 |
| Chamber of Aspects | 0.869 | 1.027 | 2.024 |
| Confrérie du Thorium | 0.527 | 0.98 | 1.421 |
| Deathguard | 0.157 | 0.838 | 2.0 |
| Dentarg | 0.241 | 0.616 | 1.006 |
| Dethecus | 0.983 | 1.135 | 2.45 |
| Die Aldor | 0.538 | 0.962 | 1.508 |
| Die Arguswacht | 0.639 | 1.005 | 1.534 |
| Drak'thul | 0.517 | 0.993 | 1.436 |
| Dun Modr | 0.604 | 1.004 | 1.547 |
| Echsenkessel | 0.267 | 0.639 | 1.074 |
| Elune | 0.778 | 1.052 | 2.372 |
| Emeriss | 0.799 | 1.117 | 2.252 |
| Eredar | 0.698 | 1.106 | 1.935 |
| Eversong | 0.191 | 0.895 | 1.383 |
| Exodar | 0.897 | 1.5 | 4.594 |
| Fordragon | 0.64 | 1.0 | 1.815 |
| Frostwolf | 0.5 | 0.999 | 1.713 |
| Galakrond | 0.157 | 0.592 | 1.25 |
| Garona | 1.0 | 1.517 | 5.182 |
| Garrosh | 0.504 | 1.0 | 1.424 |
| Genjuros | 0.803 | 1.098 | 2.496 |
| Goldrinn | 0.249 | 0.987 | 1.476 |
| Gordunni | 0.211 | 0.636 | 1.134 |
| Hellfire | 0.294 | 0.874 | 1.372 |
| Howling Fjord | 0.315 | 0.85 | 1.365 |
| Hyjal | 0.351 | 0.809 | 1.12 |
| Karazhan | 0.555 | 1.0 | 1.685 |
| Kazzak | 0.195 | 0.574 | 1.05 |
| Khadgar | 0.85 | 1.265 | 3.937 |
| Khaz Modan | 0.364 | 0.996 | 1.767 |
| Kor'gall | 0.624 | 1.048 | 1.884 |
| Kul Tiras | 0.839 | 1.444 | 3.784 |
| Lightbringer | 0.745 | 1.069 | 3.022 |
| Lothar | 0.625 | 1.0 | 1.892 |
| Magtheridon | 0.894 | 1.119 | 3.0 |
| Malygos | 0.625 | 1.0 | 1.607 |
| Marécage de Zangar | 0.698 | 1.0 | 1.417 |
| Moonglade | 0.71 | 1.012 | 1.92 |
| Naxxramas | 1.004 | 1.459 | 3.114 |
| Nazjatar | 0.931 | 1.313 | 2.533 |
| Nefarian | 0.704 | 1.0 | 1.684 |
| Nemesis | 0.854 | 1.073 | 2.304 |
| Nethersturm | 0.518 | 1.002 | 1.718 |
| Norgannon | 0.461 | 1.013 | 2.009 |
| Outland | 0.842 | 1.102 | 2.0 |
| Quel'Thalas | 0.479 | 1.0 | 1.502 |
| Ragnaros | 0.38 | 0.909 | 1.247 |
| Ravencrest | 0.304 | 0.723 | 1.063 |
| Rexxar | 0.171 | 0.784 | 1.25 |
| Scarshield Legion | 0.454 | 0.965 | 1.393 |
| Shadowsong | 0.513 | 1.0 | 1.558 |
| Soulflayer | 0.378 | 0.985 | 1.542 |
| Stormscale | 0.759 | 1.018 | 1.534 |
| Suramar | 0.964 | 1.121 | 2.125 |
| Terenas | 0.896 | 1.085 | 2.107 |
| Thrall | 0.522 | 0.962 | 1.525 |
| Todeswache | 0.428 | 0.917 | 1.495 |
| Turalyon | 0.284 | 0.705 | 1.109 |
| Tyrande | 0.783 | 1.238 | 3.494 |
| Uldaman | 0.552 | 1.005 | 1.971 |
| Un'Goro | 0.278 | 0.971 | 1.614 |
| Vol'jin | 0.194 | 0.954 | 1.5 |
| Wildhammer | 0.428 | 1.029 | 2.489 |
| Ysera | 0.791 | 1.006 | 2.017 |
| Ysondre | 0.82 | 1.113 | 2.1 |
| Zul'jin | 0.689 | 1.028 | 1.522 |

## Estratégia por classe

A banda decide **quanto abaixo** do valor de referência se posta; o reino decide **que valor de referência é esse** (o factor), **quanto se pede** (postura) e **quanto se espera** (duração e janela).

| classe | postura | efeito no preço |
|---|---:|---|
| thin | ×1,20 | um anúncio sozinho pode pedir mais; não persegue quem baixe |
| balanced | ×1,00 | só a correção para o nível local |
| deep | ×1,00 | sem desconto: com parede, o preço já é `parede − undercut`; a alavanca é a velocidade |

Duração e retenção (`maxExpires` = expirações **desde a última venda**):

| classe | liquid | collector / slow / very_slow | unproven / conflict | retenção |
|---|---:|---:|---:|---:|
| thin (pouco competitivo) | 48h (29) | 48h (29) | 48h (29) | 60 dias |
| balanced (intermédio) | 24h (29) | 48h (14) | 24h (29) | 30 dias |
| deep (competitivo) | 12h (27) | 48h (6) | 24h (13) | 14 dias |

Regras iguais em todos os reinos: `priceReset = none` (parede abaixo do mínimo → **não postar**, nunca saltar para o máximo), `aboveMax = maxPrice` (parede acima do tecto → postar no tecto), sem cancelamento automático, um anúncio por item.

## Perfis medidos (com contagem de anúncios)

### Pozzo dell'Eternità (grupo 1309) — thin, pouco competitivo

- **Indicadores** (7995 itens com preço local e regional): mediana de 1.0 anúncios por item listado; parede em 11.0% dos itens listados; um único anúncio em 66.6%; 34.9% dos itens com preço estão listados; preço local a **1.242×** a região (p25 0.856, p75 4.325).
- **Estratégia**: factor **1.24** × postura **1.20** = escala **1.49**. Um `Collector` na banda 500 g-5 k, cujo coeficiente é 0,68, posta a **1.01×V** (V = mercado regional).
- **Reatividade**: 48 h de base (o `Liquid` neste reino posta a 48 h) e retenção de 60 dias.
- **Evidência**: medida (nº de anúncios observado), 7995 itens; a razão varia dentro do reino (p25-p75), por isso o factor é um compromisso.

### Shattered Halls (grupo 1598) — thin, pouco competitivo

- **Indicadores** (10082 itens com preço local e regional): mediana de 1.0 anúncios por item listado; parede em 15.3% dos itens listados; um único anúncio em 64.1%; 46.6% dos itens com preço estão listados; preço local a **1.058×** a região (p25 0.644, p75 2.038).
- **Estratégia**: factor **1.06** × postura **1.20** = escala **1.27**. Um `Collector` na banda 500 g-5 k, cujo coeficiente é 0,68, posta a **0.86×V** (V = mercado regional).
- **Reatividade**: 48 h de base (o `Liquid` neste reino posta a 48 h) e retenção de 60 dias.
- **Evidência**: medida (nº de anúncios observado), 10082 itens; a razão varia dentro do reino (p25-p75), por isso o factor é um compromisso.

### Spinebreaker (grupo 3656) — thin, pouco competitivo

- **Indicadores** (10610 itens com preço local e regional): mediana de 1.0 anúncios por item listado; parede em 17.2% dos itens listados; um único anúncio em 63.3%; 44.0% dos itens com preço estão listados; preço local a **1.081×** a região (p25 0.771, p75 2.177).
- **Estratégia**: factor **1.08** × postura **1.20** = escala **1.30**. Um `Collector` na banda 500 g-5 k, cujo coeficiente é 0,68, posta a **0.88×V** (V = mercado regional).
- **Reatividade**: 48 h de base (o `Liquid` neste reino posta a 48 h) e retenção de 60 dias.
- **Evidência**: medida (nº de anúncios observado), 10610 itens; a razão varia dentro do reino (p25-p75), por isso o factor é um compromisso.

### Grim Batol (grupo 1303) — balanced, intermédio

- **Indicadores** (11912 itens com preço local e regional): mediana de 2.0 anúncios por item listado; parede em 26.5% dos itens listados; um único anúncio em 46.9%; 61.6% dos itens com preço estão listados; preço local a **1.038×** a região (p25 0.78, p75 1.634).
- **Estratégia**: factor **1.04** × postura **1.00** = escala **1.04**. Um `Collector` na banda 500 g-5 k, cujo coeficiente é 0,68, posta a **0.71×V** (V = mercado regional).
- **Reatividade**: 24 h de base (o `Liquid` neste reino posta a 24 h) e retenção de 30 dias.
- **Evidência**: medida (nº de anúncios observado), 11912 itens; a razão varia dentro do reino (p25-p75), por isso o factor é um compromisso.

### Twisting Nether (grupo 3674) — balanced, intermédio

- **Indicadores** (13997 itens com preço local e regional): mediana de 2.0 anúncios por item listado; parede em 49.4% dos itens listados; um único anúncio em 29.2%; 82.1% dos itens com preço estão listados; preço local a **0.740×** a região (p25 0.311, p75 1.087).
- **Estratégia**: factor **0.74** × postura **1.00** = escala **0.74**. Um `Collector` na banda 500 g-5 k, cujo coeficiente é 0,68, posta a **0.50×V** (V = mercado regional).
- **Reatividade**: 24 h de base (o `Liquid` neste reino posta a 24 h) e retenção de 30 dias.
- **Evidência**: medida (nº de anúncios observado), 13997 itens; a razão varia dentro do reino (p25-p75), por isso o factor é um compromisso.

### Draenor (grupo 1403) — deep, competitivo

- **Indicadores** (14494 itens com preço local e regional): mediana de 4.0 anúncios por item listado; parede em 66.4% dos itens listados; um único anúncio em 17.4%; 93.0% dos itens com preço estão listados; preço local a **0.300×** a região (p25 0.12, p75 0.757).
- **Estratégia**: factor **0.30** × postura **1.00** = escala **0.30**. Um `Collector` na banda 500 g-5 k, cujo coeficiente é 0,68, posta a **0.20×V** (V = mercado regional).
- **Reatividade**: 12 h de base (o `Liquid` neste reino posta a 12 h) e retenção de 14 dias.
- **Evidência**: medida (nº de anúncios observado), 14494 itens; a razão varia dentro do reino (p25-p75), por isso o factor é um compromisso.

### Silvermoon (grupo 3391) — deep, competitivo

- **Indicadores** (14633 itens com preço local e regional): mediana de 4.0 anúncios por item listado; parede em 72.4% dos itens listados; um único anúncio em 15.0%; 94.8% dos itens com preço estão listados; preço local a **0.271×** a região (p25 0.113, p75 0.672).
- **Estratégia**: factor **0.27** × postura **1.00** = escala **0.27**. Um `Collector` na banda 500 g-5 k, cujo coeficiente é 0,68, posta a **0.18×V** (V = mercado regional).
- **Reatividade**: 12 h de base (o `Liquid` neste reino posta a 12 h) e retenção de 14 dias.
- **Evidência**: medida (nº de anúncios observado), 14633 itens; a razão varia dentro do reino (p25-p75), por isso o factor é um compromisso.

### Argent Dawn (grupo 3702) — deep, competitivo

- **Indicadores** (14587 itens com preço local e regional): mediana de 5.0 anúncios por item listado; parede em 78.7% dos itens listados; um único anúncio em 11.5%; 96.6% dos itens com preço estão listados; preço local a **0.207×** a região (p25 0.085, p75 0.582).
- **Estratégia**: factor **0.21** × postura **1.00** = escala **0.21**. Um `Collector` na banda 500 g-5 k, cujo coeficiente é 0,68, posta a **0.14×V** (V = mercado regional).
- **Reatividade**: 12 h de base (o `Liquid` neste reino posta a 12 h) e retenção de 14 dias.
- **Evidência**: medida (nº de anúncios observado), 14587 itens; a razão varia dentro do reino (p25-p75), por isso o factor é um compromisso.

## Reinos com vendas e ficheiros de import

O ficheiro de cada reino é o que se importa no perfil TSM desse reino (o perfil é por personagem; cria-o antes na UI). Reinos sem ficheiro gerado aparecem como `—`; gera-o com `python scripts/tsm_groups_export.py --write --realm "<reino>"`.

| reino | perfil medido | classe | factor | vendas | receita | ficheiro |
|---|---|---|---:|---:|---:|---|
| Dragonmaw | Spinebreaker | thin | 1.08 | 6,015 | 20,297 g | `mogs_groups_dragonmaw.tsm` |
| Twisting Nether | Twisting Nether | balanced | 0.74 | 234 | 1,085 g | `mogs_groups_twisting-nether.tsm` |
| Silvermoon | Silvermoon | deep | 0.27 | 10 | 145 g | `mogs_groups_silvermoon.tsm` |
| Argent Dawn | Argent Dawn | deep | 0.21 | 21 | 830 g | `mogs_groups_argent-dawn.tsm` |
| Auchindoun | Auchindoun | thin | 1.00 | 42 | 2,203 g | `mogs_groups_auchindoun.tsm` |

## Limitações

- O `factor` é uma mediana de todo o catálogo e a razão varia com a banda (ver p25-p75): em Pozzo vai de ~1,0 acima de 50 k a ~2,5 abaixo de 500 g. Aplicar um escalar é um compromisso deliberado, não um ajuste fino.
- O nível regional é uma média inflacionada por anúncios mortos de reinos vazios; é por isso que reinos cheios aparecem a 0,2-0,3× e reinos vazios a 1,0-1,3×. O factor corrige exactamente isso.
- `tsm_market` não tem timestamp; a idade da contagem de anúncios é a da última descarga de AuctionDB em `tsm_meta`.
- Não há backtesting de lucro por reino: sem série de vendas por reino, qualquer comparação de lucro realizado seria inventada.

## Critérios para rever

1. **Mudança de classe**: a mediana de anúncios por item passa de <1,5 para ≥3,5 (ou o contrário) — refazer o export desse reino.
2. **Desvio do factor**: se a mediana `preço/região` se afastar mais de 20 % do valor na tabela, recalcular e reimportar.
3. **Cobertura**: se `% listados` ou `parede ≥3` mudar mais de 10 pontos percentuais, a classe inferida deixa de ser fiável.
4. **Resultado**: após 30 dias, comparar vendas realizadas por reino (`journal_stats`) com o esperado; se um reino `deep` vender ao mesmo ritmo que um `thin`, o problema não é o preço — é a selecção de itens.
5. **Idade**: se a última descarga de AuctionDB tiver mais de 7 dias, os indicadores de concorrência estão velhos; usar só o factor.
