"""Observed-empty hunt coverage and process-shared replenishment cooldown."""
import contextlib
import hashlib
import json
import math
import msvcrt
import time
import uuid
from pathlib import Path
from hunt_teams import atomic_json
from version_profile import configured_host

WAIT = 1800.
FRESH = 600.

@contextlib.contextmanager
def locked(path):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as stream:
        if stream.seek(0,2)==0:stream.write(b'0');stream.flush()
        deadline=time.monotonic()+2.
        while True:
            try:
                stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1);break
            except OSError:
                if time.monotonic()>=deadline:raise TimeoutError('bandit supply state lock busy')
                time.sleep(.02)
        try:yield
        finally:stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)

def state_path(base,host,port):
    if not configured_host(base,host):raise ValueError('supply host is not explicitly configured')
    key=hashlib.sha256(json.dumps([host,int(port)]).encode()).hexdigest()[:16]
    return Path(base)/'hunt_supply_states'/(key+'.json')

def load(path):
    if not path.exists():return dict(format=2,groups={},maps={},scene_next={},claims={})
    data=json.loads(path.read_text(encoding='utf8'))
    if (data.get('format')!=2 or not isinstance(data.get('groups'),dict)
            or not isinstance(data.get('maps'),dict)):raise ValueError('invalid supply state')
    return data

def observe(base,host,port,team,leader,scenes,scene,count,now):
    if (not scenes or scene not in scenes or type(count) is not int or count<0
            or not math.isfinite(now)):raise ValueError('invalid supply observation')
    path=state_path(base,host,port)
    key=hashlib.sha256(json.dumps([team,leader,sorted(scenes)]).encode()).hexdigest()
    with locked(path.with_suffix('.lock')):
        data=load(path)
        group=data['groups'].setdefault(key,dict(scenes=sorted(scenes),since=None,next_allowed=0.))
        def coverage(selected):
            return all(str(s) in data['maps'] and data['maps'][str(s)]['count']==0
                       and 0.<=now-data['maps'][str(s)]['at']<=FRESH for s in selected)
        for entry in data['groups'].values():
            if not coverage(entry['scenes']):entry['since']=None
        data['maps'][str(scene)]=dict(count=count,at=now)
        if count>0:
            for entry in data['groups'].values():
                if scene in entry['scenes']:entry['since']=None
        covered=coverage(scenes)
        if not covered:group['since']=None
        elif group['since'] is None or not 0.<=now-group['since']:group['since']=now
        token=None
        if (covered and now-group['since']>=WAIT and now>=group['next_allowed']
                and all(now>=data['scene_next'].get(str(s),0.) for s in scenes)):
            token=uuid.uuid4().hex
            # Persist reservation before requesting any server-side mutation.
            group['next_allowed']=now+WAIT
            for selected in scenes:data['scene_next'][str(selected)]=now+WAIT
            data['claims'][token]=dict(group=key,scene=scene,scenes=sorted(scenes))
        atomic_json(path,data,replace_attempts=6)
        return token

def complete(base,host,port,token,spawned,now,count=None):
    path=state_path(base,host,port)
    with locked(path.with_suffix('.lock')):
        data=load(path)
        claim=data['claims'].pop(token,None)
        if claim is None:return
        scene=claim['scene'];group=data['groups'][claim['group']]
        group['next_allowed']=now+(WAIT if spawned else 60.)
        for selected in claim['scenes']:
            data['scene_next'][str(selected)]=now+(WAIT if spawned else 60.)
        if spawned:group['since']=None
        if type(count) is int and count>0:
            data['maps'][str(scene)]=dict(count=count,at=now)
            for entry in data['groups'].values():
                if scene in entry['scenes']:entry['since']=None
        atomic_json(path,data,replace_attempts=6)
