#!/usr/bin/env python3
"""Small, dependency-free Herdr handoff coordinator."""
import argparse, json, os, shutil, sqlite3, subprocess, sys, time, unicodedata, uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("HANDOFF_STATE_DIR", Path.home()/".local/state/handoff"))
DB = ROOT / "handoff.sqlite3"
CLI = str(Path(__file__).resolve())

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
    return c
def transition(c, tid, state, action, last_action=None, error=None):
    c.execute("update tasks set state=?,action=?,state_since=?,last_action=?,last_action_at=?,error=? where id=?",
              (state,action,now(),last_action or action,now(),error,tid)); c.commit()
def herdr(*args):
    try:
        p=subprocess.run([os.environ.get("HERDR_BIN_PATH","herdr"),*args],text=True,capture_output=True,timeout=20)
        if p.returncode: return None
        return json.loads(p.stdout)
    except Exception: return None
def prompt(agent, text):
    return herdr("agent","prompt",agent,text)
def agent_get(agent): return herdr("agent","get",agent)

def cmd_send(a):
    if not a.description.strip(): raise SystemExit("description must not be empty")
    c=conn(); tid="t_"+uuid.uuid4().hex[:10]
    # Explicit source and target are required; validate when Herdr is available.
    if agent_get(a.source_agent) is None: raise SystemExit("source agent is absent or herdr is unavailable")
    if agent_get(a.target_agent) is None: raise SystemExit("target agent is absent or herdr is unavailable")
    c.execute("insert into tasks(id,description,prompt,source_agent,source_pane,target_agent,target_pane,state,action,state_since,next_prompt_at,source_presence,target_presence) values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
      (tid,a.description,a.prompt,a.source_agent,a.source_pane,a.target_agent,a.target_pane,"published","take",now(),now(),"present","present")); c.commit()
    text=f"[HANDOFF TASK]\nTask ID: {tid}\nDescription: {a.description}\nSource: {a.source_agent} / {a.source_pane}\nTarget: {a.target_agent} / {a.target_pane}\n\nBefore any work, run:\npython3 {CLI} take {tid}\n\nTask:\n{a.prompt}\n\nOn completion run:\npython3 {CLI} done {tid} --result-file <path>\nIf still working run:\npython3 {CLI} progress {tid}\nOnly if refusing run:\npython3 {CLI} reject {tid} --reason \"<reason>\""
    if prompt(a.target_agent,text) is None: transition(c,tid,"timeout","take",error="prompt failed")
    print(tid)

