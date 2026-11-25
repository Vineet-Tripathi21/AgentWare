"""Untrusted-content handling.

Everything that comes from the outside world (PR title/body, diff, file contents,
CI logs) is DATA. It is wrapped in nonce-tagged delimiters before it reaches the
model, and it is scanned for instruction-like text. A hit never stops the safe
part of the review; it is logged, labelled and reported, and the model's output
for that run is held to the same schema and policy as always.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass

PATTERNS: list[tuple[str, str]] = [
    ("override", r"\b(ignore|disregard|forget)\b.{0,40}\b(previous|prior|above|all|earlier)\b.{0,30}"
                 r"\b(instructions?|rules?|prompts?|guidelines?)"),
    ("role_hijack", r"\b(you are now|act as|new instructions|system prompt|developer mode)\b"),
    ("merge_request", r"\b(merge|approve)\b.{0,40}\b(this|the)?\s*(pr|pull request|branch|main|master)\b"),
    ("push_main", r"\b(push|commit|force[- ]push)\b.{0,30}\b(to )?(main|master|production)\b"),
    ("secrets", r"\b(print|reveal|show|send|post|leak|dump|echo|exfiltrate)\b.{0,40}"
                r"\b(env|environment variables?|secrets?|tokens?|api[_ ]?keys?|credentials?|\.env)\b"),
    ("shell", r"(curl|wget)\s+[^\s|]+\s*\|\s*(ba)?sh|\brm\s+-rf\b|\bchmod\s+\+x\b"),
    ("exfil_url", r"https?://[^\s)]*(webhook\.site|requestbin|ngrok|pipedream|burpcollaborator)"),
    ("hidden_note", r"(note|message|instruction)s?\s+(to|for)\s+(the\s+)?(ai|assistant|agent|llm|bot|reviewer)"),
    ("disable_safety", r"\b(disable|skip|bypass|turn off)\b.{0,30}\b(tests?|checks?|review|safety|guardrails?|ci)\b"),
]
_COMPILED = [(name, re.compile(p, re.IGNORECASE | re.DOTALL)) for name, p in PATTERNS]
_HIDDEN_CHARS = re.compile('[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]')


@dataclass
class Finding:
    kind: str
    source: str
    excerpt: str


def scan(text: str, source: str) -> list[Finding]:
    findings: list[Finding] = []
    if not text:
        return findings
    if _HIDDEN_CHARS.search(text):
        findings.append(Finding("hidden_unicode", source, "zero-width / bidi control characters"))
    clean = _HIDDEN_CHARS.sub("", text)
    for name, rx in _COMPILED:
        m = rx.search(clean)
        if m:
            start = max(0, m.start() - 20)
            excerpt = clean[start:m.end() + 20].replace("\n", " ").strip()
            findings.append(Finding(name, source, excerpt[:160]))
    return findings


def wrap_untrusted(label: str, text: str) -> tuple[str, str]:
    """Return (wrapped_text, nonce). The nonce stops content from faking the closing tag."""
    nonce = secrets.token_hex(4)
    safe = text.replace("</untrusted", "<\\/untrusted")
    return f"<untrusted-{nonce} source=\"{label}\">\n{safe}\n</untrusted-{nonce}>", nonce
