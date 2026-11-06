"""Deterministic tokenizer tests; real local BGE evidence is recorded separately."""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest


class CharacterTokenizer:
    is_fast = True

    def __call__(self, text, *, add_special_tokens=True, **options):
        result = {
            "input_ids": list(range(len(text) + (2 if add_special_tokens else 0)))
        }
        if options.get("return_offsets_mapping"):
            result["offset_mapping"] = [(i, i + 1) for i in range(len(text))]
        return result


@pytest.fixture
def chunker(monkeypatch, workspace_tmp_path):
    from transformers import AutoTokenizer

    from devflow.knowledge.chunking import LocalChunker

    (workspace_tmp_path / "tokenizer.json").write_text('{"test":"character"}')
    monkeypatch.setattr(
        AutoTokenizer, "from_pretrained", lambda *args, **kwargs: CharacterTokenizer()
    )
    return LocalChunker(workspace_tmp_path)


def source(body, path=None):
    from devflow.knowledge.schemas import SourceBytes, SourceIdentity

    repo = uuid4()
    return SourceBytes(
        identity=SourceIdentity(
            repository_id=repo,
            source_repository_id=repo,
            dependency_repository_ids=(repo,),
            document_id=uuid4(),
            revision_hash=hashlib.sha256(body).hexdigest(),
            workspace_id=uuid4() if path else None,
            sha="a" * 40 if path else None,
            path=path,
        ),
        body=body,
    )


def test_chunk_limits_include_prefix_and_special_tokens(chunker):
    item = source(("中文é🙂 abc\r\n" * 180).encode(), "docs/精确.md")
    chunks = chunker.split(item, "标题\r\n原样")
    prefix = '{"path":"docs/精确.md","title":"标题\\r\\n原样"}\n\n'
    covered = 0
    for chunk in chunks:
        assert chunk.embedding_text == prefix + chunk.text
        assert len(chunk.embedding_text) + 2 == chunk.token_count <= 512
        assert len(chunk.embedding_text.encode()) <= 8192
        loc = chunk.location
        assert item.body[loc.start_byte : loc.end_byte].decode() == chunk.text
        assert loc.start_byte <= covered < loc.end_byte
        if loc.start_byte < covered:
            assert len(item.body[loc.start_byte : covered].decode()) <= 64
        covered = loc.end_byte
    assert covered == len(item.body)
    assert chunks == chunker.split(item, "标题\r\n原样")
    with pytest.raises(ValueError, match="prefix"):
        chunker.split(item, "x" * 512)
    with pytest.raises(ValueError):
        chunker.split(item, "bad\0title")
    with pytest.raises(ValueError):
        chunker.split(item, "\ud800")


def test_crlf_and_long_line_roundtrip(chunker):
    raw = ("A\r\n" + "中e\u0301🙂" * 400 + "\nlast\rrow").encode()
    chunks = chunker.split(source(raw), "")
    for draft in chunks:
        loc = draft.location
        assert loc.start_line == raw[: loc.start_byte].count(b"\n") + 1
        assert loc.end_line == raw[: loc.end_byte - 1].count(b"\n") + 1
        assert raw[loc.start_byte : loc.end_byte].decode() == draft.text
    assert any(c.location.start_line == c.location.end_line == 2 for c in chunks)
    assert chunker.split(source(b""), "") == []


def test_near_full_prefix_cannot_allocate_unbounded_drafts(chunker):
    # JSON framing is 26 characters, plus two specials: leave one body character.
    framing = len('{"path":null,"title":""}\n\n')
    with pytest.raises(ValueError, match="chunk_limit"):
        chunker.split(source(b"x" * 10001), "t" * (509 - framing))


def test_byte_cap_and_normalization_gaps_preserve_original(chunker):
    class CompactTokenizer:
        is_fast = True

        def __call__(self, text, *, add_special_tokens=True, **options):
            spans = [(i, min(i + 64, len(text))) for i in range(0, len(text), 64)]
            result = {
                "input_ids": [1] * (len(spans) + (2 if add_special_tokens else 0))
            }
            if options.get("return_offsets_mapping"):
                result["offset_mapping"] = spans
            return result

    chunker.tokenizer = CompactTokenizer()
    raw = ("中🙂e\u0301" * 2500).encode()
    chunks = chunker.split(source(raw), "字节")
    assert len(chunks) > 1
    covered = 0
    for chunk in chunks:
        assert len(chunk.embedding_text.encode()) <= 8192
        assert (
            raw[chunk.location.start_byte : chunk.location.end_byte].decode()
            == chunk.text
        )
        assert chunk.location.start_byte <= covered < chunk.location.end_byte
        covered = chunk.location.end_byte
    assert covered == len(raw)

    class GapTokenizer(CharacterTokenizer):
        def __call__(self, text, **options):
            result = super().__call__(text, **options)
            if options.get("return_offsets_mapping"):
                result["offset_mapping"] = [(0, 0)] * len(text)
            return result

    chunker.tokenizer = GapTokenizer()
    chunks = chunker.split(source(b" \r\n" * 800), "")
    assert b"".join(chunk.text.encode() for chunk in chunks) == b" \r\n" * 800


