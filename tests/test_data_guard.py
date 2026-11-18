#!/usr/bin/env python3
"""Proof of the data guard: the bad case is caught, the good case passes,
and no value ever appears in the output (only masked and hashed)."""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(HERE, "..", "scripts", "data_guard.py")


def run(case, *extra):
    out = subprocess.run([sys.executable, GUARD, "--root", os.path.join(HERE, "casos", "data_guard", case), *extra],
                         capture_output=True, text=True, check=True).stdout
    return out, json.loads(out)


def main():
    raw, bad = run("ruim")
    kinds = sorted(f["kind"] for f in bad)
    assert kinds.count("cpf") == 1, kinds
    assert kinds.count("phone") == 3, kinds  # html twice (formatted, wa.me link) and the csv
    assert "email" in kinds and "data-file" in kinds, kinds
    for secret in ("CPF-REMOVED", "98123-4567", "PHONE-REMOVED", "someone@example.com"):
        assert secret not in raw, f"value leaked in the output: {secret}"

    _, good = run("bom")
    assert good == [], good

    # Allow list: a public value, by hash.
    allow = os.path.join(HERE, "casos", "allow.tmp")
    with open(allow, "w") as f:
        f.write("\n".join(x["hash"] + "  # test" for x in bad) + "\n")
    try:
        _, allowed = run("ruim", "--allow", allow)
        assert allowed == [], allowed
    finally:
        os.remove(allow)
    print("data_guard: ok")


if __name__ == "__main__":
    main()
