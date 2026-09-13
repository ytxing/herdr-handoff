#!/usr/bin/env python3
"""Small, dependency-free Herdr handoff coordinator."""
import argparse, json, os, select, shutil, sqlite3, subprocess, sys, termios, time, unicodedata, uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("HANDOFF_STATE_DIR", Path.home()/".local/state/handoff"))
DB = ROOT / "handoff.sqlite3"
CLI = str(Path(__file__).resolve())
PROTOCOL_ACK_TIMEOUT = int(os.environ.get("HANDOFF_PROTOCOL_ACK_TIMEOUT", "30"))
PROTOCOL_ACK_RETRIES = int(os.environ.get("HANDOFF_PROTOCOL_ACK_RETRIES", "3"))
EXECUTION_BACKOFF_INITIAL = int(os.environ.get("HANDOFF_EXECUTION_BACKOFF_INITIAL", "120"))
EXECUTION_BACKOFF_MAX = int(os.environ.get("HANDOFF_EXECUTION_BACKOFF_MAX", "28800"))
REVIEW_BACKOFF_INITIAL = int(os.environ.get("HANDOFF_REVIEW_BACKOFF_INITIAL", "120"))
REVIEW_BACKOFF_MAX = int(os.environ.get("HANDOFF_REVIEW_BACKOFF_MAX", "28800"))

def now(): return datetime.now(timezone.utc).isoformat()
def conn():
    ROOT.mkdir(parents=True, exist_ok=True)
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row
    c.execute("""create table if not exists tasks(
      id text primary key, description text not null, prompt text not null,
      source_agent text not null, source_pane text not null, target_agent text not null,
      target_pane text not null, state text not null, action text not null,
      state_since text not null, last_prompt_at text, next_prompt_at text,
      retry_count integer not null default 0, result_file text, last_action text,
      last_action_at text, error text, source_lifecycle text, target_lifecycle text,
      source_presence text, target_presence text)""")
    for name in ("source_tab", "target_tab"):
        try: c.execute(f"alter table tasks add column {name} text")
        except sqlite3.OperationalError: pass
    return c
def transition(c, tid, state, action, last_action=None, error=None):
    c.execute("update tasks set state=?,action=?,state_since=?,last_action=?,last_action_at=?,error=?,retry_count=0 where id=?",
              (state,action,now(),last_action or action,now(),error,tid)); c.commit()
def herdr(*args, timeout=20):
    try:
        p=subprocess.run([os.environ.get("HERDR_BIN_PATH","herdr"),*args],text=True,capture_output=True,timeout=timeout)
        if p.returncode: return None
        return json.loads(p.stdout)
    except Exception: return None
def prompt(agent, text):
    return herdr("agent","prompt",agent,text)
def agent_get(agent): return herdr("agent","get",agent)

def record_identity(c, row, a):
    side = "target" if row["action"] in ("take", "done") else "source"
    c.execute(f"update tasks set {side}_agent=?, {side}_tab=?, {side}_pane=? where id=?",
              (a.agent_name, a.tab, a.pane, row["id"]))
    c.commit()

def delete_tasks(c, ids):
    """Drop tasks by id: unlink each saved result file, then remove the rows. Returns count."""
    ids = list(ids)
    if not ids: return 0
    marks = ",".join("?" * len(ids))
    rows = c.execute("select id,result_file from tasks where id in (%s)" % marks, tuple(ids)).fetchall()
    for item in rows:
        if item["result_file"]:
            try: Path(item["result_file"]).unlink()
            except FileNotFoundError: pass
    c.execute("delete from tasks where id in (%s)" % marks, tuple(ids)); c.commit()
    return len(rows)

def task_text(row):
    """The prompt a Target receives for a task. Shared by `send` and the board's resend key."""
    return ("[HANDOFF TASK]\nTask ID: {id}\nDescription: {description}\n"
            "Source: {source_agent} / {source_pane}\nTarget: {target_agent} / {target_pane}\n\n"
            "Before any work, run:\npython3 {cli} take {id} --agent-name <your-agent> --tab <your-tab> --pane <your-pane>\n\n"
            "Task:\n{prompt}\n\n"
            "On completion run:\npython3 {cli} done {id} --result-file <path> --agent-name <your-agent> --tab <your-tab> --pane <your-pane>\n"
            "If still working run:\npython3 {cli} progress {id}\n"
            'Only if refusing run:\npython3 {cli} reject {id} --reason "<reason>"'
            ).format(cli=CLI, id=row["id"], description=row["description"],
                     source_agent=row["source_agent"], source_pane=row["source_pane"],
                     target_agent=row["target_agent"], target_pane=row["target_pane"],
                     prompt=row["prompt"])

