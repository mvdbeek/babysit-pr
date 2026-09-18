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
  wid='w'+str(len(data['workspaces'])+1)
  data['workspaces'].append({'workspace_id':wid,'label':a[a.index('--label')+1] if '--label' in a else 'Reopened','agent_status':'unknown','worktree':{'repo_root':root,'checkout_path':path}})
 result={'already_open':bool(existing),'root_pane':{'pane_id':wid+':p1'}}
 if os.environ.get('FAKE_HERDR_NO_ROOT'): result={'already_open':False}
elif a[:2] == ['workspace','close']:
 data['workspaces']=[w for w in data['workspaces'] if w['workspace_id']!=a[2]]
 data['agents']=[g for g in data['agents'] if g.get('workspace_id')!=a[2]]
 if os.environ.get('FAKE_HERDR_CLOSE_ERROR'):
  p.write_text(json.dumps(data)); print(json.dumps({'error':{'message':os.environ['FAKE_HERDR_CLOSE_ERROR']}})); sys.exit(1)
elif a[:2] == ['agent','send-keys']:
 if not os.environ.get('FAKE_HERDR_KEEP_AGENT'):
  data['agents']=[g for g in data['agents'] if g.get('pane_id')!=a[2]]
elif a[:2] == ['pane','run']:
 w=next(w for w in data['workspaces'] if a[2].startswith(w['workspace_id']+':'))
 p.write_text(json.dumps(data))
 env={**os.environ,'FAKE_WORKSPACE':w['workspace_id']}
 subprocess.check_call(['/bin/zsh','-fc',a[3]],cwd=w['worktree']['checkout_path'],env=env)
 sys.exit(0)
p.write_text(json.dumps(data)); print(json.dumps({'result':result}))
"""

AGENT = """import json,os,sys
from pathlib import Path
p=Path(os.environ['FAKE_HERDR']); data=json.loads(p.read_text())
data['agents'].append({'workspace_id':os.environ['FAKE_WORKSPACE'],'agent':Path(sys.argv[0]).name,'cwd':os.getcwd(),'task':sys.argv[-1],'argv':sys.argv[1:]})
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
    for name, body in FAKES.items():
        path = bin_dir / name
        path.write_text(f"#!{sys.executable}\n" + body)
        path.chmod(0o700)


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
