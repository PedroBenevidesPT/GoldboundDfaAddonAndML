"""Check CSV for malformed rows."""
import csv, requests

resp = requests.get('https://public-data.tradeskillmaster.com/retail/eu/region/items.csv', timeout=30)
text = resp.content.decode('utf-8-sig')
lines = text.splitlines()

print(f"Total lines: {len(lines)}")
print(f"Header: {lines[0][:200]}")

# Check for cells that can't be parsed as float where expected
header = lines[0].split(',')
print(f"Columns: {header}")

# Find rows where marketValue isn't a valid float
for i, line in enumerate(lines[1:], start=2):
    parts = line.split(',')
    if len(parts) >= 3:
        try:
            float(parts[2])
        except ValueError:
            print(f"Row {i} (line {i}): marketValue={repr(parts[2])} — {line[:200]}")
            break

# Check itemId field too
for i, line in enumerate(lines[1:], start=2):
    parts = line.split(',')
    if not parts[0].isdigit():
        print(f"Row {i}: non-numeric itemId={repr(parts[0])} — {line[:200]}")
        break
