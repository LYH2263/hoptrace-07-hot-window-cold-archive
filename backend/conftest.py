import os
import tempfile
from pathlib import Path

# 测试隔离：强制使用临时数据目录，避免触碰真实 hoptrace.db；
# 必须在任何 app.* 导入之前生效（app.db 在导入时读取环境变量）。
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="hoptrace-test-")
os.environ["FIXTURES_DIR"] = str(Path(__file__).resolve().parent.parent / "fixtures")
