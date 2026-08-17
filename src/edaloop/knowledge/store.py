from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import sqlite_vec

from edaloop.knowledge.models import BlockRecord, RetrievedBlock, UpstreamRef
from edaloop.llm.base import EmbeddingProvider, RerankProvider

_RRF_K = 60
_MAX_FTSTERMS = 128
_PARTNUM_BONUS = 1.0
_PARTNUM_RE = re.compile(r"[0-9a-z][0-9a-z_.-]{2,}")
_W_REF_EXACT = 1.0
_W_REF_CONTAIN = 0.8
_W_TAG = 0.6


def _is_specific(term: str) -> bool:
    if len(term) < 3:
        return False
    return any(c.isdigit() for c in term) or (term.isascii() and len(term) >= 5)


def _trigrams_of_run(run: str) -> list[str]:
    return [run[i : i + 3] for i in range(len(run) - 2)]


def _trigram_or_terms(query_lower: str) -> list[str]:
    runs = _PARTNUM_RE.findall(query_lower)
    cjk_runs = re.findall(r"[\u4e00-\u9fff]{3,}", query_lower)
    part_trigrams: list[str] = []
    for r in runs:
        part_trigrams.extend(_trigrams_of_run(r))
    cjk_trigrams: list[str] = []
    for r in cjk_runs:
        cjk_trigrams.extend(_trigrams_of_run(r))
    seen: set[str] = set()
    terms: list[str] = []
    for t in part_trigrams + cjk_trigrams:
        if t not in seen:
            seen.add(t)
            terms.append(t)
        if len(terms) >= _MAX_FTSTERMS:
            break
    return terms


