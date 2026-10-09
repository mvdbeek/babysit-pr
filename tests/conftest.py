"""Fake gh/herdr/tmux/cmux/agent executables shared by the process integration tests.

Every fake records its argv in the JSON state file named by ``$FAKE_HERDR`` so tests
can assert on the exact commands issued. Nothing here touches a live herdr, tmux or
cmux, and no real agent is ever started.
"""

import json
import os
import sys
from pathlib import Path

import pytest

GH = """import json,os,sys,subprocess
from pathlib import Path
p=Path(os.environ['FAKE_HERDR']); data=json.loads(p.read_text()); a=sys.argv[1:]
data.setdefault('gh',[]).append(a); p.write_text(json.dumps(data))
if os.environ.get('FAKE_GH_FAIL'):
 print(os.environ['FAKE_GH_FAIL'],file=sys.stderr); sys.exit(1)
if a[:2] == ['repo','clone']:
 subprocess.check_call(['git','clone',os.environ['FAKE_HEAD'],a[3]])
 subprocess.check_call(['git','-C',a[3],'remote','set-url','origin','https://github.com/base/repo.git'])
elif a[:2] == ['issue','create']:
 print(os.environ.get('FAKE_GH_CREATED','https://github.com/base/repo/issues/12'))
elif 'issue' in a:
 print(os.environ.get('FAKE_GH_ISSUE','{"number": 12, "title": "Crash on start!"}'))
else:
 print(os.environ.get('FAKE_GH_PR','{"headRefName": "feature", "headRepository": {"name": "repo"}, "headRepositoryOwner": {"login": "fork"}}'))
"""

HERDR = """import json,os,sys,subprocess
from pathlib import Path
p=Path(os.environ['FAKE_HERDR']); data=json.loads(p.read_text()); a=sys.argv[1:]
data['calls'].append(a)
result={}
if a[:2] == ['status','server']: result={'running':os.environ.get('FAKE_HERDR_RUNNING','true')=='true'}
elif a[:2] == ['workspace','list']: result={'workspaces':data['workspaces']}
elif a[:2] == ['agent','list']: result={'agents':data['agents']}
elif a[:2] == ['worktree','open']:
 if os.environ.get('FAKE_HERDR_OPEN_ERROR'):
  p.write_text(json.dumps(data)); print(json.dumps({'error':{'message':os.environ['FAKE_HERDR_OPEN_ERROR']}})); sys.exit(1)
 path=a[a.index('--path')+1]; root=a[a.index('--cwd')+1]
 existing=next((w for w in data['workspaces'] if w['worktree']['checkout_path']==path),None)
 if existing: wid=existing['workspace_id']
 else:
  n=len(data['workspaces'])+1
  while any(w['workspace_id']=='w'+str(n) for w in data['workspaces']): n+=1
  wid='w'+str(n)
  data['workspaces'].append({'workspace_id':wid,'label':a[a.index('--label')+1] if '--label' in a else 'Reopened','agent_status':'unknown','worktree':{'repo_root':root,'checkout_path':path}})
 result={'already_open':bool(existing),'root_pane':{'pane_id':wid+':p1'}}
 if os.environ.get('FAKE_HERDR_NO_ROOT'): result={'already_open':False}
elif a[:2] == ['workspace','close']:
 data['workspaces']=[w for w in data['workspaces'] if w['workspace_id']!=a[2]]
 data['agents']=[g for g in data['agents'] if g.get('workspace_id')!=a[2]]
 if os.environ.get('FAKE_HERDR_CLOSE_ERROR'):
  p.write_text(json.dumps(data)); print(json.dumps({'error':{'message':os.environ['FAKE_HERDR_CLOSE_ERROR']}})); sys.exit(1)
elif a[:2] == ['agent','prompt']:
 if os.environ.get('FAKE_HERDR_PROMPT_ERROR'):
  p.write_text(json.dumps(data)); print(json.dumps({'error':{'code':os.environ['FAKE_HERDR_PROMPT_ERROR'],'message':'refused'}})); sys.exit(1)
 data.setdefault('prompts',[]).append(a[2:4])
elif a[:2] == ['pane','send-text']:
 data.setdefault('typed_text',[]).append([a[2], ' '.join(a[3:])])
elif a[:2] == ['agent','send-keys']:
 if not os.environ.get('FAKE_HERDR_KEEP_AGENT'):
  data['agents']=[g for g in data['agents'] if g.get('pane_id')!=a[2]]
elif a[:2] == ['agent','read']:
 # A scripted dialog: each key or text sent to the pane advances to its next screen.
 sent=[c for c in data['calls'] if c[:2] in (['agent','send-keys'],['pane','send-text']) and c[2]==a[2]]
 screens=data.get('screens',{}).get(a[2]) or ['']
 p.write_text(json.dumps(data)); print(screens[min(len(sent),len(screens)-1)]); sys.exit(0)
elif a[:2] == ['pane','list']:
 wid=a[a.index('--workspace')+1] if '--workspace' in a else None
 result={'panes':[x for x in data.get('panes',[]) if wid is None or x['workspace_id']==wid]}
elif a[:2] == ['pane','get']:
 agent=next((g for g in data['agents'] if g.get('pane_id')==a[2]),{})
 result={'pane':dict(agent,terminal_id='terminal',scroll={'offset_from_bottom':0})}
elif a[:2] == ['agent','get']:
 result={'agent':{'state_change_seq':1}}
elif a[:2] == ['pane','process-info']:
 agent=next((g for g in data['agents'] if g.get('pane_id')==a[a.index('--pane')+1]),{})
 default={'shell_pid':1,'foreground_processes':[{'pid':200,'argv0':agent.get('agent','codex')}] if agent else []}
 result={'process_info':data.get('process_info',{}).get(a[a.index('--pane')+1],default)}
elif a[:2] == ['pane','split']:
 wid=a[2].split(':')[0]; pid=f"{wid}:p{len(data.setdefault('panes',[]))+10}"
 data['panes'].append({'pane_id':pid,'workspace_id':wid,'cwd':a[a.index('--cwd')+1]})
 data.setdefault('splits',[]).append(a[2:])
 result={'pane':{'pane_id':pid}}
elif a[:2] == ['pane','run']:
 w=next(w for w in data['workspaces'] if a[2].startswith(w['workspace_id']+':'))
 data.setdefault('runs',[]).append(a[2:])
 p.write_text(json.dumps(data))
 env={**os.environ,'FAKE_WORKSPACE':w['workspace_id'],'FAKE_PANE':a[2]}
 subprocess.check_call(['/bin/zsh','-fc',a[3]],cwd=w['worktree']['checkout_path'],env=env)
 sys.exit(0)
p.write_text(json.dumps(data)); print(json.dumps({'result':result}))
"""

