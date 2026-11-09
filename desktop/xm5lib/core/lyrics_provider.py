"""Optional, metadata-only LRCLIB lookup. No credentials, uploads or implicit I/O.

One search is one request; failures never trigger immediate retries. Reuse a provider
instance (or its cache_path) across jobs to retain throttling and negative results.
"""
from __future__ import annotations

import copy
import email.utils
import hashlib
import json
import math
import random
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from xm5lib import __version__

MAX_RESPONSE_BYTES = 1024 * 1024
MAX_CANDIDATES = 30
DURATION_TOLERANCE = 2.0
USER_AGENT = f"XM5Library/{__version__} (+https://github.com/noxusCastrator/XM5_PEQ)"


def _cancelled(cancel):
    return bool(cancel and (cancel.is_set() if hasattr(cancel, "is_set") else cancel()))


def _norm(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def _duration(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and 0 < number <= 3600 else None
    except (ValueError, TypeError):
        return None


def metadata(track):
    """Whitelist outgoing fields. Paths and arbitrary track fields cannot escape."""
    result = {}
    for field, parameter in (("title", "track_name"), ("artist", "artist_name"),
                             ("album", "album_name")):
        value = str(track.get(field) or "").strip()
        if len(value) > 512 or any(ord(c) < 32 for c in value):
            raise ValueError(f"invalid lyrics metadata: {field}")
        if value:
            result[parameter] = value
    duration = _duration(track.get("duration"))
    if duration is not None:
        result["duration"] = duration
    if not result.get("track_name") or not result.get("artist_name"):
        raise ValueError("lyrics lookup requires title and artist")
    return result


def rank_candidate(record, track):
    """Conservative recording match; no punctuation/version-marker stripping."""
    checks = {field: bool(_norm(track.get(field))) and
              _norm(track.get(field)) == _norm(record.get(field))
              for field in ("title", "artist", "album")}
    expected, actual = _duration(track.get("duration")), _duration(record.get("duration"))
    delta = abs(expected - actual) if expected is not None and actual is not None else None
    checks["duration"] = delta is not None and delta <= DURATION_TOLERANCE
    return {"strict_match": all(checks.values()), "match": checks,
            "duration_delta": delta, "score": sum(checks.values())}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect rejected", headers, fp)


class LRCLIBProvider:
    name = "lrclib"

    def __init__(self, *, cache_path=None, opener=None, clock=time.time,
                 monotonic=time.monotonic, timeout=5.0, total_timeout=20.0,
                 max_bytes=MAX_RESPONSE_BYTES, interval=1.0, jitter=random.random):
        self.cache_path = Path(cache_path) if cache_path else None
        self.opener = opener or urllib.request.build_opener(_NoRedirect()).open
        self.clock, self.monotonic, self.jitter = clock, monotonic, jitter
        self.timeout = min(15.0, max(0.1, float(timeout)))
        self.total_timeout = min(60.0, max(self.timeout, float(total_timeout)))
        self.max_bytes = min(MAX_RESPONSE_BYTES, max(128, int(max_bytes)))
        self.interval = max(0.7, float(interval))
        self.lock = threading.Lock()
        self.cache = {}
        self.next_request = 0.0
        self.cooldown = 0.0
        if self.cache_path and self.cache_path.exists():
            try:
                if self.cache_path.stat().st_size <= 4 * MAX_RESPONSE_BYTES:
                    state = json.loads(self.cache_path.read_text(encoding="utf-8"))
                    self.cooldown = float(state.get("cooldown", 0))
                    # Disk cache contains negative outcomes only, never lyrics text.
                    self.cache = {k: v for k, v in state.get("negative", {}).items()
                                  if v.get("expires", 0) > self.clock() and
                                  v.get("result", {}).get("status") == "not_found"}
            except (OSError, ValueError, TypeError, AttributeError):
                self.cache = {}

    def _save(self):
        if not self.cache_path:
            return
        negative = {k: v for k, v in self.cache.items()
                    if v["expires"] > self.clock() and v["result"]["status"] == "not_found"}
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.cache_path.with_suffix(".tmp")
        temp.write_text(json.dumps({"cooldown": self.cooldown, "negative": negative},
                                   ensure_ascii=False), encoding="utf-8", newline="\n")
        temp.replace(self.cache_path)

    def _result(self, status, **values):
        if status in ("invalid_response", "offline"):
            self.cooldown = max(self.cooldown, self.clock() + 15)
            self._save()
        return {"provider": self.name, "status": status, "candidates": [],
                "auto_candidate_id": None, "cached": False, **values}

    def _backoff(self, headers, fallback=60):
        delay = fallback
        value = headers.get("Retry-After", "") if headers else ""
        try:
            parsed = float(value)
            if math.isfinite(parsed) and parsed >= 0:
                delay = parsed
        except (ValueError, TypeError):
            try:
                date = email.utils.parsedate_to_datetime(value)
                delay = max(0, date.timestamp() - self.clock())
            except (ValueError, TypeError, OverflowError):
                pass
        delay = max(1.0, delay) * (1 + 0.3 * self.jitter())
        self.cooldown = max(self.cooldown, self.clock() + delay)
        self._save()
        return math.ceil(delay)

    def search(self, track, *, cancel=None):
        if _cancelled(cancel):
            return self._result("cancelled")
        try:
            query = metadata(track)
        except ValueError as exc:
            return self._result("invalid_metadata", error=str(exc))
        # Search lists all candidate recordings; /get's best-match result alone
        # cannot establish that a recording is unambiguous.
        sent = {k: v for k, v in query.items() if k != "duration"}
        url = "https://lrclib.net/api/search?" + urllib.parse.urlencode(sent)
        key = hashlib.sha256(json.dumps(query, sort_keys=True).encode()).hexdigest()
        while not self.lock.acquire(timeout=0.05):
            if _cancelled(cancel):
                return self._result("cancelled")
        try:
            now = self.clock()
            cached = self.cache.get(key)
            if cached and cached["expires"] > now:
                result = copy.deepcopy(cached["result"])
                result["cached"] = True
                return result
            if self.cooldown > now:
                return self._result("rate_limited", retry_after=math.ceil(self.cooldown - now))
            # Short, interruptible waits; no retry loop and no lock-free bursts.
            while self.next_request > self.clock():
                if _cancelled(cancel):
                    return self._result("cancelled")
                time.sleep(min(0.05, self.next_request - self.clock()))
            if _cancelled(cancel):
                return self._result("cancelled")
            self.next_request = self.clock() + self.interval
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                             "Accept": "application/json", "Accept-Encoding": "identity"})
            response = None
            started = self.monotonic()
            try:
                response = self.opener(request, timeout=self.timeout)
                status = getattr(response, "status", 200)
                if status != 200:
                    raise urllib.error.HTTPError(url, status, "HTTP failure", response.headers, None)
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if content_type != "application/json":
                    return self._result("invalid_response", error="expected application/json")
                if response.headers.get("Content-Encoding", "identity").lower() not in ("", "identity"):
                    return self._result("invalid_response", error="compressed payload rejected")
                length = response.headers.get("Content-Length")
                if length is not None and (not str(length).isdigit() or int(length) > self.max_bytes):
                    return self._result("invalid_response", error="invalid or oversized Content-Length")
                chunks, size = [], 0
                while True:
                    if _cancelled(cancel):
                        return self._result("cancelled")
                    if self.monotonic() - started > self.total_timeout:
                        return self._result("offline", error="response deadline exceeded")
                    chunk = response.read(min(16384, self.max_bytes + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > self.max_bytes:
                        return self._result("invalid_response", error="response too large")
                records = json.loads(b"".join(chunks).decode("utf-8"))
                if not isinstance(records, list) or len(records) > MAX_CANDIDATES:
                    return self._result("invalid_response", error="invalid candidate list")
                candidates = []
                for record in records:
                    if not isinstance(record, dict) or type(record.get("id")) is not int or record["id"] < 1:
                        raise ValueError("invalid record id")
                    candidate = {"provider": self.name, "id": record["id"],
                                 "candidate_id": f"lrclib:{record['id']}"}
                    for field, key_name in (("title", "trackName"), ("artist", "artistName"),
                                            ("album", "albumName")):
                        value = record.get(key_name)
                        if not isinstance(value, str) or len(value) > 2048:
                            raise ValueError("invalid record metadata")
                        candidate[field] = value
                    if type(record.get("duration")) not in (int, float):
                        raise ValueError("invalid numeric duration")
                    candidate["duration"] = _duration(record.get("duration"))
                    if candidate["duration"] is None or type(record.get("instrumental")) is not bool:
                        raise ValueError("invalid record duration/instrumental")
                    candidate["instrumental"] = record["instrumental"]
                    for field, key_name in (("plain_text", "plainLyrics"), ("synced_text", "syncedLyrics")):
                        value = record.get(key_name)
                        if value is not None and (not isinstance(value, str) or "\x00" in value or
                                                  len(value.encode("utf-8")) > self.max_bytes // 2):
                            raise ValueError("invalid lyrics field")
                        candidate[field] = value or ""
                    candidate.update(rank_candidate(candidate, track))
                    candidates.append(candidate)
                # Duplicate IDs do not create artificial ambiguity or duplicate variants.
                unique = {r["id"]: r for r in candidates}
                candidates = sorted(unique.values(), key=lambda r: (-r["score"], r["duration_delta"]
                                    if r["duration_delta"] is not None else math.inf, r["id"]))
                strict = [r for r in candidates if r["strict_match"] and
                          (r["plain_text"] or r["synced_text"])]
                # The stable search API currently caps results at 20 without
                # pagination. A saturated page cannot establish uniqueness.
                saturated = len(records) >= 20
                result = self._result("ok" if candidates else "not_found", candidates=candidates,
                                      auto_candidate_id=strict[0]["candidate_id"] if len(strict) == 1 and not saturated else None,
                                      ambiguous=len(strict) > 1, saturated=saturated, request_metadata=query)
            except urllib.error.HTTPError as exc:
                try:
                    if exc.code in (429, 503):
                        return self._result("rate_limited", retry_after=self._backoff(exc.headers))
                    if exc.code == 404:
                        result = self._result("not_found", request_metadata=query)
                    else:
                        return self._result("offline", http_status=exc.code)
                finally:
                    exc.close()
            except (OSError, TimeoutError, urllib.error.URLError):
                self.cooldown = max(self.cooldown, self.clock() + 15)
                self._save()
                return self._result("offline", error="network unavailable")
            except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
                return self._result("invalid_response", error="malformed provider response")
            finally:
                self.next_request = self.clock() + self.interval
                if response is not None:
                    response.close()
            ttl = 6 * 3600 if result["status"] == "not_found" else 3600
            self.cache[key] = {"expires": self.clock() + ttl, "result": copy.deepcopy(result)}
            negative = [(k, v) for k, v in self.cache.items() if v["result"]["status"] == "not_found"]
            positive = [(k, v) for k, v in self.cache.items() if v["result"]["status"] != "not_found"]
            self.cache = dict(sorted(negative, key=lambda item: item[1]["expires"])[-256:] +
                              sorted(positive, key=lambda item: item[1]["expires"])[-32:])
            self._save()
            return result
        finally:
            self.lock.release()