def cmd_send(a):
    if not a.description.strip(): raise SystemExit("description must not be empty")
    c=conn(); tid="t_"+uuid.uuid4().hex[:10]
    if c.execute("select 1 from tasks where state not in ('finished','rejected','cancelled','timeout','target_absent','source_absent') limit 1").fetchone():
        raise SystemExit("an unfinished task already exists")
    # Explicit source and target are required; validate when Herdr is available.
    if agent_get(a.source_agent) is None: raise SystemExit("source agent is absent or herdr is unavailable")
    if agent_get(a.target_agent) is None: raise SystemExit("target agent is absent or herdr is unavailable")
    c.execute("insert into tasks(id,description,prompt,source_agent,source_tab,source_pane,target_agent,target_tab,target_pane,state,action,state_since,next_prompt_at,source_presence,target_presence) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
      (tid,a.description,a.prompt,a.source_agent,a.source_tab,a.source_pane,a.target_agent,a.target_tab,a.target_pane,"published","take",now(),datetime.fromtimestamp(time.time()+PROTOCOL_ACK_TIMEOUT,timezone.utc).isoformat(),"present","present")); c.commit()
    row = {"id":tid, "description":a.description, "prompt":a.prompt,
           "source_agent":a.source_agent, "source_pane":a.source_pane,
           "target_agent":a.target_agent, "target_pane":a.target_pane}
    if prompt(a.target_agent, task_text(row)) is None: transition(c,tid,"timeout","take",error="prompt failed")
    print(tid)

def cmd_action(a):
    c=conn(); row=c.execute("select * from tasks where id=?",(a.id,)).fetchone()
    if not row: raise SystemExit("unknown task")
    if a.cmd in ("take", "done", "done-implicit", "claim", "accept"):
        record_identity(c, row, a)
    if a.cmd=="take": transition(c,a.id,"active","done","take")
    elif a.cmd=="progress": transition(c,a.id,"active","done","progress")
    elif a.cmd=="done":
        p=Path(a.result_file).expanduser()
        if not p.is_file() or not os.access(p,os.R_OK): raise SystemExit("result file is not readable")
        dest=ROOT/"results"/(a.id+".md"); dest.parent.mkdir(exist_ok=True); shutil.copyfile(p,dest)
        c.execute("update tasks set result_file=? where id=?",(str(dest),a.id)); c.commit(); transition(c,a.id,"result_ready","claim","done")
        prompt(row["source_agent"],f"[HANDOFF RESULT READY]\nTask ID: {a.id}\nDescription: {row['description']}\nResult file: {dest}\n\nRun:\npython3 {CLI} claim {a.id}\nThen inspect it and run:\npython3 {CLI} accept {a.id}\nOr:\npython3 {CLI} request-changes {a.id} --reason \"<要求>\"")
    elif a.cmd in ("claim", "accept"): transition(c,a.id,"finished","none",a.cmd)
    elif a.cmd=="reject": transition(c,a.id,"rejected","none","reject",a.reason)
    elif a.cmd=="blocked": transition(c,a.id,"active","reply","blocked",a.reason)
    elif a.cmd=="reply": transition(c,a.id,"active","done","reply",a.message)
    elif a.cmd=="request-changes": raise SystemExit("request-changes was removed; send a new task")
    elif a.cmd=="cancel": transition(c,a.id,"cancelled","none","cancel")
    elif a.cmd=="delete":
        if a.id: ids = [r["id"] for r in c.execute("select id from tasks where id=?", (a.id,))]
        elif a.state: ids = [r["id"] for r in c.execute("select id from tasks where state=?", (a.state,))]
        else: ids = [r["id"] for r in c.execute("select id from tasks")]
        print(f"deleted {delete_tasks(c, ids)} task(s)")
    elif a.cmd=="done-implicit":
        p=Path(a.result_file).expanduser()
        if not p.is_file(): raise SystemExit("result file is not readable")
        dest=ROOT/"results"/(a.id+".md"); dest.parent.mkdir(exist_ok=True)
        if p.resolve() != dest.resolve(): shutil.copyfile(p,dest)
        c.execute("update tasks set result_file=? where id=?",(str(dest),a.id)); c.commit(); transition(c,a.id,"result_ready","claim","done-implicit")

