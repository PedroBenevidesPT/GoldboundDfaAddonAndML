import sys
import re
sys.path.insert(0, r'C:\FarmAnalysis')

with open(r'C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns\DFA_Estimate\Data\DFA_Estimate_Data.lua', 'r') as f:
    content = f.read()

# Find the realms section
realms_idx = content.find('realms = {')
if realms_idx >= 0:
    section = content[realms_idx:]
    
    # Find all realm names
    realms = re.findall(r'\["([^"]+)"\] = {', section)
    print('Total realms found:', len(realms))
    for r in realms[:20]:
        print(' -', r)
    
    # Check for specific realms
    for realm in ['Dragonmaw', 'Silvermoon', 'Argent Dawn']:
        if realm in realms:
            print('{} found in realms list'.format(realm))
        else:
            print('{} NOT found in realms list'.format(realm))
            # Check partial matches
            for r in realms:
                if realm.lower() in r.lower():
                    print('  Partial match: {}'.format(r))
PY