def cmd_action(a):
    c=conn(); row=c.execute("select * from tasks where id=?",(a.id,)).fetchone()
    if not row: raise SystemExit("unknown task")
    if a.cmd=="take": transition(c,a.id,"active","done","take")
    elif a.cmd=="progress": transition(c,a.id,"active","done","progress")
    elif a.cmd=="done":
        p=Path(a.result_file).expanduser()
        if not p.is_file() or not os.access(p,os.R_OK): raise SystemExit("result file is not readable")
        dest=ROOT/"results"/(a.id+".md"); dest.parent.mkdir(exist_ok=True); shutil.copyfile(p,dest)
        c.execute("update tasks set result_file=? where id=?",(str(dest),a.id)); c.commit(); transition(c,a.id,"result_ready","claim","done")
        prompt(row["source_agent"],f"[HANDOFF RESULT READY]\nTask ID: {a.id}\nDescription: {row['description']}\nResult file: {dest}\n\nRun:\npython3 {CLI} claim {a.id}\nThen inspect it and run:\npython3 {CLI} accept {a.id}\nOr:\npython3 {CLI} request-changes {a.id} --reason \"<要求>\"")
    elif a.cmd=="claim": transition(c,a.id,"reviewing","accept","claim")
    elif a.cmd=="accept": transition(c,a.id,"finished","none","accept")
    elif a.cmd=="reject": transition(c,a.id,"rejected","none","reject",a.reason)
    elif a.cmd=="blocked": transition(c,a.id,"active","source_reply","blocked",a.reason)
    elif a.cmd=="request-changes": transition(c,a.id,"active","done","request-changes",a.reason)
    elif a.cmd=="stop": transition(c,a.id,"stopped","none","stop")
    elif a.cmd=="resume": transition(c,a.id,"active",row["action"],"resume")
    elif a.cmd=="cancel": transition(c,a.id,"cancelled","none","cancel")
    elif a.cmd=="delete":
        if row["result_file"]:
            try: Path(row["result_file"]).unlink()
            except FileNotFoundError: pass
        c.execute("delete from tasks where id=?", (a.id,)); c.commit()
    elif a.cmd=="done-implicit":
        p=Path(a.result_file).expanduser()
        if not p.is_file(): raise SystemExit("result file is not readable")
        dest=ROOT/"results"/(a.id+".md"); dest.parent.mkdir(exist_ok=True); shutil.copyfile(p,dest)
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
    ROOT.mkdir(parents=True,exist_ok=True); (ROOT/"daemon.pid").write_text(str(os.getpid())); print("daemon started")
    try:
        while not (ROOT/"daemon.stop").exists():
            c=conn()
            for r in c.execute("select * from tasks where state not in ('finished','rejected','cancelled','stopped','timeout')").fetchall():
                ag=r["target_agent"] if r["action"] in ("take","done") else r["source_agent"]
                info=agent_get(ag); lifecycle="unknown"; present="absent" if info is None else "present"
                if info:
                    lifecycle=info.get("result",info).get("status",info.get("status","unknown")) if isinstance(info,dict) else "unknown"
                c.execute(f"update tasks set {'target' if ag==r['target_agent'] else 'source'}_lifecycle=?, {'target' if ag==r['target_agent'] else 'source'}_presence=? where id=?",(lifecycle,present,r['id'])); c.commit()
                if present=="absent": transition(c,r["id"],"target_absent" if ag==r["target_agent"] else "source_absent","none",error="Herdr Agent absent"); continue
                if lifecycle=="working":
                    # Herdr owns the wait; this time is outside task backoff.
                    herdr("agent","wait",ag,"--until","idle")
                if time.time() >= datetime.fromisoformat((r["next_prompt_at"] or now())).timestamp():
                    prompt(ag,f"[HANDOFF REMINDER]\nTask ID: {r['id']}\nDescription: {r['description']}\nRequired command: python3 {CLI} {r['action']} {r['id']}")
                    c.execute("update tasks set last_prompt_at=?,next_prompt_at=?,retry_count=retry_count+1 where id=?",(now(),datetime.fromtimestamp(time.time()+30,timezone.utc).isoformat(),r['id'])); c.commit()
            time.sleep(5)
    finally:
        for p in (ROOT/"daemon.pid",ROOT/"daemon.stop"):
            try:p.unlink()
            except:pass
# ---------- board rendering ----------

CLOSED_STATES = ("finished","rejected","cancelled","stopped","timeout",
                 "target_absent","source_absent")
STATE_LABEL = {"published":"published","active":"active","result_ready":"result ready",
               "reviewing":"reviewing","finished":"finished","rejected":"rejected",
               "cancelled":"cancelled","stopped":"stopped","timeout":"timeout",
               "target_absent":"target absent","source_absent":"source absent"}
STATE_STYLE = {"published":("cyan",),"active":("cyan",),"result_ready":("boldblue",),
               "reviewing":("boldyellow",),"finished":("boldgreen",),"rejected":("boldred",),
               "cancelled":("dim",),"stopped":("dim",),"timeout":("boldred",),
               "target_absent":("boldred",),"source_absent":("boldred",)}
ACTION_LABEL = {"take":"take","done":"done","claim":"claim","accept":"accept",
                "source_reply":"reply","none":"—"}
_CODES = {"dim":"2","bold":"1","red":"31","green":"32","yellow":"33","blue":"34","cyan":"36",
          "boldred":"1;31","boldgreen":"1;32","boldyellow":"1;33","boldblue":"1;34"}
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