def cmd_list(_):
    for r in conn().execute("select * from tasks order by state_since"):
        age=int(time.time()-datetime.fromisoformat(r["state_since"]).timestamp())
        print(f"{r['id']}\t{r['description'].replace(chr(10),' / ')}\t{r['source_agent']}/{r['source_pane']}\t{r['target_agent']}/{r['target_pane']}\t{r['state']}\t{r['action']}\t{age}s")
def daemon(a):
    if a.op=="status": print("running" if (ROOT/"daemon.pid").exists() else "stopped"); return
    if a.op=="stop":
        try: (ROOT/"daemon.stop").touch()
        except: pass
        return
    ROOT.mkdir(parents=True,exist_ok=True)
    try: (ROOT/"daemon.stop").unlink()
    except FileNotFoundError: pass
    (ROOT/"daemon.pid").write_text(str(os.getpid())); print("daemon started")
    try:
        while not (ROOT/"daemon.stop").exists():
            c=conn()
            for r in c.execute("select * from tasks where state not in ('finished','rejected','cancelled','stopped','timeout')").fetchall():
                ag=r["target_agent"] if r["action"] in ("take","done") else r["source_agent"]
                info=agent_get(ag); lifecycle="unknown"; present="absent" if info is None else "present"
                if info:
                    result = info.get("result", info) if isinstance(info,dict) else {}
                    agent_info = result.get("agent", result) if isinstance(result,dict) else {}
                    lifecycle = agent_info.get("agent_status", agent_info.get("status", "unknown")) if isinstance(agent_info,dict) else "unknown"
                c.execute(f"update tasks set {'target' if ag==r['target_agent'] else 'source'}_lifecycle=?, {'target' if ag==r['target_agent'] else 'source'}_presence=? where id=?",(lifecycle,present,r['id'])); c.commit()
                if present=="absent": transition(c,r["id"],"target_absent" if ag==r["target_agent"] else "source_absent","none",error="Herdr Agent absent"); continue
                if lifecycle=="working":
                    # Herdr owns the wait; this time is outside task backoff.
                    waited = herdr("agent","wait",ag,"--until","idle", timeout=None)
                    if waited is None:
                        c.execute("update tasks set error=? where id=?", ("Herdr agent wait failed", r['id'])); c.commit()
                        continue
                if time.time() >= datetime.fromisoformat((r["next_prompt_at"] or now())).timestamp():
                    retries = r["retry_count"]
                    protocol = r["state"] in ("published", "result_ready")
                    if protocol and retries >= PROTOCOL_ACK_RETRIES:
                        transition(c, r["id"], "timeout", "none", error="protocol acknowledgement timeout")
                        continue
                    prompt(ag,f"[HANDOFF REMINDER]\nTask ID: {r['id']}\nDescription: {r['description']}\nRequired command: python3 {CLI} {r['action']} {r['id']}")
                    if protocol:
                        delay = PROTOCOL_ACK_TIMEOUT
                    else:
                        delay = min(EXECUTION_BACKOFF_MAX, EXECUTION_BACKOFF_INITIAL * (2 ** retries))
                    c.execute("update tasks set last_prompt_at=?,next_prompt_at=?,retry_count=retry_count+1 where id=?",(now(),datetime.fromtimestamp(time.time()+delay,timezone.utc).isoformat(),r['id'])); c.commit()
            time.sleep(5)
    finally:
        for p in (ROOT/"daemon.pid",ROOT/"daemon.stop"):
            try:p.unlink()
            except:pass
# ---------- board rendering ----------

CLOSED_STATES = ("finished","rejected","cancelled","timeout",
                 "target_absent","source_absent")
