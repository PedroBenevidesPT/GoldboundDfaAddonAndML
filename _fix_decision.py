import sys
sys.path.insert(0, r'C:\FarmAnalysis')

# Read the file
with open('woweconomy/ml/decision_engine.py', 'r') as f:
    content = f.read()

# Replace the action line
old = '    action: str  # one of: POST, MATCH, UNDERCUT, MAINTAIN, CANCEL_AND_RELIST, EXPIRE, LIQUIDATE, DO_NOT_LIST, REVIEW_REQUIRED'
new = '    action: str = "REVIEW_REQUIRED"  # conservative default; set by decide() logic'
content = content.replace(old, new)

with open('woweconomy/ml/decision_engine.py', 'w') as f:
    f.write(content)
print('done')
PY