def render_board(width=100):
    me = os.environ.get("HERDR_PANE_ID")
    items = []
    for r in conn().execute("select * from tasks order by state_since"):
        action = r["action"]; to_target = action in ("take","done")
        actor = r["target_agent"] if to_target else r["source_agent"]
        actor_pane = r["target_pane"] if to_target else r["source_pane"]
        items.append({
            "id": r["id"], "desc": r["description"].replace("\n"," / "),
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

    def next_cell(i):
        if i["action"] == "none": return "—", ("dim",)
        label = ACTION_LABEL.get(i["action"], i["action"])
        if i["mine"]: return "▶ %s" % label, ("boldyellow",)
        return "%s · %s" % (label, i["actor"]), ("dim",)

    kid = max([_dw("ID")] + [_dw(i["id"]) for i in items])
    kst = max([_dw("STATE")] + [_dw(STATE_LABEL.get(i["state"], i["state"])) for i in items])
    kag = max([_dw("AGE")] + [_dw(i["age"]) for i in items])
    krt = max([_dw("ROUTE")] + [_dw(i["route"]) for i in items])
    knx = max([_dw("NEXT")] + [_dw(next_cell(i)[0]) for i in items])

    show_route = show_next = True
    MIN_DESC = 18
    def fixed():
        w = 1 + kid + kst + kag + 2*4
        if show_route: w += krt + 2
        if show_next: w += knx + 2
        return w
    while width - fixed() < MIN_DESC:
        if show_route: show_route = False
        elif show_next: show_next = False
        else: break
    desc_w = max(6, width - fixed())

    def row(cells):
        parts = []
        for idx, (text, styles, w, align) in enumerate(cells):
            parts.append(_paint(_pad(text, w, align == "r") if idx < len(cells)-1 else text, *styles))
        return "  ".join(parts).rstrip()

    def build(mark, _id, desc, route, state, nxt, age, ms, ds, ss, ns, as_):
        cells = [(mark, ms, 1, "l"), (_id, ds, kid, "l"), (desc, ds, desc_w, "l")]
        if show_route: cells.append((route, ds, krt, "l"))
        cells.append((state, ss, kst, "l"))
        if show_next: cells.append((nxt, ns, knx, "l"))
        cells.append((age, as_, kag, "r"))
        return row(cells)

    n = len(items)
    awaiting = sum(1 for i in items if i["mine"] and not i["closed"])
    left_plain = "Handoff · %d task%s" % (n, "" if n == 1 else "s")
    left = _paint("Handoff", "bold") + _paint(" · %d task%s" % (n, "" if n == 1 else "s"), "dim")
    if awaiting:
        left_plain += " · %d awaiting you" % awaiting
        left += _paint(" · %d awaiting you" % awaiting, "boldyellow")
    clock = datetime.now().strftime("%H:%M:%S")
    lines = [left + " " * max(1, width - _dw(left_plain) - len(clock)) + _paint(clock, "dim"),
             _paint("─" * width, "dim")]

    lines.append(build("", "ID", "DESCRIPTION", "ROUTE", "STATE", "NEXT", "AGE",
                       ("dim",), ("dim",), ("dim",), ("dim",), ("dim",)))
    if not items:
        lines.append(_paint("  no tasks yet — handoff send ... to create one", ("dim",)))
    for i in items:
        ds = ("dim",) if i["closed"] else ()
        nxt, ns = next_cell(i)
        lines.append(build("▸" if i["mine"] else " ", i["id"], _fit(i["desc"], desc_w),
                           i["route"], STATE_LABEL.get(i["state"], i["state"]), nxt, i["age"],
                           ("boldyellow",) if i["mine"] else ds,
                           ds, STATE_STYLE.get(i["state"], ()), ns, ds))
    lines.append("")
    legend = ("▸ your turn   ·   " if awaiting else "") + "refresh 1s   ·   Ctrl-C to quit"
    lines.append(_paint(legend, ("dim",)))
    return "\n".join(lines)

def ui(_):
    if not sys.stdout.isatty():
        print(render_board(shutil.get_terminal_size((110, 30)).columns)); return
    _use_color()
    try:
        sys.stdout.write("\033[?25l\033[H\033[2J")
        while True:
            frame = render_board(shutil.get_terminal_size((110, 30)).columns)
            sys.stdout.write("\033[H" + frame + "\033[J")
            sys.stdout.flush()
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\033[?25h\n")
        sys.stdout.flush()
def main():
    p=argparse.ArgumentParser(); sp=p.add_subparsers(dest="op",required=True)
    s=sp.add_parser("send"); s.add_argument("--source-agent",required=True); s.add_argument("--source-pane",required=True); s.add_argument("--target-agent",required=True); s.add_argument("--target-pane",required=True); s.add_argument("--description",required=True); s.add_argument("--prompt",required=True); s.set_defaults(fn=cmd_send)
    for n in ("take","progress","claim","accept","reject","blocked","request-changes","stop","resume","cancel","delete","done-implicit"):
        x=sp.add_parser(n); x.add_argument("id"); x.add_argument("--reason", "--description", dest="reason", default=""); x.add_argument("--result-file"); x.set_defaults(fn=cmd_action,cmd=n)
    d=sp.add_parser("done"); d.add_argument("id"); d.add_argument("--result-file",required=True); d.add_argument("--implicit-take",action="store_true"); d.set_defaults(fn=cmd_action,cmd="done")
    l=sp.add_parser("list"); l.set_defaults(fn=cmd_list)
    d=sp.add_parser("daemon"); d.add_argument("op",choices=("start","stop","status")); d.set_defaults(fn=daemon)
    u=sp.add_parser("ui"); u.set_defaults(fn=ui)
    a=p.parse_args(); a.fn(a)
if __name__=="__main__": main()
