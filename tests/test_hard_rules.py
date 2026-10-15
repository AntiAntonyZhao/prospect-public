"""Hard rule 1 as executable checks: no pipeline code writes the fact bank.

A static scan lists every call in the package that can write a file and fails on any
site not reviewed below; a runtime spy runs every stage and fails on any write-mode
open() of the fact bank.
"""

from __future__ import annotations

import ast
import builtins
import io
from pathlib import Path

import numpy as np
import pytest

import prospect
from tests.conftest import add_posting, extraction_input

PKG = Path(prospect.__file__).parent
PATH_WRITES = {"write_text", "write_bytes"}
MODULE_WRITES = {
    ("os", "replace"),
    ("os", "rename"),
    ("shutil", "copyfile"),
    ("shutil", "copy"),
    ("shutil", "copy2"),
    ("shutil", "move"),
}


def _write_sites(path: Path) -> list[tuple[int, str]]:
    """Every call in a module that can write a file: open() in a write mode, Path writes,
    shutil copies/moves, os.replace/rename, and `*.dump(obj, stream)`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name) and fn.id == "open":
            mode = (
                node.args[1]
                if len(node.args) > 1
                else next((k.value for k in node.keywords if k.arg == "mode"), None)
            )
            if isinstance(mode, ast.Constant) and set(str(mode.value)) & set("wax+"):
                sites.append((node.lineno, ast.unparse(node)))
        elif isinstance(fn, ast.Attribute):
            owner = fn.value.id if isinstance(fn.value, ast.Name) else None
            if (
                fn.attr in PATH_WRITES
                or (owner, fn.attr) in MODULE_WRITES
                or (fn.attr == "dump" and len(node.args) >= 2)
            ):
                sites.append((node.lineno, ast.unparse(node)))
    return sites


def test_rule1_no_module_writes_the_factbank():
    """Every write site in the package must be one of the known, non-factbank targets
    below; a new write site fails this test until it is reviewed and listed."""
    allowed = {
        # eval export: a new .md label file only (refuses other suffixes and existing files)
        ("eval/labelfile.py", "path.open('x', encoding='utf-8')"),
        # `prospect demo --record`: demo/responses/<stage>.json only
        (
            "demo.py",
            "(self.directory / f'{stage}.json').write_text("
            "json.dumps(doc, indent=1, ensure_ascii=False) + '\\n', encoding='utf-8')",
        ),
    }
    offenders = []
    for path in PKG.rglob("*.py"):
        rel = path.relative_to(PKG).as_posix()
        for line, src in _write_sites(path):
            if (rel, src) not in allowed:
                offenders.append(f"{rel}:{line}: {src}")
    assert offenders == [], "unreviewed write sites:\n" + "\n".join(offenders)


class _Embedder:
    name = "fake-embedder"

    def encode(self, texts):
        out = []
        for t in texts:
            rng = np.random.default_rng(abs(hash(t)) % (2**32))
            v = rng.normal(size=16).astype(np.float32)
            out.append(v / np.linalg.norm(v))
        return np.stack(out)


@pytest.fixture
def guarded_io(monkeypatch, settings):
    """Record every open(); fail on any write-mode open of the fact bank."""
    events: list[tuple[str, str]] = []
    real_open = builtins.open
    fb_path = settings.factbank_path.resolve()

    def spy(file, mode="r", *a, **kw):
        if isinstance(file, str | Path):
            p = Path(file).resolve()
            events.append((str(p), mode))
            if p == fb_path and set(mode) & set("wax+"):
                raise AssertionError(f"factbank.yaml opened for writing ({mode})")
        return real_open(file, mode, *a, **kw)

    monkeypatch.setattr(builtins, "open", spy)
    monkeypatch.setattr(io, "open", spy)
    return events


def test_rule1_runtime_every_stage_leaves_the_factbank_untouched(
    settings, conn, fb, fake_llm, guarded_io
):
    from prospect.pipeline.extract import run_extract
    from prospect.pipeline.filter import full_pass, prepass
    from prospect.pipeline.rank import run_rank
    from prospect.pipeline.score import run_score

    before = settings.factbank_path.read_bytes()
    add_posting(conn)

    def responder(p):
        name = p["tools"][0]["name"]
        if name == "record_posting":
            return extraction_input()
        return {
            "labels": [
                {
                    "index": 1,
                    "soft": False,
                    "tier": 2,
                    "t3_reason": None,
                    "matched_node": None,
                    "evidence": [],
                    "est_hours": 4,
                    "nearest_node": "websockets",
                    "rationale": "r",
                }
            ]
        }

    client, _ = fake_llm(responder)
    prepass(settings, conn, fb)
    run_rank(settings, conn, fb, embedder=_Embedder())
    run_extract(settings, conn, mode="sync", client=client)
    full_pass(settings, conn, fb)
    run_score(settings, conn, fb, client=client)
    assert any(Path(p) == settings.factbank_path.resolve() for p, _ in guarded_io)
    assert settings.factbank_path.read_bytes() == before
