"""Offline token windows with authoritative, unchanged UTF-8 source intervals."""

import hashlib
import json
import re
import time
from bisect import bisect_right
from importlib.metadata import version
from pathlib import Path

from devflow.knowledge.schemas import ChunkDraft, ChunkLocation, SourceBytes

CHUNKING_VERSION = "knowledge-chunks-v1:json-prefix:lf-boundaries:overlap64"


def location(body, start, end):
    return ChunkLocation(
        start_line=body.count(b"\n", 0, start) + 1,
        end_line=body.count(b"\n", 0, end - 1) + 1,
        start_byte=start,
        end_byte=end,
    )


class LocalChunker:
    def __init__(self, tokenizer_path: Path):
        from transformers import AutoTokenizer

        path = Path(tokenizer_path).resolve(strict=True)
        if not path.is_dir() or not (path / "tokenizer.json").is_file():
            raise ValueError("A local fast tokenizer.json is required")
        self.tokenizer = AutoTokenizer.from_pretrained(
            path, local_files_only=True, trust_remote_code=False, use_fast=True
        )
        if not self.tokenizer.is_fast:
            raise ValueError("A fast tokenizer with original offsets is required")
        digest = hashlib.sha256()
        for name in ("transformers", "tokenizers"):
            digest.update(f"{name}={version(name)}\0".encode())
        for name in (
            "tokenizer.json",
            "tokenizer_config.json",
            "special_tokens_map.json",
            "added_tokens.json",
            "config.json",
        ):
            file = path / name
            if file.is_file():
                digest.update(name.encode() + b"\0")
                digest.update(hashlib.sha256(file.read_bytes()).digest())
        self.tokenizer_fingerprint = digest.hexdigest()

    def _encode(self, text, *, special=True, offsets=False):
        return self.tokenizer(
            text,
            add_special_tokens=special,
            truncation=False,
            padding=False,
            return_offsets_mapping=offsets,
        )

    def split(
        self, source: SourceBytes, title: str, *, cancel=None, deadline=None
    ) -> list[ChunkDraft]:
        def check():
            if (cancel is not None and cancel.is_set()) or (
                deadline is not None and time.monotonic() >= deadline
            ):
                raise TimeoutError("Chunking stopped")

        check()
        source = SourceBytes.model_validate(source.model_dump())
        raw = source.body
        if (
            b"\0" in raw
            or hashlib.sha256(raw).hexdigest() != source.identity.revision_hash
        ):
            raise ValueError("Invalid original source body")
        text = raw.decode("utf-8")
        if (
            not isinstance(title, str)
            or "\0" in title
            or len(title.encode("utf-8")) > 8192
        ):
            raise ValueError("Invalid source title")
        prefix = (
            json.dumps(
                {"path": source.identity.path, "title": title},
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n\n"
        )
        prefix_bytes = len(prefix.encode("utf-8"))
        prefix_tokens = len(self._encode(prefix)["input_ids"])
        if prefix_bytes > 8192 or prefix_tokens > 512:
            raise ValueError("prefix_unfit")
        if not text:
            return []
        byte_offsets = [0]
        for char in text:
            byte_offsets.append(byte_offsets[-1] + len(char.encode("utf-8")))
        offsets = self._encode(text, special=False, offsets=True)["offset_mapping"]
        check()
        if any(not 0 <= a <= b <= len(text) for a, b in offsets):
            raise ValueError("Invalid tokenizer offsets")
        # Normalization can create gaps/duplicate/zero-width token offsets. Raw
        # character intervals remain authoritative; these ends are only cut hints.
        ends = sorted({b for a, b in offsets if b > a} | {len(text)})
        preferred = [
            match.end() for match in re.finditer(r"\n[ \t\r]*\n|\n(?=#{1,6} )", text)
        ]
        line_ends = [match.end() for match in re.finditer("\n", text)]
        line_end_set = set(line_ends)
        drafts, start, frontier = [], 0, 0
        while frontier < len(text):
            check()
            if len(drafts) >= 10000:
                raise ValueError("chunk_limit")
            index = bisect_right(ends, start)
            end = ends[min(len(ends) - 1, index + max(1, 512 - prefix_tokens) - 1)]
            byte_end = (
                bisect_right(byte_offsets, byte_offsets[start] + 8192 - prefix_bytes)
                - 1
            )
            end = min(end, byte_end)
            for boundaries in (preferred, line_ends):
                cut = bisect_right(boundaries, end) - 1
                if cut >= 0 and boundaries[cut] > frontier:
                    end = boundaries[cut]
                    break
            # Do not assume token counts are monotone in substring length.
            while end > frontier:
                check()
                embedding = prefix + text[start:end]
                count = len(self._encode(embedding)["input_ids"])
                if count <= 512 and len(embedding.encode("utf-8")) <= 8192:
                    break
                end = start + (end - start) // 2
            else:
                # Overlap may have consumed the whole budget: retry without it.
                if start < frontier:
                    start = frontier
                    continue
                raise ValueError("source_piece_unfit")
            piece = text[start:end]
            drafts.append(
                ChunkDraft(
                    text=piece,
                    embedding_text=embedding,
                    location=location(raw, byte_offsets[start], byte_offsets[end]),
                    content_hash=hashlib.sha256(piece.encode("utf-8")).hexdigest(),
                    token_count=count,
                    tokenizer_fingerprint=self.tokenizer_fingerprint,
                    chunking_version=CHUNKING_VERSION,
                )
            )
            frontier = end
            if end == len(text):
                break
            # At natural boundaries use zero overlap; token-window breaks retain
            # a verified suffix without preventing the next chunk from advancing.
            start = end
            if end not in line_end_set:
                piece_offsets = self._encode(piece, special=False, offsets=True)[
                    "offset_mapping"
                ]
                if len(piece_offsets) > 64:
                    overlap = piece_offsets[-64][0]
                    proposed = end - len(piece) + overlap
                    if (
                        proposed < end
                        and len(
                            self._encode(text[proposed:end], special=False)["input_ids"]
                        )
                        <= 64
                    ):
                        start = proposed
        return drafts
