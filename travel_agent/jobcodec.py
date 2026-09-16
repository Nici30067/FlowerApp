"""Compact, bounded transport of planning jobs and proposals between the API server and Flower runs.

Payloads travel inside `agent.input` and run events, so they are gzip-compressed and base64-encoded.
Decoding is bounded to prevent decompression bombs from untrusted run metadata.
"""
from __future__ import annotations

import base64
import hashlib
import json
import zlib

MAX_DECODED_BYTES = 2_000_000
PREFIX = "tcj1:"  # travel companion job, format 1


def encode(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=True, sort_keys=True).encode()
    body = base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode("ascii")
    return PREFIX + hashlib.sha256(raw).hexdigest()[:16] + ":" + body


def decode(text: str) -> dict:
    if not isinstance(text, str) or not text.startswith(PREFIX):
        raise ValueError("Unrecognized job payload format")
    digest, _, body = text[len(PREFIX):].partition(":")
    try:
        blob = base64.urlsafe_b64decode(body.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        raise ValueError("Job payload is not valid base64") from None
    decompressor = zlib.decompressobj()
    try:
        raw = decompressor.decompress(blob, MAX_DECODED_BYTES + 1)
    except zlib.error:
        raise ValueError("Job payload is corrupted") from None
    if len(raw) > MAX_DECODED_BYTES or decompressor.unconsumed_tail:
        raise ValueError("Job payload exceeds the decoded size limit")
    if hashlib.sha256(raw).hexdigest()[:16] != digest:
        raise ValueError("Job payload integrity check failed")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Job payload must be a JSON object")
    return payload
