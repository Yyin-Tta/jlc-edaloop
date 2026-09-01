from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

import sqlite_vec

from edaloop.knowledge.models import BlockRecord, CaseDigest, CaseRecord, Electrical, RetrievedBlock, UpstreamRef
from edaloop.llm.base import EmbeddingProvider, RerankProvider

_RRF_K = 60
_MAX_FTSTERMS = 128
_PARTNUM_BONUS = 1.0
_PARTNUM_RE = re.compile(r"[0-9a-z][0-9a-z_.-]{2,}")
_W_REF_EXACT = 1.0
_W_REF_CONTAIN = 0.8
_W_TAG = 0.6
# P4-6① rail 适配度通道:查询里同时出现 ≥2 条轨且块的结构化轨源(electrical.rails/
# upstream 端口网/ports)都命中 → 这是「在这两条轨之间转换」的电源块,给一次性加权。
# 只认 ≥2 轨:单轨命中在 query 里几乎是常态(3.3V 到处都是),作信号太噪会淹没非电源金标。
_W_RAIL_CONV = 0.35
# P4-6① 意图行槽:query_text 按行组织(每功能/接口一行),多行查询逐行 dense+kw,
# 每行每通道前三名拿递减加成——多意图长查询里的小众意图(LED 指示/端子)不再被
# 主题块(电源/USB)整段稀释。实测:led-indicator 单行查询双通道 #1,混进 13 行
# 长查询后双双跌出 top-20,是 req-03/req-10 miss 的根因。
_INTENT_SLOT_BONUS = (0.7, 0.5, 0.35)
# P4-6① 同族替换品 cap:usb-c-16p / usb-c-power-entry / up-usbc_dual_orientation_data
# 端口集高度重叠(同一连接器的不同集成度),全挤进 top-8 互为替换品,挤掉真金标
# (req-10 四个 USB 族占位,端子金标出局)。终切时按端口重叠判族,每族最多留 2。
_FAMILY_CAP = 1  # 每族 1 席:金标从不要求同族两席(等价类单席),cap=2 实测放 ch340k 抢占 req-10 端子坑
_FAMILY_MIN_PORTS = 4
_RAIL_RE = re.compile(r"(\d+(?:\.\d+)?)\s*v(\d+)?(?![a-z0-9])", re.IGNORECASE)
# P4-6④ 不可落图块后置过滤:无 upstream、无 lcsc+pinout、也非 std R/C 通道的块
# (battery-18650-holder/decoupling-caps-bank/test-points)检索可见但 planner 无法应用,
# 白占 top-k 候选位。与 generate/stdparts.kind_of 的主判据同源(块 id 白名单)。
_STD_BLOCK_IDS = ("resistor-std", "capacitor-std")
# P4-6② 电气不兼容降权幅度:要足以把 deny 块推出 top-k,但不至于把分数清零
# (候选位次可追溯,审计里能看到它被降权而非消失)。
_W_ELEC_DENY = 0.8
# P4-6③ 案例通道:命中案例按相似度整组加权;评测路径不传 ir → 通道结构性消融。
_W_CASE = 0.5
_CASE_SIM_MIN = 0.3
# 轨最强(结构性)、接口次之、功能名最弱(语义表述方差大)。
_CASE_WEIGHTS = {"rails": 2.0, "interfaces": 1.5, "functions": 1.0}


def _case_digest_of(ir) -> CaseDigest:
    """DesignIR → 案例指纹(鸭子类型取字段,不 import intent——knowledge 层保持零反向依赖)。"""
    parts: list[str] = []
    power = getattr(ir, "power", None)
    if power is not None:
        parts.extend(getattr(power, "inputs", None) or [])
        for r in getattr(power, "rails", None) or []:
            parts.append(r.v_text())
            if r.name:
                parts.append(r.name)
    rails = sorted(_rails_in(" ".join(parts)))
    interfaces = sorted({str(i.type).lower().strip() for i in getattr(ir, "interfaces", None) or [] if str(i.type).strip()})
    functions = sorted({str(f.name).lower().strip() for f in getattr(ir, "functions", None) or [] if str(f.name).strip()})
    return CaseDigest(rails=rails, interfaces=interfaces, functions=functions)


