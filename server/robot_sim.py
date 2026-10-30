"""Glassistant · Modal Sandbox protocol runner (ROBOT_SIM=modal).

Runs server/robot_runner.py on one protocol inside an isolated Modal
Sandbox and prints the runner's JSON line. The default simulator is local
(server/handoff.py); this is the switch.

  modal run server/robot_sim.py --code-path /path/to/protocol.py
"""

from pathlib import Path

import modal

RUNNER = Path(__file__).resolve().parent / "robot_runner.py"

app = modal.App("glassistant-robot-sim")
image = modal.Image.debian_slim(python_version="3.12").pip_install("opentrons")


@app.local_entrypoint()
def main(code_path: str):
    code = Path(code_path).read_text()
    sb = modal.Sandbox.create(app=app, image=image, timeout=300)
    try:
        script = (
            "cat > /tmp/runner.py <<'RUNNER'\n" + RUNNER.read_text() + "\nRUNNER\n"
            "cat > /tmp/protocol.py <<'PROTO'\n" + code + "\nPROTO\n"
            "cd /tmp && python runner.py protocol.py"
        )
        proc = sb.exec("bash", "-c", script)
        proc.wait()
        print(proc.stdout.read())
    finally:
        sb.terminate()
