"""Incremental first-token extraction for streamed NLP completions (roadmap O-2).

``GroqNlpProvider`` emits a JSON object in JSON-mode; the spoken reply is the
``"response_text"`` string field. To start TTS on the first sentence before the
whole completion lands, ``ResponseTextSentenceStreamer`` scans the raw token
stream and yields complete sentences of ``response_text`` the moment their
boundary is seen — while the caller keeps accumulating the full buffer to parse
the authoritative ``NlpResponse`` (intent/escalation/citations) at the end.

Design guarantees (this feeds the live audio path):
- **Never diverges from the final parse.** Emitted sentences are an exact prefix
  of the decoded ``response_text`` value, so concatenating them reproduces the
  same text ``json.loads`` yields. If the stream can't be scanned cleanly
  (malformed escape, key absent), the streamer emits nothing and the caller
  falls back to the full parse — no wrong audio is ever spoken.
- **Conservative.** A sentence is emitted only once a *following* sentence has
  begun (or the value has closed), so a still-growing final sentence is never
  spoken half-formed.
"""

from __future__ import annotations

import re

from koel.common.text import split_sentences

_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


class ResponseTextSentenceStreamer:
    """Feed raw completion-stream text; get back newly-complete reply sentences."""

    def __init__(self, key: str = "response_text") -> None:
        self._key_re = re.compile(r'"' + re.escape(key) + r'"\s*:\s*"')
        self.buffer = ""          # full raw stream, for the caller's final json.loads
        self._value_start = -1    # index of the first char of the value body, once found
        self._emitted = 0         # count of sentences already yielded
        self._done = False        # closing quote of the value seen
        self.failed = False       # gave up scanning → caller uses the full parse

    def feed(self, chunk: str) -> list[str]:
        """Append streamed text; return any newly-complete sentences (possibly [])."""
        self.buffer += chunk
        if self.failed or self._done:
            return []
        if self._value_start < 0:
            m = self._key_re.search(self.buffer)
            if not m:
                return []
            self._value_start = m.end()
        decoded, closed = self._decode(self.buffer[self._value_start :])
        if self.failed:
            return []
        if closed:
            self._done = True
        return self._emit(decoded, final=closed)

    def _decode(self, raw: str) -> tuple[str, bool]:
        """Decode the JSON-string body ``raw`` up to the closing quote or the last
        fully-formed char (an incomplete trailing escape is held for next feed).
        Returns (decoded_prefix, closed)."""
        out: list[str] = []
        i, n = 0, len(raw)
        while i < n:
            c = raw[i]
            if c == '"':
                return "".join(out), True  # unescaped closing quote → value done
            if c == "\\":
                if i + 1 >= n:
                    break  # dangling backslash — wait for more
                e = raw[i + 1]
                if e == "u":
                    if i + 6 > n:
                        break  # incomplete \uXXXX — wait for more
                    try:
                        out.append(chr(int(raw[i + 2 : i + 6], 16)))
                    except ValueError:
                        self.failed = True
                        return "", False
                    i += 6
                    continue
                out.append(_ESCAPES.get(e, e))
                i += 2
                continue
            out.append(c)
            i += 1
        return "".join(out), False

    def _emit(self, decoded: str, *, final: bool) -> list[str]:
        sentences = split_sentences(decoded)
        # Until the value closes, hold the last (still-growing) sentence back.
        complete = sentences if final else sentences[:-1]
        new = complete[self._emitted :]
        self._emitted = len(complete)
        return new
