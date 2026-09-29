"""The React web app as the Python server sees it (IMP-011, [[decisions]] 056).

The React code lives in web/ and is tested there (`npm test`). What only the
Python side can check:

  * the committed build in api/static/ matches the source in web/ -- the
    staleness guard. Change a component, forget `npm run build`, and the VM
    would quietly serve the OLD page; this test fails first;
  * the server serves that build: the page, its assets with long cache
    headers, and nothing outside api/static/assets.
"""

import hashlib
import json
import re
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from api.server import ASSET_CACHE, STATIC_DIR, ApiServer
from core.live import Counters, LiveCalls
from core.pool import AgentPool
from tests.test_api import roster

REPO = Path(__file__).resolve().parent.parent
WEB = REPO / "web"
TOP_LEVEL = ["package.json", "package-lock.json", "index.html", "vite.config.ts", "tsconfig.json"]
IS_TEST = re.compile(r"\.test\.(ts|tsx)$")


def source_hash() -> str:
    """Python twin of web/scripts/build-info.mjs -- same files, same order,
    same \\r\\n -> \\n normalisation. Keep the two in step."""
    files = [WEB / f for f in TOP_LEVEL]
    for p in (WEB / "src").rglob("*"):
        rel = p.relative_to(WEB / "src").parts
        if p.is_file() and "test" not in rel[:-1] and not IS_TEST.search(p.name):
            files.append(p)
    h = hashlib.sha256()
    for rel in sorted(p.relative_to(WEB).as_posix() for p in files):
        text = (WEB / rel).read_bytes().decode("utf-8").replace("\r\n", "\n")
        h.update((rel + "\n" + text + "\n").encode("utf-8"))
    return h.hexdigest()


class BuildIsCurrentTest(unittest.TestCase):
    def test_the_committed_build_matches_the_source(self):
        info = json.loads((STATIC_DIR / "build-info.json").read_text(encoding="utf-8"))
        self.assertEqual(
            info["source_hash"], source_hash(),
            "api/static/ is out of date with web/. Run `npm run build` in web/ "
            "and commit api/static/ together with the source change.",
        )

    def test_the_page_loads_only_its_own_files(self):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn("<title>Voice agents</title>", html)
        # Bundled, not fetched: the VM may have no internet.
        self.assertNotRegex(html, r'(src|href)="(https?:)?//')
        for ref in re.findall(r'(?:src|href)="(/assets/[^"]+)"', html):
            self.assertTrue((STATIC_DIR / ref.lstrip("/")).is_file(), ref)

    def test_node_modules_is_never_committed(self):
        ignore = (REPO / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("web/node_modules/", ignore)


class ServingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        server = ApiServer(pool=AgentPool(roster(1)), live=LiveCalls(), counters=Counters())
        # What start() does, without binding a real port.
        server._dashboard = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        app = web.Application()
        app.add_routes([web.get("/", server._dashboard_page),
                        web.get("/assets/{name}", server._asset)])
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_the_page_is_served_and_never_cached(self):
        r = await self.client.get("/")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers["Cache-Control"], "no-cache")
        self.assertIn('<div id="root">', await r.text())

    async def test_assets_are_served_with_a_long_cache(self):
        name = next((STATIC_DIR / "assets").glob("*.js")).name
        r = await self.client.get(f"/assets/{name}")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers["Cache-Control"], ASSET_CACHE)

    async def test_nothing_outside_assets_can_be_fetched(self):
        for bad in ("..%2Fserver.py", "%2E%2E%2F..%2Fbot.py", ".hidden", "nope.js"):
            with self.subTest(bad=bad):
                self.assertEqual((await self.client.get(f"/assets/{bad}")).status, 404)


if __name__ == "__main__":
    unittest.main()
