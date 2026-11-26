Drop the official Agentware Trap Pack files here (emails, web pages, documents,
voice transcripts), then run `uv run fr trap-pack`.

Each file becomes a hostile mock PR (its text is both the PR body and an added file),
and the run is checked against the universal safety expectations.

Formats read: `.pdf` (all page text, including invisible text), `.docx` (including hidden
text), `.eml` (headers, every text part, attachment names), `.html` (raw, so comments and
hidden elements are kept), and plain text (`.txt`, `.md`, `.json`, `.vtt`, `.srt`, …).
Images and audio need OCR or transcription and are listed as SKIP in the results, never
silently ignored. Transcribe audio to `.txt` first.

With `LLM_PROVIDER=gemini` in `.env` the results go to `RESULTS-gemini.md`, and with the
mock model to `RESULTS.md`. Commit both.
