# run_daily.ps1 — pipeline diario DfA
cd C:\FarmAnalysis

python scripts/sync_market.py --snapshots --snapshot-kinds realm_items --snapshot-workers 1 --snapshot-delay 10
python scripts/dfa_ml.py train --refresh-journal
python scripts/dfa_pricing_export.py

$src = "C:\FarmAnalysis\DFA_Estimate"
$dst = "C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns\DFA_Estimate"
robocopy $src $dst /MIR /NFL /NDL /NJH /NJS /NP
