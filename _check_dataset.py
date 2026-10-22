import sys
import re
sys.path.insert(0, r'C:\FarmAnalysis')

# Check the DFA_Estimate dataset
with open(r'C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns\DFA_Estimate\Data\DFA_Estimate_Data.lua', 'r') as f:
    content = f.read()

# Count realms
realm_matches = re.findall(r'\["([^"]+)"\].*?items\s*=\s*\{', content)
print('Realms in dataset:', len(realm_matches))
print('Realms:', realm_matches)

# Count items in Dragonmaw
dragonmaw_idx = content.find('Dragonmaw')
if dragonmaw_idx >= 0:
    dragonmaw_section = content[dragonmaw_idx:dragonmaw_idx+5000]
    items = re.findall(r'\[(\d+)\]\s*=\s*\{', dragonmaw_section)
    print('Dragonmaw items:', len(items))

    # Check for price fields
    item_section = dragonmaw_section[:2000]
    has_price = 'price' in item_section
    has_plow = 'plow' in item_section
    has_phigh = 'phigh' in item_section
    has_conf = 'conf' in item_section
    has_psell = 'psell' in item_section
    print('Has price:', has_price)
    print('Has plow:', has_plow)
    print('Has phigh:', has_phigh)
    print('Has conf:', has_conf)
    print('Has psell:', has_psell)
PY