from pathlib import Path

ROOT = Path(__file__).parent.parent
DATA_PATH = ROOT / "data"

def getDataPath(filename):
    return DATA_PATH / filename