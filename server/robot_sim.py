"""Glassistant · Modal Sandbox protocol runner.

Simulates server/data/robot_protocol.py (written by server/robot.py)
with opentrons_simulate inside an isolated Modal Sandbox.

Run from the repo root:
  modal run server/robot_sim.py
"""

from pathlib import Path

import modal

PROTO_FILE = Path(__file__).resolve().parent / "data" / "robot_protocol.py"

app = modal.App("glassistant-robot-sim")
image = modal.Image.debian_slim(python_version="3.12").pip_install("opentrons")


@app.local_entrypoint()
def main():
    code = PROTO_FILE.read_text()
    sb = modal.Sandbox.create(app=app, image=image, timeout=300)
    try:
        script = (
            "cat > /tmp/protocol.py <<'PROTO'\n" + code + "\nPROTO\n"
            "opentrons_simulate /tmp/protocol.py"
        )
        proc = sb.exec("bash", "-c", script)
        proc.wait()
        print("exit code:", proc.returncode)
        print(proc.stdout.read())
        err = proc.stderr.read()
        if err.strip():
            print("--- stderr ---")
            print(err)
    finally:
        sb.terminate()
