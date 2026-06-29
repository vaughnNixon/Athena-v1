import hashlib
import json
import time
import copy
from typing import Optional

class SearchCache:
    def __init__(self, ttl_seconds: int = 3600, max_size: int = 500):
        self.ttl = ttl_seconds
        self.max_size = max_size
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
        # Return deep copy to prevent in-memory state pollution across calls
        res = copy.deepcopy(entry["response"])
        res["from_cache"] = True
        return res

    def set(self, capability: str, query: str, response: dict):
        # Enforce max capacity by evicting oldest expired or first inserted item
        if len(self._store) >= self.max_size:
            now = time.time()
            expired_keys = [k for k, v in self._store.items() if now - v["timestamp"] > self.ttl]
            if expired_keys:
                for k in expired_keys:
                    del self._store[k]
            else:
                # Remove oldest entry
                oldest_key = min(self._store.keys(), key=lambda k: self._store[k]["timestamp"])
                del self._store[oldest_key]
                
        key = self._make_key(capability, query)
        self._store[key] = {
            "timestamp": time.time(),
            "response": copy.deepcopy(response)
        }

    def clear(self):
        self._store.clear()
