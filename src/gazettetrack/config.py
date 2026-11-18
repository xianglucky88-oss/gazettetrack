from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures" / "input.json"
DB_PATH = ROOT / "gazettetrack.db"
ENTITY_REGISTRY = ROOT / "entity_registry.json"
