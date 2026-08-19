"""Tamper-evident append-only event log with SQLite or plain-JSON storage."""
from __future__ import annotations
import hashlib, json, os, sqlite3, threading, time
from dataclasses import dataclass, asdict
GENESIS = "0" * 64

def _canonical(obj): return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
def _atomic_json_write(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)
def event_hash(prev_hash, seq, ts_ns, kind, profile, payload):
    return hashlib.sha256(_canonical({"prev": prev_hash, "seq": seq, "ts_ns": ts_ns,
        "kind": kind, "profile": profile, "payload": payload}).encode()).hexdigest()

@dataclass(frozen=True)
class Event:
    seq: int; ts_ns: int; kind: str; profile: str; payload: dict; prev_hash: str; hash: str

class EventLog:
    def __init__(self, path=":memory:", profile="DEV", clock=None):
        self.profile, self.path = profile, path
        self._clock = clock or (lambda: time.time_ns())
        self._lock = threading.Lock()
        self.backend = "json" if path != ":memory:" and path.lower().endswith(".json") else "sqlite"
        if self.backend == "json":
            self.db = None; self._events = []; self._load_json()
        else:
            self.db = sqlite3.connect(path, check_same_thread=False)
            self.db.execute("""CREATE TABLE IF NOT EXISTS events(
                seq INTEGER PRIMARY KEY, ts_ns INTEGER NOT NULL, kind TEXT NOT NULL,
                profile TEXT NOT NULL, payload TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL)""")
            self.db.commit()

    def _load_json(self):
        if not os.path.exists(self.path): _atomic_json_write(self.path, []); return
        with open(self.path, encoding="utf-8") as f: rows = json.load(f)
        if not isinstance(rows, list): raise ValueError("JSON event log must contain a list")
        self._events = [Event(int(r["seq"]), int(r["ts_ns"]), str(r["kind"]), str(r["profile"]),
            dict(r["payload"]), str(r["prev_hash"]), str(r["hash"])) for r in rows]

    def _save_json(self): _atomic_json_write(self.path, [asdict(e) for e in self._events])

    def append(self, kind, payload):
        with self._lock:
            if self.backend == "json":
                cur = self._events[-1] if self._events else None
                seq, prev, ts = (cur.seq + 1 if cur else 0), (cur.hash if cur else GENESIS), self._clock()
                e = Event(seq, ts, kind, self.profile, payload, prev, event_hash(prev, seq, ts, kind, self.profile, payload))
                self._events.append(e); self._save_json(); return e
            cur = self.db.execute("SELECT seq,hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            seq, prev, ts = ((cur[0]+1) if cur else 0), (cur[1] if cur else GENESIS), self._clock()
            h = event_hash(prev, seq, ts, kind, self.profile, payload)
            self.db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?)", (seq, ts, kind, self.profile, _canonical(payload), prev, h)); self.db.commit()
            return Event(seq, ts, kind, self.profile, payload, prev, h)

    def all(self):
        if self.backend == "json": return list(self._events)
        rows = self.db.execute("SELECT seq,ts_ns,kind,profile,payload,prev_hash,hash FROM events ORDER BY seq").fetchall()
        return [Event(r[0],r[1],r[2],r[3],json.loads(r[4]),r[5],r[6]) for r in rows]
    def filter(self, kind): return [e for e in self.all() if e.kind == kind]
    def verify(self):
        prev = GENESIS
        for e in self.all():
            expect = event_hash(prev,e.seq,e.ts_ns,e.kind,e.profile,e.payload)
            if e.prev_hash != prev: return False, f"seq {e.seq}: prev_hash mismatch"
            if e.hash != expect: return False, f"seq {e.seq}: hash mismatch (content altered)"
            prev = e.hash
        return True, "chain intact"
    def head(self):
        if self.backend == "json": return self._events[-1].hash if self._events else GENESIS
        cur = self.db.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone(); return cur[0] if cur else GENESIS
    def reconstruct_positions(self):
        pos, cash = {}, None
        for e in self.all():
            if e.kind == "BALANCE": cash = e.payload.get("cash", cash)
            elif e.kind == "FILL":
                p=e.payload; q=p["qty"]*(1 if p["side"]=="BUY" else -1); pos[p["symbol"]]=pos.get(p["symbol"],0.0)+q
                if pos[p["symbol"]] <= 1e-12: pos.pop(p["symbol"],None)
        return {"cash": cash, "positions": pos}
    def checkpoint(self, path, signing_key):
        import hmac
        key = signing_key.encode() if isinstance(signing_key,str) else signing_key
        if not key or len(key)<32: raise ValueError("audit signing key must be at least 32 bytes")
        body={"profile":self.profile,"head":self.head(),"ts_ns":self._clock()}; sig=hmac.new(key,_canonical(body).encode(),hashlib.sha256).hexdigest(); record={**body,"signature":sig}
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
        try: os.write(fd,(_canonical(record)+"\n").encode()); os.fsync(fd)
        finally: os.close(fd)
        return record
    @staticmethod
    def verify_checkpoint(record, signing_key):
        import hmac
        key=signing_key.encode() if isinstance(signing_key,str) else signing_key
        try: sig=record["signature"]; body={k:record[k] for k in ("profile","head","ts_ns")}
        except (KeyError,TypeError): return False,"malformed checkpoint"
        expect=hmac.new(key,_canonical(body).encode(),hashlib.sha256).hexdigest()
        return (True,"checkpoint signature valid") if hmac.compare_digest(sig,expect) else (False,"checkpoint signature invalid")
