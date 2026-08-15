import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中，使 `import dba_pipeline` 在未安装时也可用
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