def test_final_encoding_is_not_sum_of_prefix_and_body(chunker):
    class BoundaryTokenizer(CharacterTokenizer):
        def __call__(self, text, **options):
            result = super().__call__(text, **options)
            if "\n\nx" in text:
                result["input_ids"].extend([7] * 5)
            return result

    chunker.tokenizer = BoundaryTokenizer()
    chunks = chunker.split(source(b"x" * 1300), "")
    assert chunks
    for chunk in chunks:
        assert chunk.token_count == len(chunk.embedding_text) + 7 <= 512


@pytest.mark.parametrize(
    "path,code,reason",
    [
        (name, (), None)
        for name in (
            "README",
            "ReadMe.MD",
            "README.TXT",
            "README.rst",
            "docs/a.MD",
            "docs/x/a.txt",
            "docs/a.RST",
        )
    ]
    + [
        ("src/a" + ext.upper(), ("src",), None)
        for ext in [
            ".py",
            ".pyi",
            ".js",
            ".mjs",
            ".cjs",
            ".jsx",
            ".ts",
            ".tsx",
            ".go",
            ".rs",
            ".java",
            ".kt",
            ".c",
            ".h",
            ".cc",
            ".cpp",
            ".hpp",
            ".cs",
            ".rb",
            ".php",
            ".sh",
            ".sql",
            ".vue",
            ".svelte",
        ]
    ]
    + [
        ("src/" + name + "/a.py", ("src",), "excluded_directory")
        for name in [
            "node_modules",
            "vendor",
            "venv",
            "env",
            "__pycache__",
            "dist",
            "build",
            "out",
            "target",
            "coverage",
            "htmlcov",
        ]
    ]
    + [
        ("src/" + name, ("src",), "excluded_hidden")
        for name in (
            ".git/a.py",
            ".env",
            ".env.prod",
            ".venv/a.py",
            ".next/a.py",
            ".nuxt/a.py",
            ".config/a.py",
        )
    ]
    + [
        ("src/" + name, ("src",), "excluded_credential")
        for name in [
            "id_rsa",
            "id_dsa",
            "id_ecdsa",
            "id_ed25519",
            "credentials",
            "credentials.json",
            "credentials.yml",
            "credentials.yaml",
            "secrets.json",
            "secrets.yml",
            "secrets.yaml",
            "a.pem",
            "a.key",
            "a.p12",
            "a.pfx",
            "a.keystore",
            "a.jks",
        ]
    ]
    + [
        ("src/main", ("src",), "unsupported"),
        ("src/a.exe", ("src",), "unsupported"),
        ("src/a.py", (), "not_selected"),
        ("docs/a.pdf", (), "unsupported"),
        ("../x", (), "unsupported_path"),
        ("x" * 4097, (), "unsupported_path"),
    ],
)
def test_path_rules_v1(path, code, reason):
    from devflow.knowledge.sources import path_reason

    assert path_reason(path, code_paths=code) == reason


def test_workspace_version_hash_preserves_identity():
    from devflow.knowledge.sources import workspace_source_version

    repo, dep, workspace = uuid4(), uuid4(), uuid4()
    values = {
        "repository_id": repo,
        "source_repository_id": dep,
        "dependency_repository_ids": (dep, repo),
        "workspace_id": workspace,
        "sha": "b" * 40,
        "path": "docs/é.md",
    }
    payload = dict(values, version="knowledge-workspace-source-v1")
    payload.update(
        repository_id=str(repo),
        source_repository_id=str(dep),
        dependency_repository_ids=sorted([str(repo), str(dep)]),
        workspace_id=str(workspace),
    )
    expected = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    assert workspace_source_version(**values) == expected
    assert (
        workspace_source_version(
            **(values | {"dependency_repository_ids": (repo, dep, repo)})
        )
        == expected
    )
    assert (
        workspace_source_version(**(values | {"path": "docs/e\u0301.md"})) != expected
    )


def test_local_bge_tokenizer_limits_and_offsets():
    """Actual local tokenizer only, never models/weights/downloads."""
    from devflow.knowledge.chunking import LocalChunker

    model_path = Path("D:/python/models/BAAI/bge-m3")
    if not model_path.is_dir():
        pytest.skip("Local BGE tokenizer fixture is not present")
    chunker = LocalChunker(model_path)
    body = (
        "# 中英 heading\r\n\n代码 snake_case CamelCase src/a.py E123 e\u0301🙂\n" * 160
    ).encode()
    item = source(body, "docs/说明.md")
    chunks = chunker.split(item, "本地")
    covered = 0
    for chunk in chunks:
        encoded = chunker.tokenizer(
            chunk.embedding_text,
            add_special_tokens=True,
            truncation=False,
            padding=False,
        )
        assert len(encoded["input_ids"]) == chunk.token_count <= 512
        assert len(chunk.embedding_text.encode()) <= 8192
        loc = chunk.location
        assert body[loc.start_byte : loc.end_byte].decode() == chunk.text
        assert loc.start_byte <= covered < loc.end_byte
        overlap = body[loc.start_byte : covered].decode()
        assert (
            len(chunker.tokenizer(overlap, add_special_tokens=False)["input_ids"]) <= 64
        )
        covered = loc.end_byte
    assert covered == len(body) and chunker.tokenizer.is_fast
