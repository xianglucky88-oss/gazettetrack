import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures" / "input.json"
# 允许测试/脚本指向临时库：GAZETTETRACK_DB 优先于默认路径。
DB_PATH = Path(os.environ.get("GAZETTETRACK_DB", ROOT / "gazettetrack.db"))
