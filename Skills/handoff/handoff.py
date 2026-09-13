#!/usr/bin/env python3
"""Small, dependency-free Herdr handoff coordinator."""
import argparse, json, os, shutil, sqlite3, subprocess, sys, time, uuid
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
    elif a.cmd=="accept": transition(c,a.id,"accepted","none","accept")
    elif a.cmd=="reject": transition(c,a.id,"rejected","none","reject",a.reason)
    elif a.cmd=="blocked": transition(c,a.id,"active","source_reply","blocked",a.reason)
    elif a.cmd=="request-changes": transition(c,a.id,"active","done","request-changes",a.reason)
    elif a.cmd=="stop": transition(c,a.id,"stopped","none","stop")
    elif a.cmd=="resume": transition(c,a.id,"active",row["action"],"resume")
    elif a.cmd=="cancel": transition(c,a.id,"cancelled","none","cancel")
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
            for r in c.execute("select * from tasks where state not in ('accepted','rejected','cancelled','stopped','timeout')").fetchall():
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
def ui(_):
    while True:
        os.system("clear"); print("TASK\tDESCRIPTION\tSOURCE\tTARGET\tSTATE\tACTION\tAGE")
        cmd_list(None); time.sleep(2)
def main():
    p=argparse.ArgumentParser(); sp=p.add_subparsers(dest="op",required=True)
    s=sp.add_parser("send"); s.add_argument("--source-agent",required=True); s.add_argument("--source-pane",required=True); s.add_argument("--target-agent",required=True); s.add_argument("--target-pane",required=True); s.add_argument("--description",required=True); s.add_argument("--prompt",required=True); s.set_defaults(fn=cmd_send)
    for n in ("take","progress","claim","accept","reject","blocked","request-changes","stop","resume","cancel","done-implicit"):
        x=sp.add_parser(n); x.add_argument("id"); x.add_argument("--reason", "--description", dest="reason", default=""); x.add_argument("--result-file"); x.set_defaults(fn=cmd_action,cmd=n)
    d=sp.add_parser("done"); d.add_argument("id"); d.add_argument("--result-file",required=True); d.add_argument("--implicit-take",action="store_true"); d.set_defaults(fn=cmd_action,cmd="done")
    l=sp.add_parser("list"); l.set_defaults(fn=cmd_list)
    d=sp.add_parser("daemon"); d.add_argument("op",choices=("start","stop","status")); d.set_defaults(fn=daemon)
    u=sp.add_parser("ui"); u.set_defaults(fn=ui)
    a=p.parse_args(); a.fn(a)
if __name__=="__main__": main()
