import sys
import re
sys.path.insert(0, r'C:\FarmAnalysis')

with open(r'C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns\DFA_Estimate\Data\DFA_Estimate_Data.lua', 'r') as f:
    content = f.read()

# Find all realms
realms = re.findall(r'\["([^\"]+)"\]\s*=\s*\{', content)
print('Realms found:', len(realms))
for r in realms:
    print('  -', r)

# Count items per realm
for realm in realms:
    realm_idx = content.find('[' + repr(realm) + ']')
    if realm_idx >= 0:
        realm_start = content.find('items\s*=\s*\{', content.find('[' + repr(realm) + ']'))
        if realm_start >= 0:
            realm_end = content.find('\t\t\t\t}', realm_start)
            if realm_end < 0:
                realm_end = content.find('\t\t}', realm_start)
            if realm_end < 0:
                realm_end = realm_start + 5000
            realm_section = content[realm_start:realm_end]
            items = re.findall(r'\[(\d+)\]\s*=\s*\{', realm_section)
            print(f'  {realm}: {len(items)} items')
PY