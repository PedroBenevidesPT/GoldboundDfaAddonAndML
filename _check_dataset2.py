import sys
import re
sys.path.insert(0, r'C:\FarmAnalysis')

with open(r'C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns\DFA_Estimate\Data\DFA_Estimate_Data.lua', 'r') as f:
    content = f.read()

# Find all realms
matches = re.findall(r'\["([^"]+)"\] = {', content)
print('Found', len(matches), 'realms')

# Check Dragonmaw specifically
dragonmaw_idx = content.find('Dragonmaw')
if dragonmaw_idx >= 0:
    section = content[dragonmaw_idx:dragonmaw_idx+5000]
    items = re.findall(r'\[(\d+)\] = {', section)
    print('Dragonmaw items:', len(items))
    for item in items[:5]:
        print('  item:', item)