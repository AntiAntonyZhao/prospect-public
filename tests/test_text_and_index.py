from __future__ import annotations

from pathlib import Path

import pytest

from prospect.factbank.alias_index import AliasIndex, norm_term
from prospect.factbank.metrics import metric_tokens
from prospect.factbank.version import blob_hash
from prospect.text import (
    content_hash,
    detect_language,
    html_to_text,
    norm_company,
    norm_title,
)
from prospect.yamlio import load_yaml_text


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("closing 120+ tickets and resolving 45+ bugs", ["120+", "45+"]),
        ("Reduced build time by ~40% by caching", ["~40%"]),
        ("a local 7B-parameter model", ["7B"]),
        ("0.912 AUC on the validation set", ["0.912"]),
        ("88.4% precision vs 81.2% for the baseline", ["88.4%", "81.2%"]),
        ("an 8-person team, 2-second limit", ["8", "2"]),
        ("GPT-4-powered tool on STL10 stored in S3", []),
        ("scaled to 3x throughput and 1,000 users", ["3x", "1,000"]),
    ],
)
def test_metric_tokens(text, tokens):
    assert metric_tokens(text) == tokens


def test_yaml_12_keeps_on_a_string():
    assert load_yaml_text("p: [QC, ON]\nq: yes\n") == {"p": ["QC", "ON"], "q": "yes"}


def test_alias_index(fb):
    idx = AliasIndex.build(fb)
    assert idx.lookup("Python").kind == "exact"
    assert idx.lookup("python3").skill_id == "python"  # canonical spelling
    assert idx.lookup("Web-Sockets").kind == "exact"  # punctuation-insensitive
    m = idx.lookup("object-oriented programming")
    assert (m.kind, m.skill_id, m.alias) == ("alias", "java", "object-oriented programming")
    assert idx.lookup("Golang").kind == "alias"
    assert idx.lookup("Kafka").kind == "adjacent" and idx.lookup("Kafka").skill_id == "websockets"
    assert idx.lookup("Haskell") is None
    assert norm_term("C++") == "c++" and norm_term("Node.js") == "nodejs"


def test_html_to_text_handles_escaped_markup():
    text = html_to_text("&lt;p&gt;Hello&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;")
    assert "Hello" in text and "• Python" in text


def test_title_and_company_norm():
    assert norm_title("Senior Software Engineer II (Toronto) - Remote") == "software engineer"
    assert norm_title("Software Engineer, New Grad 2026") == "software engineer"
    assert norm_company("Acme Commerce Inc.") == "acme commerce"
    assert content_hash("A Inc", "Dev (Montreal)", "x") == content_hash("a", "Dev - Remote", " X ")


def test_detect_language():
    en = "We are looking for a developer to join our team and work on the platform with you."
    fr = (
        "Nous cherchons un développeur pour notre équipe. Vous serez responsable de la "
        "plateforme et des projets avec les clients dans une équipe de travail."
    )
    assert detect_language(en * 3) == "en"
    assert detect_language(fr * 3) == "fr"
    assert detect_language((en + " " + fr) * 3) == "bilingual"


def test_blob_hash_matches_git(tmp_path: Path):
    f = tmp_path / "x.txt"
    f.write_bytes(b"hello\n")
    assert blob_hash(f) == "ce013625030ba8dba906f756967f9e9ca394464a"  # git hash-object
    f.write_bytes(b"hello\r\n")
    assert blob_hash(f) == "ce013625030ba8dba906f756967f9e9ca394464a"
