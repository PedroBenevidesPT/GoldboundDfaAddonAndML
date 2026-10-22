import sys
import re
sys.path.insert(0, r'C:\FarmAnalysis')

with open(r'C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns\DFA_Estimate\Data\DFA_Estimate_Data.lua', 'r') as f:
    content = f.read()

# Count items per realm
for realm in ['Dragonmaw', 'Silvermoon', 'Argent Dawn', 'Auchindoun', 'Defias Brotherhood', 'Draenor', 'Kazzak', 'Silvermoon', 'Twisting Nether', 'Antonidas']:
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
            print('  {}: {} items'.format(realm, len(items)))
PY