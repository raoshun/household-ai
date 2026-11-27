"""SQLite 層 (Phase 1)。

README §3 tree L171-172 の `backend/app/db/` で、`database.py` と
`migrations/` を公開する。schema の正本は `migrations/` 配下の .sql file に
置き、README に無い `db/schema.py` は作らない (契約 §2.4.4 L173)。
"""

from __future__ import annotations
