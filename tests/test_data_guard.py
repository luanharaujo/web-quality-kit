#!/usr/bin/env python3
"""Proof of the data guard: the bad case is caught, the good case passes,
and no value ever appears in the output (only masked and hashed).

The made-up values are assembled here at run time, so the repository itself
holds no text with the shape of personal data."""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(HERE, "..", "scripts", "data_guard.py")


def cpf(base):
    """A made-up CPF with valid check digits, formatted."""
    d = [int(c) for c in base]
    for n in (9, 10):
        s = sum(d[i] * (n + 1 - i) for i in range(n))
        d.append(s * 10 % 11 % 10)
    t = "".join(map(str, d))
    return f"{t[:3]}.{t[3:6]}.{t[6:9]}-{t[9:]}"


AREA, MOBILE = "61", "9" + "8123" + "4567"
BAD_VALUES = {
    "cpf": cpf("111444777"),
    "phone": f"({AREA}) {MOBILE[:5]}-{MOBILE[5:]}",
    "link": "https://wa.me/" + "55" + AREA + MOBILE,
    "email": "someone.test" + "@" + "made-up-provider.com.br",
}


def write(folder, files):
    os.makedirs(folder, exist_ok=True)
    for name, text in files.items():
        with open(os.path.join(folder, name), "w") as f:
            f.write(text)


def run(root, *extra):
    out = subprocess.run([sys.executable, GUARD, "--root", root, *extra],
                         capture_output=True, text=True, check=True).stdout
    return out, json.loads(out)


def main():
    tmp = tempfile.mkdtemp()
    bad, good = os.path.join(tmp, "bad"), os.path.join(tmp, "good")
    v = BAD_VALUES
    write(bad, {
        "contact.html": f"<p>CPF {v['cpf']}</p>\n<p>Phone {v['phone']}</p>\n"
                        f"<a href=\"{v['link']}\">talk</a>\n<p>{v['email']}</p>\n",
        "members.csv": f"name,phone\nSomeone,{AREA}{MOBILE}\n",
    })
    write(good, {
        "test.html": "<p>Invalid CPF " + "123.456.789-" + "00</p>\n"
                     "<p>Test number 55" + AREA + "9000" + "0001</p>\n"
                     "<p>contact@example.com, 55" + AREA + "test-id</p>\n"
                     "<p>Version 1.0.0.0-0 is not a CPF</p>\n",
        "package.json": '{"name": "x"}\n',
    })

    raw, found = run(bad)
    kinds = sorted(f["kind"] for f in found)
    assert kinds.count("cpf") == 1, kinds
    assert kinds.count("phone") == 3, kinds  # html twice (formatted, wa.me link) and the csv
    assert "email" in kinds and "data-file" in kinds, kinds
    for value in (v["cpf"], v["phone"], AREA + MOBILE, v["email"]):
        assert value not in raw, "a value leaked in the output"

    _, clean = run(good)
    assert clean == [], clean

    # Allow list: a public value, by hash.
    allow = os.path.join(tmp, "allow")
    with open(allow, "w") as f:
        f.write("\n".join(x["hash"] + "  # test" for x in found) + "\n")
    _, allowed = run(bad, "--allow", allow)
    assert allowed == [], allowed
    print("data_guard: ok")


if __name__ == "__main__":
    main()
