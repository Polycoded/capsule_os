import subprocess
import sys
import time


children = []
for _ in range(3):
    children.append(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"]))

time.sleep(20)
for child in children:
    child.terminate()
