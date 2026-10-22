import pathlib
p = pathlib.Path(r'C:\FarmAnalysis\scripts\watch.py')
content = p.read_text()
idx = content.find('def derive_undercut')
if idx >= 0:
    lines = content.split('\n')
    for i in range(idx, min(idx+60, len(lines))):
        print(f'{i+1}: {lines[i]}')
else:
    print('NOT FOUND')
PY