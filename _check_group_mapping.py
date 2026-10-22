import sys
import re
sys.path.insert(0, r'C:\FarmAnalysis')

with open(r'C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns\DFA_Estimate\Data\DFA_Estimate_Data.lua', 'r') as f:
    content = f.read()

# Check if realmIndex has group field
idx = content.find('realmIndex')
if idx >= 0:
    section = content[idx:idx+20000]
    matches = re.findall(r'\["([^"]+)"\] = \{[^}]*group\s*=\s*"([^"]+)"', section)
    print('RealmIndex entries with group:')
    for m in matches:
        print(' -', m[0], '->', m[1])
    
    # Also check realms section for group
    realms_idx = content.find('realms = {')
    if realms_idx >= 0:
        section = content[realms_idx:realms_idx+20000]
        matches = re.findall(r'group = "([^"]+)"', section[:10000])
        print('Groups in realms section:', len(matches))
        for g in matches[:20]:
            print(' -', g)
PY