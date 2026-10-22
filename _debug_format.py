import sys
import re
sys.path.insert(0, r'C:\FarmAnalysis')

with open(r'C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns\DFA_Estimate\Data\DFA_Estimate_Data.lua', 'r') as f:
    content = f.read()

# Find the realms section
realms_idx = content.find('realms = {')
if realms_idx >= 0:
    section = content[realms_idx:]
    
    # Print first 2000 chars to see format
    print(section[:2000])
PY