AGENT = """import json,os,sys
from pathlib import Path
if sys.argv[1:] == ['--version']:
    print(os.environ.get('FAKE_AGENT_VERSION', 'fixture-cli 0.0.1')); sys.exit()
p=Path(os.environ['FAKE_HERDR']); data=json.loads(p.read_text())
agent={'workspace_id':os.environ['FAKE_WORKSPACE'],'agent':Path(sys.argv[0]).name,'cwd':os.getcwd(),'task':sys.argv[-1],'argv':sys.argv[1:]}
if os.environ.get('FAKE_PANE'): agent.update(pane_id=os.environ['FAKE_PANE'],agent_status='working')
if os.environ.get('SAFE_ENABLE'): agent['safe_enable']=os.environ['SAFE_ENABLE']
if os.environ.get('CLAUDE_CONFIG_DIR'): agent['claude_config_dir']=os.environ['CLAUDE_CONFIG_DIR']
for flag in ('resume','--resume'):
 if flag in sys.argv[1:]: agent['agent_session']={'value':sys.argv[sys.argv.index(flag)+1]}
data['agents'].append(agent)
p.write_text(json.dumps(data))
"""

TMUX = """import json,os,sys
from pathlib import Path
p=Path(os.environ['FAKE_HERDR']); data=json.loads(p.read_text()); a=sys.argv[1:]
data.setdefault('tmux',[]).append(a)
sessions=data.setdefault('tmux_sessions',[])
code=0
if a[0] == 'has-session': code=0 if a[2].lstrip('=') in sessions else 1
elif a[0] == 'new-session': sessions.append(a[a.index('-s')+1])
p.write_text(json.dumps(data)); sys.exit(code)
"""

CMUX = """import json,os,sys
from pathlib import Path
p=Path(os.environ['FAKE_HERDR']); data=json.loads(p.read_text()); a=sys.argv[1:]
data.setdefault('cmux',[]).append(a)
workspaces=data.setdefault('cmux_workspaces',[]); groups=data.setdefault('cmux_groups',[])
if a[:1] == ['list-workspaces']: print(json.dumps({'workspaces':workspaces}))
elif a[:2] == ['workspace-group','list']: print(json.dumps({'groups':groups}))
elif a[:2] == ['workspace-group','create']:
 group={'name':a[a.index('--name')+1],'ref':'g'+str(len(groups)+1)}; groups.append(group)
 print(json.dumps({'group':group}))
elif a[:1] == ['new-workspace']:
 workspaces.append({'ref':'ws'+str(len(workspaces)+1),'name':a[a.index('--name')+1],'current_directory':a[a.index('--cwd')+1]})
p.write_text(json.dumps(data))
"""

FAKES = {"gh": GH, "herdr": HERDR, "tmux": TMUX, "cmux": CMUX, "codex": AGENT, "claude": AGENT}


def install_fakes(bin_dir: Path) -> None:
    # -S: the fakes need only the stdlib, and skipping site keeps coverage's subprocess
    # hook from starting (and slowing) every fake call; scripts they launch still measure.
    for name, body in FAKES.items():
        path = bin_dir / name
        path.write_text(f"#!{sys.executable} -S\n" + body)
        path.chmod(0o700)


@pytest.fixture(scope="session", autouse=True)
def delete_output_dir():
    """Overrides pytest-playwright's per-session wipe of the screenshot directory.

    Under xdist every worker is a session, so a late-starting worker would delete
    an earlier one's failure screenshots; tox clears the directory once instead.
    """


@pytest.fixture(autouse=True)
def github_token_from_env(monkeypatch):
    """No test opens the system keyring: gh gets its token from the environment."""
    monkeypatch.setenv("GH_TOKEN", "gho_fixture_token_0000000000000000000000")


@pytest.fixture
def fake_tools(tmp_path, monkeypatch):
    """Fake executables first on PATH plus the JSON state file recording their calls."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_fakes(bin_dir)
    state = tmp_path / "herdr.json"
    state.write_text(json.dumps({"workspaces": [], "agents": [], "calls": []}))
    monkeypatch.setenv("FAKE_HERDR", str(state))
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return state


@pytest.fixture(autouse=True)
def fresh_viewer_caches(monkeypatch):
    """Viewer caches are per process; each test starts with none."""
    import workspace_viewer

    for name in ("_checkouts", "_details", "_parsers"):
        monkeypatch.setattr(workspace_viewer, name, {})
    import agent_docker
    import agent_messages

    # Never query the host's Seatbelt policies for the fake agent PIDs.
    monkeypatch.setattr(agent_docker, "has_access", lambda pid: True)
    monkeypatch.setattr(agent_messages, "_recent", {})
