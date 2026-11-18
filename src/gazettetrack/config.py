import os
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures" / "input.json"
DB_PATH = ROOT / "gazettetrack.db"

# 附件证据归档根目录：内容寻址 blob + 每个下载批次的独立暂存目录。
# 目录下的数据均为不可信外部内容，只通过本模块的句柄读取，不能作为代码执行。
ARCHIVE_DIR = Path(os.environ.get("GAZETTETRACK_ARCHIVE", ROOT / "archive"))
BLOB_DIR = ARCHIVE_DIR / "blobs"
STAGE_DIR = ARCHIVE_DIR / "staging"
# 附件证据独立 sqlite（与记录库分离：证据只追加、生命周期独立）
ATTACH_DB_PATH = Path(os.environ.get("GAZETTETRACK_ATTACH_DB",
                                     ROOT / "attachments.db"))

# 单个附件槽位在 404/410（待补窗口）下允许的最大尝试次数；超过后判定失败。
MAX_MISSING_GRACE = 3
# 摘要不匹配等硬校验错误允许的最大尝试次数（防止源站修复后无法自愈）。
MAX_HARD_RETRIES = 2
