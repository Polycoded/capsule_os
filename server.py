from pathlib import Path
import os
import sys


LOCAL_DEPENDENCIES = Path(__file__).resolve().parent / ".deps"
if LOCAL_DEPENDENCIES.exists():
    sys.path.insert(0, str(LOCAL_DEPENDENCIES))

from capsuleos.server import main


if __name__ == "__main__":
    if "--port" in sys.argv:
        port_index = sys.argv.index("--port") + 1
        if port_index < len(sys.argv):
            os.environ["CAPSULEOS_PORT"] = sys.argv[port_index]
    main()