STATE_LABEL = {"published":"published","active":"active","result_ready":"result ready",
               "finished":"finished","rejected":"rejected",
               "cancelled":"cancelled","timeout":"timeout",
               "target_absent":"target absent","source_absent":"source absent"}
STATE_STYLE = {"published":("blue",),"active":("cyan",),"result_ready":("magenta",),
               "finished":("green",),"rejected":("red",),
               "cancelled":("dim",),"timeout":("red",),
               "target_absent":("red",),"source_absent":("red",)}
ACTION_LABEL = {"take":"take","done":"done","claim":"claim","accept":"accept",
                "reply":"reply","source_reply":"reply","none":"—"}
# No bold anywhere: weight is carried by colour alone. The bold codes are deliberately
# absent from this table so a stray `("boldred",)` fails to render instead of creeping back.
_CODES = {"dim":"2","red":"31","green":"32","yellow":"33","blue":"34","magenta":"35","cyan":"36"}
_ANSI_ON = False

def _use_color():
    global _ANSI_ON
    _ANSI_ON = (sys.stdout.isatty() and os.environ.get("TERM","") not in ("","dumb")
                and "NO_COLOR" not in os.environ)
    return _ANSI_ON

def _cw(ch):
    """Terminal columns for one character. CJK counts as 2."""
    return 2 if unicodedata.east_asian_width(ch) in ("W","F") else 1

def _dw(s): return sum(_cw(ch) for ch in s)

def _fit(s, width):
    if width <= 0: return ""
    if _dw(s) <= width: return s
    if width == 1: return "…"
    out, w = "", 0
    for ch in s:
        if w + _cw(ch) > width - 1: break
        out += ch; w += _cw(ch)
    return out + "…"

def _fit_segments(segs, width):
    """Trim a [(text, styles)] list to `width` display columns. Returns (segments, used)."""
    out, used = [], 0
    for text, styles in segs:
        if used + _dw(text) > width:
            room = width - used
            if room > 1:
                piece = _fit(text, room)
                out.append((piece, styles)); used += _dw(piece)
            break
        out.append((text, styles)); used += _dw(text)
    return out, used

def _pad(s, width, right=False):
    gap = max(0, width - _dw(s))
    return " "*gap + s if right else s + " "*gap

def _paint(s, *styles):
    flat = []
    for st in styles: flat.extend(st if isinstance(st,(tuple,list)) else (st,))
    if not flat or not _ANSI_ON: return s
    return "".join("\033[%sm" % _CODES[x] for x in flat if x in _CODES) + s + "\033[0m"

