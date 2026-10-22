import sys
import re
sys.path.insert(0, r'C:\FarmAnalysis')

with open(r'C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns\DFA_Estimate\Data\DFA_Estimate_Data.lua', 'r') as f:
    content = f.read()

# Find the realms section
realms_idx = content.find('realms = {')
if realms_idx >= 0:
    section = content[realms_idx:]
    print('Section length:', len(section))
    print('First 500 chars:', section[:500])
    
    # Check for each realm
    for realm in ['Dragonmaw', 'Silvermoon', 'Argent Dawn']:
        realm_idx = section.find('[' + repr(realm) + ']')
        print('{} realm_idx: {}'.format(realm, realm_idx))
        if realm_idx >= 0:
            items_idx = section.find('items = {', realm_idx)
            print('  items_idx:', items_idx)
            if items_idx >= 0:
                end_idx = section.find('        }', items_idx)
                print('  end_idx:', end_idx)
                if end_idx < 0:
                    end_idx = items_idx + 10000
                realm_section = section[items_idx:end_idx]
                items = re.findall(r'\[(\d+)\] = {', realm_section)
                print('  {}: {} items'.format(realm, len(items)))
PY