def _case_sim(a: CaseDigest, b: CaseDigest) -> float:
    """三组加权 Jaccard,只在双侧至少一侧非空的分量上计;全空 → 0(不判)。"""
    num = 0.0
    den = 0.0
    for field, w in _CASE_WEIGHTS.items():
        sa, sb = set(getattr(a, field)), set(getattr(b, field))
        if not sa and not sb:
            continue
        den += w
        num += w * len(sa & sb) / len(sa | sb)
    return num / den if den else 0.0


def _is_specific(term: str) -> bool:
    if len(term) < 3:
        return False
    return any(c.isdigit() for c in term) or (term.isascii() and len(term) >= 5)


def _trigrams_of_run(run: str) -> list[str]:
    return [run[i : i + 3] for i in range(len(run) - 2)]


def _rail_norm(num: str, tail: str | None) -> str:
    """3.3V→3v3、1.8V→1v8、5V→5v、12V→12v、3V3→3v3 —— 归一成 catalog 常用的轨名写法。"""
    if tail:
        return f"{int(num)}v{int(tail)}"
    if "." not in num:
        return f"{int(num)}v"
    whole, frac = num.split(".", 1)
    if int(frac) == 0:
        return f"{int(whole)}v"
    return f"{int(whole)}v{int(frac)}"


def _rails_in(text_lower: str) -> set[str]:
    return {_rail_norm(m.group(1), m.group(2)) for m in _RAIL_RE.finditer(text_lower)}


def _rail_volts(rail: str) -> float | None:
    """'3v3'→3.3、'12v'→12.0;解析失败返回 None。"""
    m = re.fullmatch(r"(\d+)v(\d+)?", rail)
    if not m:
        return None
    if m.group(2):
        return float(f"{m.group(1)}.{m.group(2)}")
    return float(m.group(1))


def _block_rails(ports: list[str], upstream: str, electrical: str) -> set[str]:
    """块侧轨源只认结构化字段:ports 引脚网名 + upstream 端口网名 + electrical.rails。

    name/tags 不进:「5V tolerant」这类词会把非电源块抬进转换器信号。
    """
    text_parts: list[str] = [str(p) for p in ports]
    if upstream:
        try:
            up = json.loads(upstream)
            text_parts.extend(str(v) for v in (up.get("ports") or {}).values())
        except (json.JSONDecodeError, AttributeError):
            pass
    if electrical:
        try:
            ele = json.loads(electrical)
            text_parts.extend(str(r) for r in (ele.get("rails") or []))
        except (json.JSONDecodeError, AttributeError):
            pass
    rails: set[str] = set()
    for t in text_parts:
        rails |= _rails_in(t.lower())
    return rails


