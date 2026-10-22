import sys
sys.path.insert(0, r'C:\FarmAnalysis')
from woweconomy.ml import MLConfig, DfaLearningEngine
from woweconomy.ml.sentinel import SentinelConfig, SentinelTrainer, retrain_decision
from pathlib import Path

cfg0 = MLConfig()
e = DfaLearningEngine(cfg0)
e.load()
frame = None
# we need frame, let's build it
from woweconomy.ml.pipeline import DfaLearningEngine as OldEngine
# Actually let's just use the engine we have
e = DfaLearningEngine(cfg0)
e.load()
frame, _, _ = e.build_rows()

# Check retrain decision
retrain, reason = retrain_decision(cfg0, frame, int(frame["ts"].max()))
print(f'Retrain decision: {retrain}')
print(f'Reason: {reason}')

# Check current artifact
artifact, reason = SentinelTrainer._load_artifact(cfg0.models_root)
if artifact:
    version = artifact.version
    print(f'Current artifact: {version}')
    # Find price target
    for key, entry in (artifact.manifest.get('targets') or {}).items():
        if 'price' in key.lower():
            status = entry.get('status', '')
            reason = entry.get('reason', '')
            print(f'Price status: {status}')
            print(f'Price reason: {reason}')
else:
    print('No artifact loaded')
PY