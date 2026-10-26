"""
Glassistant · Modal Sandbox smoke test
Runs a small Opentrons Flex protocol inside an isolated Modal Sandbox
with opentrons_simulate, and prints the run log.

Run from the repo root:
  modal run server/sandbox_test.py
"""

import modal

PROTOCOL = '''
from opentrons import protocol_api

requirements = {"robotType": "Flex", "apiLevel": "2.20"}

def run(protocol: protocol_api.ProtocolContext):
    tips = protocol.load_labware("opentrons_flex_96_tiprack_1000ul", "C1")
    res = protocol.load_labware("nest_12_reservoir_15ml", "D1")
    plate = protocol.load_labware("corning_96_wellplate_360ul_flat", "D2")
    protocol.load_trash_bin("A3")
    p = protocol.load_instrument("flex_1channel_1000", "left", tip_racks=[tips])
    for well in ["B2", "B3", "B4", "B5", "B6"]:   # e12: B2-B6 done
        p.transfer(50, res["A1"], plate[well])
'''

app = modal.App("glassistant-sandbox-test")
image = modal.Image.debian_slim(python_version="3.12").pip_install("opentrons")


@app.local_entrypoint()
def main():
    sb = modal.Sandbox.create(app=app, image=image, timeout=300)
    try:
        script = (
            "cat > /tmp/protocol.py <<'PROTO'\n" + PROTOCOL + "\nPROTO\n"
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
