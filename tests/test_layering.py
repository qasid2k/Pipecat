"""The layering invariants, actually checked.

`architecture.md` has described these as "machine-checked" since the modular
refactor, but no checker was ever committed -- the claim rested on someone
having run a grep once. A rule nobody enforces is a rule that decays silently,
and these two are the ones holding the whole design together:

    1. Nothing outside engine/ imports Pipecat.
    2. core/ imports no vendor and no engine.

Rule 1 is what makes a second conversation engine a contained job rather than a
rewrite. Rule 2 is what stops the contracts depending on the things that are
supposed to depend on THEM.

The check parses imports with `ast` rather than grepping text, so a commented-out
import or the word "pipecat" in a docstring does not trip it, and
`import pipecat.services.deepgram as x` does.

    python -m unittest discover -s tests -t . -v
"""

import ast
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Modules that are development scratch tools, not part of the running service.
# They are allowed to do as they please; they are not imported by anything.
EXCLUDED = {
    "audiosocket_server.py",  # superseded standalone echo server
    "ari_test.py",
    "ari_media_test.py",
    "nettest_client.py",
    "nettest_server.py",
    "bot_backup.py",
}


def python_files() -> list[Path]:
    """Every source file that is part of the service."""
    found = []
    for path in REPO.rglob("*.py"):
        rel = path.relative_to(REPO)
        parts = set(rel.parts)
        if parts & {".venv", "__pycache__", "build", "dist", "tests"}:
            continue
        if rel.name in EXCLUDED:
            continue
        found.append(path)
    return found


def imported_roots(path: Path) -> set[str]:
    """Top-level package names imported by a file, via the AST.

    Relative imports (`from . import x`) have no root package and are ignored --
    they cannot reach outside their own package, which is the thing being
    policed here.
    """
    # utf-8-sig, not utf-8: transports/audiosocket.py carries a UTF-8 BOM (a
    # PowerShell redirect somewhere in its history). Python's own tokenizer
    # strips it, so the module imports fine, but `ast.parse` on a plain utf-8
    # read chokes on the U+FEFF.
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


class LayeringTest(unittest.TestCase):
    def test_only_the_engine_package_imports_pipecat(self):
        """Rule 1: Pipecat is quarantined inside engine/.

        If this fails, the Engine interface has stopped being a real seam and a
        second engine implementation is no longer a contained change.
        """
        offenders = []
        for path in python_files():
            rel = path.relative_to(REPO)
            if rel.parts[0] == "engine":
                continue  # the one package allowed to know Pipecat exists
            if "pipecat" in imported_roots(path):
                offenders.append(str(rel))

        self.assertEqual(
            offenders, [],
            "these files import Pipecat but live outside engine/: "
            f"{offenders}. Pipecat must stay behind the Engine interface.",
        )

    def test_core_imports_no_vendor_and_no_engine(self):
        """Rule 2: core/ holds the contracts, so it depends on nobody.

        core/ may use the standard library, a small number of declared
        third-party helpers, and other core modules. It must never import an
        adapter (transports/, ari_controller), an engine (engine/, pipecat), or
        the factory that builds them -- that would invert the dependency the
        layering exists to establish.
        """
        # Declared rather than inferred, so adding a dependency to core/ is a
        # deliberate act that shows up in this list and in review.
        allowed = {
            "__future__", "abc", "asyncio", "dataclasses", "os", "pathlib",
            "sys", "typing", "collections", "enum", "json", "datetime",
            "signal", "socket", "threading", "time", "uuid",
            "yaml", "dotenv",   # the config loader's two third-party helpers
            # loguru is allowed here for core/logging.py, which exists to
            # CONFIGURE it. It is a logging library, not a telephony vendor or a
            # conversation engine, so it does not invert the dependency this rule
            # protects -- but it is listed rather than assumed, because that is
            # the point of an allow-list.
            "loguru",
            "core",             # core modules may use each other
        }
        forbidden_hits = []
        for path in sorted((REPO / "core").rglob("*.py")):
            for root in sorted(imported_roots(path)):
                if root not in allowed:
                    forbidden_hits.append(f"{path.relative_to(REPO)} imports {root}")

        self.assertEqual(
            forbidden_hits, [],
            "core/ must not depend on adapters, engines or factories: "
            f"{forbidden_hits}",
        )

    def test_the_pool_and_the_call_loop_stay_engine_agnostic(self):
        """The pool and the wiring must know Engine, never PipecatEngine.

        This is the invariant the multi-agent pool project was built on: the pool
        hands out personas and the call loop builds engines through a factory, so
        neither needs to know what an engine actually is.
        """
        for name in ("core/pool.py", "bot.py"):
            path = REPO / name
            self.assertNotIn(
                "pipecat", imported_roots(path), f"{name} imports Pipecat directly"
            )
            # Look for real references in the CODE, via the AST -- a plain text
            # search also matches comments, and bot.py legitimately explains in
            # a comment why PipecatEngine.__init__ being trivial makes the
            # startup warm-up safe.
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            named = {
                node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
            } | {
                node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
            }
            self.assertNotIn(
                "PipecatEngine", named,
                f"{name} references PipecatEngine in code -- it should only know "
                "the Engine interface and the factory.",
            )


if __name__ == "__main__":
    unittest.main()
