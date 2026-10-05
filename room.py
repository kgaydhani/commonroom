"""Run the `room` CLI without installing: python D:/Dev/commonroom/room.py <command>."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from commonroom.cli import main  # noqa: E402

sys.exit(main())
