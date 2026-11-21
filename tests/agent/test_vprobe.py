import json, os, stat, pytest, agent.audit
from agent.ask import ask, NoPersonaError
from agent.model import ToolCall
from agent.audit import AuditWriteFailed, FileLog
Q = "What share of the City of Chicago's served payment rows repeat another served row?"
PLANT = "INVENTED-VPROBE-Marker-0000"

class ShownThenFails:
    def step(self, turn):
        if not turn.results:
            return ToolCall("duplicate-payment-rate")
        print("MODEL SAW ROWS:", turn.results[0].rows)
        raise TimeoutError("model endpoint timed out")

def test_cor1(as_persona, audit):
    with pytest.raises(TimeoutError):
        ask("accountant-chicago", "q", as_persona=as_persona, audit=audit, model=ShownThenFails())
    print("COR1 LOG EXISTS:", audit.log.path.exists())

def test_leak1(as_persona, audit, monkeypatch):
    real = agent.audit._write_all
    def torn(fd, data):
        if data.endswith(b"\n"): raise OSError("disk went away")
        real(fd, data)
    monkeypatch.setattr(agent.audit, "_write_all", torn)
    with pytest.raises(AuditWriteFailed):
        ask("accountant-chicago", Q + " " + PLANT, as_persona=as_persona, audit=audit)
    print("LEAK1 LOG:", audit.log.path.read_bytes(), "KEPT:", sorted(p.name for p in audit.log.kept.iterdir()),
          "PLANT in kept:", PLANT in (audit.log.kept / "0.json").read_text())

def test_leak2(as_persona, audit, monkeypatch):
    real, n = os.fsync, [0]
    def bad(fd):
        n[0] += 1
        if n[0] == 1: raise OSError("fsync failed")
        real(fd)
    monkeypatch.setattr(agent.audit.os, "fsync", bad)
    with pytest.raises(AuditWriteFailed):
        ask("accountant-chicago", Q + " " + PLANT, as_persona=as_persona, audit=audit)
    monkeypatch.setattr(agent.audit.os, "fsync", real)
    print("LEAK2 LOG exists:", audit.log.path.exists() and audit.log.path.read_bytes(), "KEPT:", sorted(p.name for p in audit.log.kept.iterdir()),
          "PLANT in tmp:", PLANT in (audit.log.kept / ".0.json.tmp").read_text())

def test_clm3_fsync_line(as_persona, audit, monkeypatch):
    real = os.fsync; n = []
    def f(fd):
        n.append(fd)
        if len(n) == 3: raise OSError("x")
        return real(fd)
    monkeypatch.setattr(agent.audit.os, "fsync", f)
    with pytest.raises(AuditWriteFailed):
        ask("accountant-chicago", Q, as_persona=as_persona, audit=audit)
    print("CLM3 calls", len(n), "log", audit.log.path.read_bytes())

def test_clm4(audit):
    def boom(p): raise NoPersonaError("x")
    with pytest.raises(NoPersonaError):
        ask("accountant-chicago", Q, as_persona=boom, audit=audit)
    print("CLM4 log exists", audit.log.path.exists())

def test_grd1(tmp_path):
    k = tmp_path/'k2'; k.mkdir(); os.chmod(k, 0o755)
    t = k/'.0.json.tmp'; t.write_text('x'); os.chmod(t, 0o644)
    FileLog(tmp_path/'log.jsonl', k)._keep(0, {"question": ["s", "v"]})
    print("GRD1", oct(stat.S_IMODE(k.stat().st_mode)), oct(stat.S_IMODE((k/'0.json').stat().st_mode)))
    k3 = tmp_path/'fresh'/'a'/'kept'
    FileLog(tmp_path/'l2', k3)._keep(0, {"question": ["s", "v"]})
    print("GRD1 fresh", oct(stat.S_IMODE(k3.stat().st_mode)), oct(stat.S_IMODE(k3.parent.stat().st_mode)), oct(stat.S_IMODE((k3/'0.json').stat().st_mode)))
