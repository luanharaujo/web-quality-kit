#!/usr/bin/env python3
"""Proofs for scripts/interaction_gate.py: route scan, coverage, new vs baseline."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
import interaction_gate as g  # noqa: E402

SERVER = """
app.post('/api/bookings', h)
router.delete("/api/bookings/:id", h)
const routes = [{ match: route('PUT', 'videos/#id/result'), run: f }]
if (method === 'PATCH' && url.pathname === '/api/profile') {}
if (m === 'POST' && parts.length === 2) {}
await fetch(x, { method: 'POST' })  // outbound call, not a route
"""


class Normalize(unittest.TestCase):
    def test_ids_become_params(self):
        self.assertEqual(g.route_key("post", "/api/classes/42/bookings?x=1"), "POST /api/classes/:id/bookings")
        self.assertEqual(g.normalize_path("/a/{id}/b/[slug]/#n"), "/a/:id/b/:id/:id")

    def test_suffix_match(self):
        self.assertTrue(g.key_matches("POST /app/api/bookings", "POST /api/bookings"))
        self.assertTrue(g.key_matches("DELETE /api/bookings/:id", "DELETE /api/bookings/:id"))
        self.assertFalse(g.key_matches("POST /api/bookings", "PUT /api/bookings"))
        self.assertFalse(g.key_matches("POST /bookings", "POST /api/bookings"))

    def test_same_rules_as_the_fixture(self):
        js = (HERE.parent / "interaction" / "routes.mjs").as_uri()
        cases = ["/api/classes/42/bookings?x=1", "/a/{id}/b/[slug]", "/x/0123456789abcdef0123", "/u/abc"]
        script = f"import {{ routeKey }} from '{js}'; console.log(JSON.stringify({json.dumps(cases)}.map(p => routeKey('post', p))))"
        out = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(out.stdout), [g.route_key("post", p) for p in cases])


class Words(unittest.TestCase):
    def test_english_defaults_and_app_override(self):
        js = (HERE.parent / "interaction" / "index.mjs").as_uri()
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"success": "\\b(gebucht|booked)\\b", "retry": "(erneut|retry)"}, f)
        script = (f"import {{ DEFAULTS, loadConfig }} from '{js}'; const s = new RegExp(DEFAULTS.success, 'i'), r = new RegExp(DEFAULTS.retry, 'i');"
                  "const c = loadConfig(), cs = new RegExp(c.success, 'i'), cr = new RegExp(c.retry, 'i');"
                  "console.log(JSON.stringify([s.test('Slot booked'), s.test('Book'), r.test('Try again'), cs.test('Termin gebucht'), cr.test('Erneut senden'), c.holdMs]))")
        env = dict(os.environ, KIT_INTERACTION_CONFIG=f.name)
        out = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, check=True, env=env)
        self.assertEqual(json.loads(out.stdout), [True, False, True, True, True, 2000])


class Scan(unittest.TestCase):
    def test_finds_routes_and_unnamed_handlers(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "server.ts").write_text(SERVER)
            Path(d, "functions", "api", "items").mkdir(parents=True)
            Path(d, "functions", "api", "items", "[id].js").write_text("export async function onRequestDelete() {}\n")
            Path(d, "server.test.ts").write_text("app.post('/only/in/tests', h)\n")
            out = g.scan([d])
        self.assertEqual(out["routes"], [
            "DELETE /api/bookings/:id", "DELETE /api/items/:id", "PATCH /api/profile", "POST /api/bookings", "PUT /videos/:id/result",
        ])
        self.assertTrue(out["where"]["POST /api/bookings"].endswith("server.ts:2"))
        self.assertEqual(len(out["unmapped"]), 1)  # m === 'POST' && parts.length; the outbound fetch is not counted
        self.assertTrue(out["unmapped"][0].endswith("server.ts:6"))


    def test_other_methods_on_request(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "s.ts").write_text("app.get('/api/list', h)\napp.post('/api/add', h)\n")
            self.assertEqual(g.scan([d])["routes"], ["POST /api/add"])
            self.assertEqual(g.scan([d], ("GET",) + g.WRITE)["routes"], ["GET /api/list", "POST /api/add"])


ROUTES = {"routes": ["POST /api/bookings", "DELETE /api/bookings/:id", "POST /webhooks/sync"], "unmapped": []}


def entries(*items):
    return [dict(zip(("type", "route", "check", "ok"), i)) for i in items]


class Collect(unittest.TestCase):
    def test_coverage(self):
        cur = g.collect(ROUTES, entries(
            ("seen", "POST /app/api/bookings", None, None),
            ("check", "POST /app/api/bookings", "busy", True),
            ("check", "POST /app/api/bookings", "double", False),
            ("contract", "POST /api/bookings", None, True),
            ("contract", "POST /webhooks/sync", None, True),
            ("seen", "POST /api/other", None, None),
        ), {"no_ui": [{"route": "POST /webhooks/sync", "reason": "webhook only"}], "exclude": []})
        self.assertEqual(cur["covered"], ["POST /api/bookings", "POST /webhooks/sync"])
        self.assertEqual(cur["without_e2e"], ["DELETE /api/bookings/:id"])
        # the write the scan missed still needs its contract
        self.assertEqual(cur["without_contract"], ["DELETE /api/bookings/:id", "POST /api/other"])
        self.assertEqual(cur["failing"], ["POST /app/api/bookings double"])
        self.assertEqual(cur["seen_undeclared"], ["POST /api/other"])

    def test_exception_needs_a_reason(self):
        for field in ("exclude", "no_ui", "optimistic", "duplicate_reads_ok", "no_loading_state"):
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                json.dump({field: [{"route": "POST /x"}]}, f)
            with self.assertRaises(SystemExit, msg=field):
                g.load_config(f.name)


def health(**kw):
    base = {"routes": ["POST /a", "POST /b"], "covered": [], "without_e2e": [], "without_contract": [], "failing": [], "unmapped": []}
    base.update(kw)
    return base


class Compare(unittest.TestCase):
    def test_new_repo_any_gap_is_red(self):
        panel, worse = g.compare(None, health(covered=["POST /a"], without_contract=["POST /b"]), "new", "pt")
        self.assertTrue(worse)
        self.assertIn(f"🔴 1 {g.texts('pt')['without_contract']}", panel)
        self.assertNotEqual(g.texts("pt")["without_contract"], g.TEXT["without_contract"])  # translated
        self.assertIn("▓▓▓▓▓░░░░░ 50,0%", panel)

    def test_old_repo_starts_as_baseline(self):
        panel, worse = g.compare(None, health(without_e2e=["POST /a", "POST /b"]), "baseline", "en")
        self.assertFalse(worse)
        self.assertIn("🟡 2", panel)

    def test_old_repo_never_worse(self):
        base = health(without_e2e=["POST /a"])
        _, worse = g.compare(base, health(without_e2e=["POST /a"]), "baseline", "en")
        self.assertFalse(worse)
        panel, worse = g.compare(base, health(without_e2e=["POST /a"], failing=["POST /b double"]), "baseline", "en")
        self.assertTrue(worse)
        self.assertIn("🔴 1 checks failing", panel)
        self.assertIn("**new** `POST /b double`", panel)

    def test_all_covered_is_green(self):
        panel, worse = g.compare(health(), health(covered=["POST /a", "POST /b"]), "new", "en")
        self.assertFalse(worse)
        self.assertIn("🟢 ▓▓▓▓▓▓▓▓▓▓ 100.0% ↑", panel)


if __name__ == "__main__":
    unittest.main(verbosity=1)
