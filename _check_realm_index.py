import sys
import re
sys.path.insert(0, r'C:\FarmAnalysis')

with open(r'C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns\DFA_Estimate\Data\DFA_Estimate_Data.lua', 'r') as f:
    content = f.read()

# Find realmIndex entries with sample > 0
idx = content.find('realmIndex')
if idx >= 0:
    section = content[idx:idx+20000]
    matches = re.findall(r'\["([^"]+)"\] = \{ factor = [\d.]+, sample = (\d+), source = "([^"]+)"', section)
    print('RealmIndex entries with sample > 0:')
    for m in matches:
        if int(m[1]) > 0:
            print(' -', m[0], 'sample=', m[1], 'source=', m[2])
PY