def _elec_digest(b: BlockRecord) -> str:
    """P4-6①:category + electrical 摘要并入索引文本(电压/电流适配度可检索)。"""
    parts = [b.category or ""]
    e = b.electrical
    if e is not None:
        if e.v_supply_min is not None:
            parts.append(f"v_supply_min {e.v_supply_min}v")
        if e.v_supply_max is not None:
            parts.append(f"v_supply_max {e.v_supply_max}v")
        if e.i_max is not None:
            parts.append(f"i_max {e.i_max}a")
        if e.i_typ is not None:
            parts.append(f"i_typ {e.i_typ}a")
        if e.rails:
            parts.append("rails " + " ".join(e.rails))
        parts.extend(f"{k} {v}" for k, v in e.params.items())
    return " ".join(p for p in parts if p)


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
        self.conn.execute("CREATE TABLE IF NOT EXISTS blocks (rowid INTEGER PRIMARY KEY, block_id TEXT UNIQUE NOT NULL, name TEXT NOT NULL, desc TEXT NOT NULL, category TEXT DEFAULT 'general', tags TEXT DEFAULT '[]', parts TEXT DEFAULT '[]', ports TEXT DEFAULT '[]', provenance TEXT DEFAULT '', upstream TEXT DEFAULT '', lcsc TEXT DEFAULT '', pinout TEXT DEFAULT '', electrical TEXT DEFAULT '')")
        # 旧库迁移(P4-0②):已有 blocks 表补 electrical 列,免全量 rebuild
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(blocks)")}
        if "electrical" not in cols:
            self.conn.execute("ALTER TABLE blocks ADD COLUMN electrical TEXT DEFAULT ''")
            self.conn.commit()
        self.conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS blocks_vec USING vec0(embedding float[{dim}])")
        self.conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS blocks_fts USING fts5(text, content='', tokenize='trigram')")
        # P4-6③ 案例库:rebuild 不 DROP 此表(eval 库重建后案例自然清空 = 消融;生产库跨 run 累积)
        self.conn.execute("CREATE TABLE IF NOT EXISTS cases (case_id TEXT PRIMARY KEY, name TEXT NOT NULL, origin TEXT NOT NULL, digest TEXT NOT NULL, blocks TEXT NOT NULL, hash TEXT UNIQUE NOT NULL, created TEXT DEFAULT '')")

    def rebuild(self, blocks: list[BlockRecord]) -> int:
        self.conn.execute("DROP TABLE IF EXISTS blocks")
        self.conn.execute("DROP TABLE IF EXISTS blocks_vec")
        self.conn.execute("DROP TABLE IF EXISTS blocks_fts")
        self._ensure_schema()
        texts = [self._embed_text(b) for b in blocks]
        # 无 embedder(未配密钥)→ 只建关键词 FTS,跳过向量表(检索降级不阻断)
        vectors = self.embedder.embed_documents(texts) if self.embedder else [None] * len(blocks)
        for b, v in zip(blocks, vectors):
            cur = self.conn.execute(
                "INSERT INTO blocks(block_id, name, desc, category, tags, parts, ports, provenance, upstream, lcsc, pinout, electrical) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    b.lcsc or "",
                    json.dumps(b.pinout, ensure_ascii=False) if b.pinout else "",
                    b.electrical.model_dump_json() if b.electrical else "",
                ),
            )
            rowid = cur.lastrowid
            if v is not None:
                self.conn.execute("INSERT INTO blocks_vec(rowid, embedding) VALUES(?, ?)", (rowid, json.dumps(v)))
            self.conn.execute("INSERT INTO blocks_fts(rowid, text) VALUES(?, ?)", (rowid, self._fts_text(b)))
        self.conn.commit()
        return len(blocks)

    def _embed_text(self, b: BlockRecord) -> str:
        return "\n".join([b.name, b.desc, _elec_digest(b), " ".join(b.tags), " ".join(p.ref for p in b.parts)])

    def _fts_text(self, b: BlockRecord) -> str:
        return " ".join(
            [b.block_id, b.name, b.desc, _elec_digest(b), " ".join(b.tags), " ".join(p.ref for p in b.parts)]
        )

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
                # 泛化 ref(usb-c/led 这类无数字短词)只值 tag 价:ref="USB-C" 曾在
                # req-10 把全部 USB 族抬进 top-8 挤掉端子金标,型号级证据必须有数字或足够长。
                w_exact = _W_REF_EXACT if _is_specific(ref) else _W_TAG
                w_contain = _W_REF_CONTAIN if _is_specific(ref) else _W_TAG
                if ref in q:
                    best = max(best, w_exact)
                elif any(r in ref for r in runs):
                    best = max(best, w_contain)
            for tag in tags:
                if tag in q:
                    best = max(best, _W_TAG)
            if best > 0.0:
                hits[rowid] = best
        return hits

    def _rail_conv_hits(self, query: str) -> set[int]:
        """P4-6①:块结构化轨源 ≥2 条都出现在查询里 → 电源转换器信号,整行加分。"""
        qr = _rails_in(query.lower())
        if len(qr) < 2:
            return set()
        hits: set[int] = set()
        for rowid, ports_json, upstream_raw, electrical_raw in self.conn.execute(
            "SELECT rowid, ports, upstream, electrical FROM blocks"
        ):
            try:
                ports = json.loads(ports_json) if ports_json else []
            except json.JSONDecodeError:
                ports = []
            brails = _block_rails(ports, upstream_raw or "", electrical_raw or "")
            if len(qr & brails) >= 2:
                hits.add(rowid)
        return hits

    def _elec_deny_hits(self, query: str) -> set[int]:
        """P4-6② 电气不兼容降权(窄域):电源块 v_supply_max 已知且被设计最高轨超过,
        且其声明的输入轨(upstream VIN 命名)在设计轨集合里无落点 → 降权。

        三态原则:无 electrical 数据不判(UNKNOWN 不杀);ldo(VIN_5V, max 15V) 在
        24V 直入 3V3 的负样本设计里让位给宽压 buck,12V→buck→5V→ldo 正常设计不误伤。
        """
        qr = _rails_in(query.lower())
        if not qr:
            return set()
        qvolts = {v for r in qr if (v := _rail_volts(r)) is not None}
        if not qvolts:
            return set()
        max_qv = max(qvolts)
        hits: set[int] = set()
        for rowid, upstream_raw, electrical_raw in self.conn.execute(
            "SELECT rowid, upstream, electrical FROM blocks WHERE category='power'"
        ):
            vmax = None
            if electrical_raw:
                try:
                    vmax = json.loads(electrical_raw).get("v_supply_max")
                except (json.JSONDecodeError, AttributeError):
                    vmax = None
            if vmax is None or max_qv <= float(vmax):
                continue  # 无数据不判 / 宽压耐受
            in_rails: set[str] = set()
            if upstream_raw:
                try:
                    up = json.loads(upstream_raw)
                    for k, v in (up.get("ports") or {}).items():
                        if "vin" in str(k).lower() or "vin" in str(v).lower():
                            in_rails |= _rails_in(f"{k} {v}".lower())
                except (json.JSONDecodeError, AttributeError):
                    pass
            if in_rails & qr:
                continue  # 设计里有它声明的输入轨,有落点
            hits.add(rowid)
        return hits

    def _datasheet_backfill(self, parts_json: str, electrical: Electrical | None) -> Electrical | None:
        """P4-6②/G16:JOIN datasheets 表(同库)按 parts.ref 回填块电气字段。

        只补缺不覆写:wmsc 源的既有字段优先;datasheet 数值行来源并入 source,
        数值表与 prose 建议分级处理(这里只信 elec 数值行,不读 suggestions)。
        """
        try:
            refs = [str(p["ref"]).upper() for p in json.loads(parts_json)]
        except (json.JSONDecodeError, KeyError, TypeError):
            return electrical
        rows: list[dict] = []
        matched_part: str | None = None
        try:
            for ref in refs:
                r = self.conn.execute(
                    "SELECT pins, part FROM datasheets WHERE upper(part) = ?", (ref,)
                ).fetchone()
                if not r and len(ref) >= 5:
                    # 变体后缀命中(P5-1 实测:库 ref=AMS1117-3.3/CH340K/STM32F103C8T6,
                    # datasheet 部件名=AMS1117/CH340/STM32F103C8,精确等值 JOIN 零命中)。
                    # 同 die 变体共享电气参数表,elec 行通用;前缀 ≥4 字符+最长优先控误命中。
                    r = self.conn.execute(
                        "SELECT pins, part FROM datasheets "
                        "WHERE length(part) >= 4 AND ? LIKE upper(part) || '%' "
                        "ORDER BY length(part) DESC LIMIT 1",
                        (ref,),
                    ).fetchone()
                if r:
                    table = json.loads(r[0])
                    rows = list(table.get("elec") or [])
                    if rows:
                        matched_part = str(r[1])
                        break
        except sqlite3.OperationalError:
            return electrical  # 同库无 datasheets 表(未跑过 ingest)
        if not rows:
            return electrical

        def _v(row: dict, key: str) -> float | None:
            try:
                return float(row.get(key) or "")
            except (ValueError, TypeError):
                return None

        src_part = matched_part or refs[0]
        v_min = v_max = i_max = None
        # 供电行匹配用 startswith(P5-1 实测:子串 "vin" 会命中脚注条件句
        # 「(VIN - VOUT) ≤ 12V」行,把 AMS1117 的 1.21V 基准电压回填成供电范围;
        # 供电范围行总是以量名开头,条件句总以被测量/括号开头)。
        _V_KEYS = ("supply voltage", "input voltage", "operating voltage",
                   "vcc", "vdd", "vin", "vbat", "operating input voltage")
        for row in rows:
            param = str(row.get("param", "")).lower().strip()
            unit = str(row.get("unit", "")).upper()
            if unit == "V" and param.startswith(_V_KEYS):
                v_min = _v(row, "min") if v_min is None else v_min
                v_max = _v(row, "max") if v_max is None else v_max
            if unit in ("A", "mA") and any(k in param for k in ("output current", "iout", "supply current", "continuous current")):
                a = _v(row, "max")
                if a is not None:
                    i_max = a / 1000.0 if unit == "mA" else a
        electrical = electrical or Electrical()
        touched = False
        if v_min is not None and electrical.v_supply_min is None:
            electrical.v_supply_min = v_min
            touched = True
        if v_max is not None and electrical.v_supply_max is None:
            electrical.v_supply_max = v_max
            touched = True
        if i_max is not None and electrical.i_max is None:
            electrical.i_max = i_max
            touched = True
        if touched:
            src = f"datasheet:{src_part}"
            electrical.source = f"{electrical.source}; {src}" if electrical.source else src
        return electrical

    def _placeable(self, rowid: int) -> bool:
        """P4-6④:planner 三通道(block-apply / lcsc place / std-value)一个都不占的块 → 不可落图。"""
        row = self.conn.execute(
            "SELECT block_id, upstream, lcsc, pinout FROM blocks WHERE rowid = ?", (rowid,)
        ).fetchone()
        if row is None:
            return False
        block_id, upstream, lcsc, pinout = row
        if upstream:
            return True
        if lcsc and pinout and pinout != "[]" and pinout != "":
            return True
        return block_id in _STD_BLOCK_IDS

    def retrieve(self, query: str, top_k: int = 5, *, candidate_k: int = 20, ir=None) -> list[RetrievedBlock]:
        # P4-6③ 案例第五通道:ir=None 时通道关闭(评测路径结构性消融——eval 只喂 query_text,
        # 案例库即使非空也不参与打分,防「用自己回写的案例命中自己的金标」自证清白)。
        # P4-6① 意图行分解:query_text 按行组织(每功能/接口/电源轨一行)。
        # 多行查询逐行跑 dense+kw 并给行内前三名意图槽加成;单行/无行结构退化为整段查询。
        lines = [ln.strip() for ln in (query or "").splitlines() if len(ln.strip()) >= 4]
        if not lines:
            lines = [(query or "power mcu interface").strip()]
        multi = len(lines) > 1
        fused: dict[int, float] = {}
        channels: dict[int, set[str]] = {}
        # 意图槽每通道只记跨行最大值:槽分若跨行累加,主题块(电源/USB 词渗进每行的
        # dense 前三)会滚出 5+ 分重新挤掉单行冠军(led/端子),违背本通道初衷。
        best_slot: dict[int, tuple[float, float]] = {}

        def _add(rowid: int, score: float, channel: str) -> None:
            fused[rowid] = fused.get(rowid, 0.0) + score
            channels.setdefault(rowid, set()).add(channel)

        for ln in lines:
            dense = self._dense_search(ln, candidate_k) if self.embedder else []
            kw = self._keyword_search(ln, candidate_k)
            for rank, (rowid, _) in enumerate(dense):
                _add(rowid, 1.0 / (_RRF_K + rank + 1), "dense")
            for rank, rowid in enumerate(kw):
                _add(rowid, 1.0 / (_RRF_K + rank + 1), "keyword")
            if multi:
                for ci, hits in enumerate(([r for r, _ in dense], kw)):
                    for i, rowid in enumerate(hits[: len(_INTENT_SLOT_BONUS)]):
                        d, k = best_slot.get(rowid, (0.0, 0.0))
                        best_slot[rowid] = (max(d, _INTENT_SLOT_BONUS[i]), k) if ci == 0 else (d, max(k, _INTENT_SLOT_BONUS[i]))
        for rowid, (d_slot, k_slot) in best_slot.items():
            if d_slot or k_slot:
                _add(rowid, d_slot + k_slot, "intent")
        for rowid, bonus in self._partnum_hits(query).items():
            _add(rowid, bonus, "partnum")
        for rowid in self._rail_conv_hits(query):
            _add(rowid, _W_RAIL_CONV, "rail")
        for rowid in self._elec_deny_hits(query):
            fused[rowid] = fused.get(rowid, 0.0) - _W_ELEC_DENY
            channels.setdefault(rowid, set()).add("elec-deny")
        # P4-6③ 案例通道:IR×case 结构化相似度 ≥ 阈值 → 整组块按相似度加权进候选。
        # 分数随 sim 缩放(强案例多抬,弱案例轻抬),块仍需过 placeable 过滤与 family cap。
        if ir is not None:
            q_digest = _case_digest_of(ir)
            for case in self.cases():
                sim = _case_sim(q_digest, case.digest)
                if sim < _CASE_SIM_MIN:
                    continue
                for bid in case.block_ids:
                    row = self.conn.execute("SELECT rowid FROM blocks WHERE block_id = ?", (bid,)).fetchone()
                    if row:
                        _add(row[0], _W_CASE * sim, "case")
        # P4-6④:不可落图块(无 upstream/lcsc+pinout/std 通道)从候选池剔除,
        # 把 top-k 位让给 planner 真能应用的块。
        fused = {r: s for r, s in fused.items() if self._placeable(r)}
        channels = {r: c for r, c in channels.items() if r in fused}
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
        top = self._family_cap_cut(cand_rowids, top_k)
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
            pinout_raw = m["pinout"]
            pinout = json.loads(pinout_raw) if pinout_raw else None
            electrical_raw = m["electrical"] if "electrical" in m.keys() else ""
            electrical = None
            if electrical_raw:
                try:
                    electrical = Electrical.model_validate_json(electrical_raw)
                except Exception:
                    electrical = None
            electrical = self._datasheet_backfill(m["parts"], electrical)
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
                    lcsc=m["lcsc"] or None,
                    pinout=pinout,
                    electrical=electrical,
                    score=round(fused.get(rowid, 0.0), 6),
                    channels=sorted(channels.get(rowid, set())),
                    rank=i + 1,
                )
            )
        return results

    def _family_cap_cut(self, cand_rowids: list[int], top_k: int) -> list[int]:
        """P4-6①:端口集高度重叠的替换品族(USB-C 三兄弟)每族最多占 top_k 里 1 席。

        只影响截断边缘:被挤掉的同族替补按原序回填,保证结果数不变。
        """
        ports_of: dict[int, set[str]] = {}
        for rowid in cand_rowids:
            row = self.conn.execute("SELECT ports FROM blocks WHERE rowid = ?", (rowid,)).fetchone()
            try:
                ports_of[rowid] = {str(p) for p in (json.loads(row[0]) if row and row[0] else [])}
            except json.JSONDecodeError:
                ports_of[rowid] = set()

        def same_family(a: int, b: int) -> bool:
            pa, pb = ports_of[a], ports_of[b]
            shared = len(pa & pb)
            return shared >= _FAMILY_MIN_PORTS and shared >= 0.5 * min(len(pa), len(pb))

        kept: list[int] = []
        deferred: list[int] = []
        for rowid in cand_rowids:
            if len(kept) >= top_k:
                break
            if sum(1 for k in kept if same_family(rowid, k)) >= _FAMILY_CAP:
                deferred.append(rowid)
                continue
            kept.append(rowid)
        for rowid in deferred:
            if len(kept) >= top_k:
                break
            kept.append(rowid)
        return kept

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
            f"SELECT rowid, block_id, name, desc, category, tags, parts, ports, provenance, upstream, lcsc, pinout, electrical FROM blocks WHERE rowid IN ({marks})",
            rowids,
        ).fetchall()
        return {r["rowid"]: r for r in rows}

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]

    # ---- P4-6③ 案例库读写 ----

    def record_case(self, case: CaseRecord) -> bool:
        """回写案例;hash(digest+blocks 规范化 JSON 的 sha256)唯一,重复入库被忽略。

        返回 True=新入库。三护栏之 hash 去重落点;origin/case_id 由调用方带溯源。
        """
        canon = json.dumps(
            {"digest": case.digest.model_dump(), "blocks": sorted(case.block_ids)},
            ensure_ascii=False,
            sort_keys=True,
        )
        h = hashlib.sha256(canon.encode("utf-8")).hexdigest()
        try:
            self.conn.execute(
                "INSERT INTO cases(case_id, name, origin, digest, blocks, hash, created) VALUES(?,?,?,?,?,?,?)",
                (
                    case.case_id,
                    case.name,
                    case.origin,
                    case.digest.model_dump_json(),
                    json.dumps(case.block_ids, ensure_ascii=False),
                    h,
                    case.created,
                ),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def cases(self) -> list[CaseRecord]:
        out: list[CaseRecord] = []
        for row in self.conn.execute("SELECT case_id, name, origin, digest, blocks, created FROM cases"):
            try:
                out.append(
                    CaseRecord(
                        case_id=row[0],
                        name=row[1],
                        origin=row[2],
                        digest=CaseDigest.model_validate_json(row[3]),
                        block_ids=json.loads(row[4]),
                        created=row[5] or "",
                    )
                )
            except Exception:
                continue
        return out

    def close(self) -> None:
        self.conn.close()
