import sys
import re
sys.path.insert(0, r'C:\FarmAnalysis')

with open(r'C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns\DFA_Estimate\Data\DFA_Estimate_Data.lua', 'r') as f:
    content = f.read()

# Find the realms section
realms_idx = content.find('realms = {')
if realms_idx >= 0:
    section = content[realms_idx:]
    
    # Count items per realm using a more robust method
    # Find all realm blocks
    realm_blocks = re.split(r'\n\s*\["([^"]+)"\] = \{', section)[1:]
    
    for i, block in enumerate(realm_blocks):
        # Extract realm name from the split
        realm_name = re.split(r'\] = \{', block)[0]
        # Count items in this block
        items = re.findall(r'\[(\d+)\] = {', block)
        if items:
            print('{} : {} items'.format(realm_name, len(items)))
PY