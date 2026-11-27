from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"m10"))
from fixture_m10 import NOW, make_case, advance