class KnowledgeStore:
    def __init__(
        self,
        path: str | Path,
        embedder: EmbeddingProvider,
        reranker: RerankProvider | None = None,
    ) -> None:
        self.path = str(path)
        self.embedder = embedder
        self.reranker = reranker
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self.conn.enable_load_extension(False)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        dim = getattr(self.embedder, "dim", 1024)
        self.conn.execute("CREATE TABLE IF NOT EXISTS blocks (rowid INTEGER PRIMARY KEY, block_id TEXT UNIQUE NOT NULL, name TEXT NOT NULL, desc TEXT NOT NULL, category TEXT DEFAULT 'general', tags TEXT DEFAULT '[]', parts TEXT DEFAULT '[]', ports TEXT DEFAULT '[]', provenance TEXT DEFAULT '', upstream TEXT DEFAULT '')")
        self.conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS blocks_vec USING vec0(embedding float[{dim}])")
        self.conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS blocks_fts USING fts5(text, content='', tokenize='trigram')")

    def rebuild(self, blocks: list[BlockRecord]) -> int:
        self.conn.execute("DROP TABLE IF EXISTS blocks")
        self.conn.execute("DROP TABLE IF EXISTS blocks_vec")
        self.conn.execute("DROP TABLE IF EXISTS blocks_fts")
        self._ensure_schema()
        texts = [self._embed_text(b) for b in blocks]
        vectors = self.embedder.embed_documents(texts)
        for b, v in zip(blocks, vectors):
            cur = self.conn.execute(
                "INSERT INTO blocks(block_id, name, desc, category, tags, parts, ports, provenance, upstream) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    b.block_id,
                    b.name,
                    b.desc,
                    b.category,
                    json.dumps(b.tags, ensure_ascii=False),
                    json.dumps([p.model_dump() for p in b.parts], ensure_ascii=False),
                    json.dumps(b.ports, ensure_ascii=False),
                    b.provenance,
                    b.upstream.model_dump_json() if b.upstream else "",
                ),
            )
            rowid = cur.lastrowid
            self.conn.execute("INSERT INTO blocks_vec(rowid, embedding) VALUES(?, ?)", (rowid, json.dumps(v)))
            self.conn.execute("INSERT INTO blocks_fts(rowid, text) VALUES(?, ?)", (rowid, self._fts_text(b)))
        self.conn.commit()
        return len(blocks)

    def _embed_text(self, b: BlockRecord) -> str:
        return "\n".join([b.name, b.desc, " ".join(b.tags), " ".join(p.ref for p in b.parts)])

    def _fts_text(self, b: BlockRecord) -> str:
        return " ".join([b.block_id, b.name, b.desc, " ".join(b.tags), " ".join(p.ref for p in b.parts)])

    def _dense_search(self, query: str, k: int) -> list[tuple[int, float]]:
        qv = self.embedder.embed_query(query)
        rows = self.conn.execute(
            "SELECT rowid, distance FROM blocks_vec WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (json.dumps(qv), k),
        ).fetchall()
        return [(r, 1.0 - d * d / 2.0) for r, d in rows]

    def _keyword_search(self, query: str, k: int) -> list[int]:
        q = query.strip().lower()
        terms = _trigram_or_terms(q)
        if not terms:
            return []
        expr = " OR ".join(f'"{t}"' for t in terms)
        try:
            rows = self.conn.execute(
                "SELECT rowid FROM blocks_fts WHERE blocks_fts MATCH ? ORDER BY rank LIMIT ?",
                (expr, k),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [r for (r,) in rows]

    def _partnum_hits(self, query: str) -> dict[int, float]:
        q = query.lower()
        runs = [r for r in _PARTNUM_RE.findall(q) if len(r) >= 4]
        hits: dict[int, float] = {}
        for rowid, parts_json, tags_json in self.conn.execute(
            "SELECT rowid, parts, tags FROM blocks"
        ):
            refs = [p["ref"].lower() for p in json.loads(parts_json)]
            tags = [t.lower() for t in json.loads(tags_json) if _is_specific(t.lower())]
            best = 0.0
            for ref in refs:
                if ref in q:
                    best = max(best, _W_REF_EXACT)
                elif any(r in ref for r in runs):
                    best = max(best, _W_REF_CONTAIN)
            for tag in tags:
                if tag in q:
                    best = max(best, _W_TAG)
            if best > 0.0:
                hits[rowid] = best
        return hits

    def retrieve(self, query: str, top_k: int = 5, *, candidate_k: int = 20) -> list[RetrievedBlock]:
        dense = self._dense_search(query, candidate_k)
        kw = self._keyword_search(query, candidate_k)
        fused: dict[int, float] = {}
        channels: dict[int, set[str]] = {}
        for rank, (rowid, _) in enumerate(dense):
            fused[rowid] = fused.get(rowid, 0.0) + 1.0 / (_RRF_K + rank + 1)
            channels.setdefault(rowid, set()).add("dense")
        for rank, rowid in enumerate(kw):
            fused[rowid] = fused.get(rowid, 0.0) + 1.0 / (_RRF_K + rank + 1)
            channels.setdefault(rowid, set()).add("keyword")
        for rowid, bonus in self._partnum_hits(query).items():
            fused[rowid] = fused.get(rowid, 0.0) + bonus
            channels.setdefault(rowid, set()).add("partnum")
        if not fused:
            return []
        candidates = sorted(fused.items(), key=lambda x: -x[1])
        cand_rowids = [r for r, _ in candidates]
        if self.reranker is not None:
            docs = self._docs_for(cand_rowids)
            reranked = self.reranker.rerank(query, docs, top_k=len(docs))
            for rank, (i, _score) in enumerate(reranked):
                rowid = cand_rowids[i]
                fused[rowid] = fused.get(rowid, 0.0) + 1.0 / (_RRF_K + rank + 1)
                channels.setdefault(rowid, set()).add("rerank")
            candidates = sorted(fused.items(), key=lambda x: -x[1])
            cand_rowids = [r for r, _ in candidates]
        top = cand_rowids[:top_k]
        metas = self._meta_for(top)
        results = []
        for i, rowid in enumerate(top):
            m = metas[rowid]
            upstream_raw = m["upstream"]
            upstream = None
            if upstream_raw:
                try:
                    upstream = UpstreamRef.model_validate_json(upstream_raw)
                except Exception:
                    upstream = None
            results.append(
                RetrievedBlock(
                    block_id=m["block_id"],
                    name=m["name"],
                    desc=m["desc"],
                    category=m["category"],
                    tags=json.loads(m["tags"]),
                    parts=[p for p in json.loads(m["parts"])],
                    ports=json.loads(m["ports"]),
                    provenance=m["provenance"],
                    upstream=upstream,
                    score=round(fused.get(rowid, 0.0), 6),
                    channels=sorted(channels.get(rowid, set())),
                    rank=i + 1,
                )
            )
        return results

    def _docs_for(self, rowids: list[int]) -> list[str]:
        metas = self._meta_for(rowids)
        return [
            "\n".join(
                [
                    m["name"],
                    m["desc"],
                    " ".join(json.loads(m["tags"])),
                    " ".join(p["ref"] for p in json.loads(m["parts"])),
                ]
            )
            for m in (metas[r] for r in rowids)
        ]

    def _meta_for(self, rowids: list[int]) -> dict[int, sqlite3.Row]:
        if not rowids:
            return {}
        marks = ",".join("?" * len(rowids))
        rows = self.conn.execute(
            f"SELECT rowid, block_id, name, desc, category, tags, parts, ports, provenance, upstream FROM blocks WHERE rowid IN ({marks})",
            rowids,
        ).fetchall()
        return {r["rowid"]: r for r in rows}

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]

    def close(self) -> None:
        self.conn.close()
