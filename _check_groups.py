import sys
import re
sys.path.insert(0, r'C:\FarmAnalysis')

with open(r'C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns\DFA_Estimate\Data\DFA_Estimate_Data.lua', 'r') as f:
    content = f.read()

# Find the realms section
idx = content.find('realms = {')
if idx >= 0:
    section = content[idx:idx+5000]
    groups = re.findall(r'group = "([^"]+)"', section)
    print('Groups found:', len(groups))
    for g in groups:
        print(' -', g)
PY