def _human_age(sec):
    sec = max(0, int(sec))
    if sec < 60: return "%ds" % sec
    if sec < 3600: return "%dm" % (sec // 60)
    if sec < 86400: return "%dh%02dm" % (sec // 3600, sec % 3600 // 60)
    return "%dd%02dh" % (sec // 86400, sec % 86400 // 3600)

STATUS_STYLE = {"idle":("green",), "done":("green",), "working":("yellow",),
                "blocked":("red",), "unknown":("dim",)}
# Herdr reports `idle` and `done` alike as "ready for input"; both count as safe to prompt.
READY_STATUSES = ("idle","done")
STATUS_TTL = 1.0
_STATUS_CACHE = {"at":0.0, "map":{}}

def agent_statuses(max_age=STATUS_TTL):
    """Map agent name AND pane id -> agent_status. One herdr call covers every agent."""
    if time.time() - _STATUS_CACHE["at"] < max_age: return _STATUS_CACHE["map"]
    data = herdr("agent","list", timeout=5)
    if not data: return _STATUS_CACHE["map"]     # keep the last known map; retry next tick
    m = {}
    for ag in ((data.get("result") or {}).get("agents") or []):
        st = ag.get("agent_status")
        if not st: continue
        if ag.get("name"): m[ag["name"]] = st
        if ag.get("pane_id"): m[ag["pane_id"]] = st
    _STATUS_CACHE.update(at=time.time(), map=m)
    return m

def _lookup_status(statuses, name, pane):
    st = statuses.get(name) if name else None
    return st if st is not None else (statuses.get(pane) if pane else None)

def daemon_running():
    """True only if the pid file names a live process — a stale file must not read as running."""
    pidfile = ROOT/"daemon.pid"
    if not pidfile.exists(): return False
    try:
        os.kill(int(pidfile.read_text().strip()), 0); return True
    except (ValueError, OSError): return False

def board_items():
    """Query tasks and enrich each with routing, computed age and ownership."""
    me = os.environ.get("HERDR_PANE_ID")
    c = conn()
    try: rows = c.execute("select * from tasks order by state_since").fetchall()
    finally: c.close()                      # the board redraws every second; do not leak connections
    items = []
    for r in rows:
        action = r["action"]; to_target = action in ("take","done")
        actor = r["target_agent"] if to_target else r["source_agent"]
        actor_pane = r["target_pane"] if to_target else r["source_pane"]
        items.append({
            "id": r["id"], "desc": r["description"].replace("\n"," / "),
            "src_agent": r["source_agent"], "src_pane": r["source_pane"],
            "dst_agent": r["target_agent"], "dst_pane": r["target_pane"],
            "route": "%s → %s" % (r["source_agent"], r["target_agent"]),
            "state": r["state"], "action": action, "actor": actor,
            "mine": (bool(me) and actor_pane == me and action != "none"
                     and r["state"] not in CLOSED_STATES),
            "closed": r["state"] in CLOSED_STATES,
            "age": _human_age(time.time() - datetime.fromisoformat(r["state_since"]).timestamp()),
            "since": r["state_since"],
        })
    # live tasks first, finished ones sink to the bottom
    items.sort(key=lambda x: (x["closed"], x["since"]))
    return items

MARK_W = 4          # cursor glyph + "[x]" checkbox
LEGEND = [("↑↓", "move"), ("space", "select"), ("a", "all"), ("r", "resend"),
          ("d", "delete"), ("t", "daemon"), ("q", "quit")]

def _legend_segments():
    """Keys rendered bright so they stand out from their dim descriptions."""
    segs = []
    for idx, (key, label) in enumerate(LEGEND):
        if idx: segs.append(("  ·  ", ("dim",)))
        segs.append((key, ("cyan",)))
        segs.append((" " + label, ("dim",)))
    return segs

def render_board(width=100, selected=None, cursor=None, statuses=None, items=None, message=None):
    selected = selected or frozenset()
    if statuses is None: statuses = agent_statuses()
    if items is None: items = board_items()
    for i in items:
        i["src_status"] = _lookup_status(statuses, i["src_agent"], i["src_pane"])
        i["dst_status"] = _lookup_status(statuses, i["dst_agent"], i["dst_pane"])

    def next_cell(i):
        if i["action"] == "none": return "—", ("dim",)
        label = ACTION_LABEL.get(i["action"], i["action"])
        if i["mine"]: return "▶ %s" % label, ("yellow",)
        return "%s · %s" % (label, i["actor"]), ("dim",)

    def name_of(i, which): return i["src_agent"] if which == "src" else i["dst_agent"]
    def stat_of(i, which): return i["src_status"] if which == "src" else i["dst_status"]

    def widest(header, values):
        return max([_dw(header)] + [_dw(v) for v in values])

    kid = widest("ID", [i["id"] for i in items])
    kst = widest("STATE", [STATE_LABEL.get(i["state"], i["state"]) for i in items])
    kag = widest("AGE", [i["age"] for i in items])
    knx = widest("ACTION", [next_cell(i)[0] for i in items])
    krt = widest("ROUTE", [i["route"] for i in items])
    ksn = widest("SRC", [name_of(i,"src") for i in items])
    kdn = widest("DST", [name_of(i,"dst") for i in items])

    def status_cell(i, which, dim):
        """(plain, segments) for one SRC/DST cell.

        Names are padded to the column's widest so the status words line up down the board,
        and the name is left unstyled while only the status carries colour -- the eye
        should land on the state, not on the agent name. The status keeps that colour on a
        dimmed row too: the STATE column does, and greying one but not the other left a
        finished row with a coloured state and apparently uncoloured agent statuses.
        """
        name, st = name_of(i, which), stat_of(i, which)
        name_w = ksn if which == "src" else kdn
        word = "absent" if st is None else st
        style = ("red",) if st is None else STATUS_STYLE.get(st, ("dim",))
        name_text = _pad(name, name_w) + " "
        return name_text + word, [(name_text, ("dim",) if dim else ()), (word, style)]

    ksf = widest("SRC", [status_cell(i,"src",False)[0] for i in items])
    kdf = widest("DST", [status_cell(i,"dst",False)[0] for i in items])

    # Narrowing ladder: full status columns, then names only, then one ROUTE column, then drop NEXT.
    routing, show_action = "full", True
    MIN_DESC = 12
    def routing_w():
        if routing == "full":  return ksf + 2 + kdf + 2
        if routing == "names": return ksn + 2 + kdn + 2
        if routing == "route": return krt + 2
        return 0
    def fixed():
        w = MARK_W + kid + kst + kag + 2*4 + routing_w()
        if show_action: w += knx + 2
        return w
    while width - fixed() < MIN_DESC:
        if routing == "full": routing = "names"
        elif routing == "names": routing = "route"
        elif show_action: show_action = False
        elif routing == "route": routing = "none"
        else: break
    desc_w = max(4, width - fixed())

    def row(cells):
        """Each cell is (text_or_segments, styles, width, align); last cell is not padded."""
        parts = []
        for idx, (text, styles, w, align) in enumerate(cells):
            if isinstance(text,(list,tuple)):
                segs = list(text)
            else:
                if _dw(text) > w: text = _fit(text, w)   # a cell must never exceed its column
                segs = [(text, styles)]
            if idx < len(cells)-1:
                gap = max(0, w - _dw("".join(s for s,_ in segs)))
                segs = ([(" "*gap, ())] + segs) if align == "r" else (segs + [(" "*gap, ())])
            parts.append("".join(_paint(s, st) for s, st in segs))
        return "  ".join(parts).rstrip()

    def routing_cells(i, dim, header):
        if header:
            if routing == "full":  return [("SRC",("dim",),ksf,"l"), ("DST",("dim",),kdf,"l")]
            if routing == "names": return [("SRC",("dim",),ksn,"l"), ("DST",("dim",),kdn,"l")]
            if routing == "route": return [("ROUTE",("dim",),krt,"l")]
            return []
        if routing == "full":  return [(status_cell(i,"src",dim)[1],(),ksf,"l"),
                                       (status_cell(i,"dst",dim)[1],(),kdf,"l")]
        if routing == "names": return [(name_of(i,"src"),dim,ksn,"l"), (name_of(i,"dst"),dim,kdn,"l")]
        if routing == "route": return [(i["route"], dim, krt, "l")]
        return []

    def assemble(mark, _id, desc, state, nxt, age, ms, ds, ss, ns, as_, i=None, header=False):
        cells = [(mark, ms, MARK_W, "l"), (_id, ds, kid, "l"), (desc, ds, desc_w, "l")]
        cells += routing_cells(i, ds, header)
        cells.append((state, ss, kst, "l"))
        if show_action: cells.append((nxt, ns, knx, "l"))
        cells.append((age, as_, kag, "r"))
        return row(cells)

    n = len(items)
    awaiting = sum(1 for i in items if i["mine"] and not i["closed"])
    daemon_on = daemon_running()
    # Header is built from segments so it can be truncated instead of overrunning a narrow pane.
    segs = [("Handoff", ()), (" · %d task%s" % (n, "" if n == 1 else "s"), ("dim",))]
    if awaiting: segs.append((" · %d awaiting you" % awaiting, ("yellow",)))
    segs += [(" · daemon ", ("dim",)),
             ("on" if daemon_on else "off", ("green",) if daemon_on else ("dim",))]
    clock = datetime.now().strftime("%H:%M:%S")
    head, used = _fit_segments(segs, max(0, width - len(clock) - 1))
    lines = ["".join(_paint(t, *s) for t, s in head)
             + " " * max(1, width - used - len(clock)) + _paint(clock, "dim"),
             _paint("─" * max(1, width), "dim")]
    lines.append(assemble("", "ID", "DESCRIPTION", "STATE", "ACTION", "AGE",
                          ("dim",), ("dim",), ("dim",), ("dim",), ("dim",), header=True))
    if not items:
        lines.append(_paint("  no tasks yet — handoff send ... to create one", ("dim",)))
    for idx, i in enumerate(items):
        ds = ("dim",) if i["closed"] else ()
        nxt, ns = next_cell(i)
        glyph = "❯" if idx == cursor else ("▸" if i["mine"] else " ")
        box = "[x]" if i["id"] in selected else "[ ]"
        ms = ("cyan",) if idx == cursor else (("yellow",) if i["mine"] else ds)
        lines.append(assemble(glyph + box, i["id"], _fit(i["desc"], desc_w),
                              STATE_LABEL.get(i["state"], i["state"]), nxt, i["age"],
                              ms, ds, STATE_STYLE.get(i["state"], ()), ns, ds, i=i))
    # The last two lines are fixed furniture: the message line, then the key legend.
    # The message line is reserved even when empty so the board never shifts under a keypress.
    lines.append("")
    lines.append(_paint(_fit(message[0], width), *message[1]) if message else "")
    lines.append("".join(_paint(t, *s) for t, s in _fit_segments(_legend_segments(), width)[0]))
    return "\n".join(lines)

# ---------- interactive board ----------

def _read_key(timeout):
    """Return a key name ("UP"/"DOWN"/"ESC"), a literal character, or None on timeout.

    Reads the raw fd, never sys.stdin. The buffered reader would swallow a whole escape
    sequence into Python's buffer, leaving the fd empty so the next select() reports no
    data: every arrow key would degrade to a bare Escape and the leftover bytes would
    desynchronise the keys that follow.
    """
    fd = sys.stdin.fileno()
    def ready(t): return bool(select.select([fd], [], [], t)[0])
    if not ready(timeout): return None
    ch = os.read(fd, 1).decode("utf-8", "replace")
    if ch != "\033": return ch
    if not ready(0.03): return "ESC"                                       # bare Escape
    if os.read(fd, 1) != b"[": return "ESC"
    if not ready(0.03): return "ESC"
    return {b"A":"UP", b"B":"DOWN", b"C":"RIGHT", b"D":"LEFT"}.get(os.read(fd, 1))

def toggle_daemon():
    """The daemon is a foreground process, so start it detached; stop just drops the stop file."""
    if daemon_running():
        try: (ROOT/"daemon.stop").touch()
        except OSError: return "daemon stop failed"
        return "daemon stopping"
    subprocess.Popen([sys.executable, CLI, "daemon", "start"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return "daemon starting"

def resend_tasks(ids, statuses):
    """Re-deliver each task's prompt, but only to a Target that is ready for input."""
    c = conn()
    try: rows = [c.execute("select * from tasks where id=?", (tid,)).fetchone() for tid in ids]
    finally: c.close()
    sent, skipped, failed = [], [], []
    for row in rows:
        if not row: continue
        st = _lookup_status(statuses, row["target_agent"], row["target_pane"])
        if st not in READY_STATUSES:
            skipped.append("%s(%s)" % (row["target_agent"], st or "absent")); continue
        if prompt(row["target_agent"], task_text(row)) is None: failed.append(row["target_agent"])
        else: sent.append(row["target_agent"])
    return sent, skipped, failed

def ui(_):
    if not sys.stdout.isatty():
        print(render_board(shutil.get_terminal_size((110, 30)).columns)); return
    _use_color()
    fd = sys.stdin.fileno(); saved = termios.tcgetattr(fd)
    selected, cursor = set(), 0
    mode, pending, flash = None, [], None
    statuses, last_status = {}, 0.0
    try:
        attr = termios.tcgetattr(fd)
        attr[3] &= ~(termios.ICANON | termios.ECHO)   # cbreak; keep ISIG so Ctrl-C still works
        termios.tcsetattr(fd, termios.TCSADRAIN, attr)
        sys.stdout.write("\033[?25l\033[H\033[2J")
        while True:
            width = shutil.get_terminal_size((110, 30)).columns
            if time.time() - last_status >= STATUS_TTL:
                statuses = agent_statuses(max_age=0); last_status = time.time()
            items = board_items()
            if selected: selected &= {i["id"] for i in items}   # drop ids that vanished
            cursor = max(0, min(cursor, len(items)-1)) if items else 0
            if flash and time.time() > flash[1]: flash = None
            if mode == "confirm_delete":
                message = ("Delete %d task(s)? %s   [y/N]" % (len(pending), ", ".join(pending)),
                           ("red",))
            elif flash: message = (flash[0], ("yellow",))
            else: message = None
            sys.stdout.write("\033[H" + render_board(width, selected, cursor,
                                                     statuses, items, message) + "\033[J")
            sys.stdout.flush()

            key = _read_key(1.0)
            if key is None: continue
            if mode == "confirm_delete":
                if key in ("y","Y","\r","\n"):
                    c = conn()
                    try: n = delete_tasks(c, pending)
                    finally: c.close()
                    selected -= set(pending)
                    flash = ("Deleted %d task(s)" % n, time.time()+3)
                else:
                    flash = ("Delete cancelled", time.time()+2)
                mode, pending = None, []
                continue
            if key in ("q","Q"): break
            if key in ("UP","k"): cursor = max(0, cursor-1)
            elif key in ("DOWN","j"):
                if items: cursor = min(len(items)-1, cursor+1)
            elif key == " ":
                if items:
                    tid = items[cursor]["id"]
                    if tid in selected: selected.discard(tid)
                    else: selected.add(tid)
            elif key == "a":
                ids = {i["id"] for i in items}
                selected = set() if ids and selected >= ids else set(ids)
            elif key == "r":
                if not selected: flash = ("Nothing selected - space to pick a row", time.time()+3)
                else:
                    sent, skipped, failed = resend_tasks(sorted(selected), statuses)
                    parts = []
                    if sent: parts.append("resent: " + ", ".join(sent))
                    if skipped: parts.append("skipped not-ready: " + ", ".join(skipped))
                    if failed: parts.append("failed: " + ", ".join(failed))
                    flash = (" · ".join(parts) or "nothing to do", time.time()+4)
            elif key == "d":
                if not selected: flash = ("Nothing selected - space to pick a row", time.time()+3)
                else: pending, mode = sorted(selected), "confirm_delete"
            elif key == "t":
                flash = (toggle_daemon(), time.time()+3)
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write("\033[?25h\n")
        sys.stdout.flush()
def main():
    p=argparse.ArgumentParser(); sp=p.add_subparsers(dest="op",required=True)
    s=sp.add_parser("send"); s.add_argument("--source-agent",required=True); s.add_argument("--source-tab",required=True); s.add_argument("--source-pane",required=True); s.add_argument("--target-agent",required=True); s.add_argument("--target-tab",required=True); s.add_argument("--target-pane",required=True); s.add_argument("--description",required=True); s.add_argument("--prompt",required=True); s.set_defaults(fn=cmd_send)
    for n in ("take","progress","claim","accept","reject","blocked","reply","cancel","done-implicit"):
        x=sp.add_parser(n); x.add_argument("id"); x.add_argument("--reason", "--description", dest="reason", default=""); x.add_argument("--result-file"); x.set_defaults(fn=cmd_action,cmd=n)
        if n in ("take", "claim", "accept", "done-implicit"):
            x.add_argument("--agent-name", required=True); x.add_argument("--tab", required=True); x.add_argument("--pane", required=True)
    sp_reply = sp.choices["reply"]; sp_reply.add_argument("--message", required=True)
    x=sp.add_parser("delete"); x.add_argument("id", nargs="?"); x.add_argument("--state"); x.add_argument("--all", action="store_true"); x.set_defaults(fn=cmd_action,cmd="delete")
    d=sp.add_parser("done"); d.add_argument("id"); d.add_argument("--result-file",required=True); d.add_argument("--implicit-take",action="store_true"); d.add_argument("--agent-name",required=True); d.add_argument("--tab",required=True); d.add_argument("--pane",required=True); d.set_defaults(fn=cmd_action,cmd="done")
    l=sp.add_parser("list"); l.set_defaults(fn=cmd_list)
    d=sp.add_parser("daemon"); d.add_argument("op",choices=("start","stop","status")); d.set_defaults(fn=daemon)
    u=sp.add_parser("ui"); u.set_defaults(fn=ui)
    a=p.parse_args(); a.fn(a)
if __name__=="__main__": main()
