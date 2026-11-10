import time


blocks = []
for _ in range(64):
    blocks.append(bytearray(1024 * 1024))
    time.sleep(0.1)

time.sleep(10)
