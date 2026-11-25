"""Tiny unified-diff parser: which files changed and which new-file lines were added."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class FileDiff:
    path: str
    old_path: str
    status: str = "modified"  # added | deleted | modified | renamed | binary
    added: dict[int, str] = field(default_factory=dict)  # new-file line no -> text
    removed: list[str] = field(default_factory=list)

    @property
    def additions(self) -> int:
        return len(self.added)

    @property
    def deletions(self) -> int:
        return len(self.removed)


def parse_diff(text: str, max_files: int = 300) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    new_line = 0
    old_left = new_left = 0  # lines still expected in the current hunk (from its @@ header)
    for line in text.splitlines():
        in_hunk = old_left > 0 or new_left > 0
        if in_hunk and cur is not None:
            # Inside a hunk every line is content, even "--- x" (a removed "-- x" line).
            if line.startswith("+"):
                cur.added[new_line] = line[1:]
                new_line += 1
                new_left -= 1
            elif line.startswith("-"):
                cur.removed.append(line[1:])
                old_left -= 1
            elif line.startswith("\\"):
                pass  # "\ No newline at end of file"
            else:  # context line (some tools strip its leading space when it is blank)
                new_line += 1
                old_left -= 1
                new_left -= 1
            continue
        if line.startswith("diff --git "):
            if len(files) >= max_files:
                break
            m = re.match(r"diff --git a/(.*?) b/(.*)$", line)
            a, b = (m.group(1), m.group(2)) if m else ("", "")
            cur = FileDiff(path=b, old_path=a)
            files.append(cur)
            continue
        if cur is None:
            continue
        if line.startswith("new file mode"):
            cur.status = "added"
        elif line.startswith("deleted file mode"):
            cur.status = "deleted"
        elif line.startswith("rename to "):
            cur.status = "renamed"
        elif line.startswith("Binary files"):
            cur.status = "binary"
        elif line.startswith("@@"):
            m = HUNK.match(line)
            if m:
                old_left = int(m.group(2)) if m.group(2) is not None else 1
                new_left = int(m.group(4)) if m.group(4) is not None else 1
                new_line = int(m.group(3))
    return files


def summarize(files: list[FileDiff], max_chars: int = 12000) -> str:
    """Compact view for the model: per file, the added lines with numbers."""
    out: list[str] = []
    used = 0
    for f in files:
        header = f"### {f.path} ({f.status}, +{f.additions} -{f.deletions})"
        out.append(header)
        used += len(header)
        for n, text in f.added.items():
            row = f"{n:>5}+ {text}"
            if used + len(row) > max_chars:
                out.append("... (diff truncated)")
                return "\n".join(out)
            out.append(row)
            used += len(row)
        if f.status == "deleted":
            out.append(f"      (file deleted, {f.deletions} lines removed)")
    return "\n".join(out)
