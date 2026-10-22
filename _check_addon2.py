import sys
sys.path.insert(0, r'C:\FarmAnalysis')
import re, os, pathlib

# Check the addon data file - look at more content
addon_data_path = pathlib.Path(r'C:\Program Files (x86)\World of Warcraft\_retail_\WTF\Account\1186541918#1\SavedVariables\DFA_AuctionIntelligence.lua')
content = open(addon_data_path, 'r', errors='replace').read()

# Search for key patterns
patterns = ['totalObservations', 'meanConfidence', 'sentinelModel', 'price', 'conf', 'psell', 'ttl', 'plow', 'phigh']
for p in patterns:
    # Count occurrences
    count = content.count(p)
    print(f'{p}: {count} occurrences in first 50000 chars')

# Show a sample of the structure
print()
print('=== First 50 lines ===')
lines = content.split('\n')
for i, line in enumerate(lines[:50]):
    print(f'{i+1}: {line}')

# Show last lines
print()
print('=== Last 20 lines ===')
for i, line in enumerate(content.split('\n')[-20:]):
    print(f'{len(content.split(chr(10)))-19+i}: {line}')
PY