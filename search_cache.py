import hashlib
import json
import time
from typing import Optional

class SearchCache:
    def __init__(self, ttl_seconds: int = 3600):
        self.ttl = ttl_seconds
        self._store = {}

    def _make_key(self, capability: str, query: str) -> str:
        raw = f"{capability}:{query.strip().lower()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, capability: str, query: str) -> Optional[dict]:
        key = self._make_key(capability, query)
        entry = self._store.get(key)
        if not entry:
            return None
        if time.time() - entry["timestamp"] > self.ttl:
            del self._store[key]
            return None
        res = entry["response"]
        res["from_cache"] = True
        return res

    def set(self, capability: str, query: str, response: dict):
        key = self._make_key(capability, query)
        self._store[key] = {
            "timestamp": time.time(),
            "response": response
        }

    def clear(self):
        self._store.clear()
