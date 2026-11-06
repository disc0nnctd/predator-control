"""Control Centre adapter for the installed SysBear handlers. No Discord delivery."""
import collections
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import threading
import time

SOURCE = Path('/home/notdc/projects/miniclaw/plugins/systembear-control')
FAN_SOURCE = Path('/home/notdc/.hermes/plugins/fan-control-command/__init__.py')

# Each tuple is (action, label, changes state). Metadata also drives the page.
def group(domain, host, title, actions, target='', note='', wait=False, duration=False):
    return dict(domain=domain, host=host, title=title, actions=[dict(action=a, label=l, mutates=m) for a,l,m in actions],
                target=target, note=note, wait=wait, duration=duration)

GROUPS = [
 group('health','dcmini','System health',[('summary','Summary',False),('events','Hardware faults',False)]),
 group('disk','dcmini','Disk space',[('status','Filesystem usage',False),('top','Largest home folders',False),('models','Model storage',False),('projects','Project storage',False),('cache','Cache storage',False),('cleanup','Cleanup review',False)],note='Cleanup review lists candidates; it does not delete files.'),
 group('fan','dcmini','Fan',[('status','Status and temperature',False),('auto','Automatic',True),('on','Always on',True),('off','Off',True)],note='The existing fan controller retains its temperature protection.'),
 group('llm','dcmini','Local language model',[('status','Status',False),('start','Start',True),('stop','Stop',True),('restart','Restart',True)],target='primary',duration=True,note='Starts expire automatically. Stopping interrupts active requests.'),
 group('media','dcmini','Media services',[('status','Health',False),('recover','Recover',True),('pause','Pause recovery',True),('resume','Resume recovery',True),('logs','Recovery history',False)],note='Recovery can change DNS and restart unhealthy media services.'),
 group('tradingview','dcmini','TradingView',[('status','Status',False),('on','Start',True),('off','Stop',True)],note='Starts the desktop, connection and recovery timer for this session. Automatic startup stays disabled.'),
 group('bluetooth','dcmini','Bluetooth',[('status','Controller status',False),('list','Devices',False),('scan','Scan',True),('connect','Connect',True),('disconnect','Disconnect',True),('pair','Pair',True),('trust','Trust',True),('untrust','Untrust',True),('remove','Forget device',True),('power','Radio power',True),('reset','Reset Bluetooth',True)],target='Exact device name or address; on/off for radio power',wait=True,note='Pairing needs the physical device in pairing mode. Forgetting removes its pairing.'),
 group('wifi','dcmini','Wi-Fi',[('status','Status',False),('list','Saved networks',False),('scan','Scan nearby',True),('switch','Switch saved network',True)],target='Saved network name',wait=True,note='Switching can disconnect this page. The bot handler restores the previous network if internet fails to recover.'),
 group('channels','dcmini','Router channels',[('status','N1 settings',False),('scan','Survey',True),('recommend','Recommend channels',True),('apply','Apply N1 channels',True)],target='auto or channel pair, e.g. 1/44',note='Applies to N1. Airtel is survey-only. Changing channels can interrupt Wi-Fi; N1 may need a restart.'),
 group('power','dcmini','Power profile',[('status','Status',False),('set','Set profile',True)],target='balanced, performance or power-saver'),
 group('tailnet','dcmini','Tailscale',[('status','Status',False),('peers','Devices',False),('netcheck','Connection diagnostics',False),('ping','Ping device',False),('exit-node-list','Available exit nodes',False),('exit-node-use','Use exit node',True),('exit-node-off','Disable exit node',True),('login','Sign in',True)],target='Device name / address; force to sign in again',wait=True,note='Exit-node changes roll back if internet fails. Signing in again can disconnect this page until you approve the login.'),
 group('predator','predator','Desktop and power',[('status','Full system status',False),('wake','Wake',True),('display-off','Display off',True),('display-on','Display on',True),('lock','Lock',True),('sleep','Sleep',True)],wait=True,note='Sleep interrupts active work and disconnects Predator. Wake requires its wake-on-LAN setup.'),
 group('image-model','predator','Image model',[('list','List models',False),('set','Switch image model',True)],target='Model name, e.g. uc-q4_k_m',note='Switching changes the model used by the image app and Discord. Wait for active image jobs to finish.'),
]
GROUP_MAP = {g['domain']: g for g in GROUPS}
_guard = threading.RLock()
_jobs = collections.OrderedDict()
_active = None
_control = None
_fan = None

class ControlError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def load_handlers():
    global _control, _fan
    with _guard:
        if _control is None:
            if not SOURCE.joinpath('control.py').is_file():
                raise ControlError('SysBear handlers are not installed on this host.', 503)
            spec = importlib.util.spec_from_file_location('_centre_sysbear', SOURCE / '__init__.py', submodule_search_locations=[str(SOURCE)])
            package = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = package
            spec.loader.exec_module(package)
            _control = sys.modules['_centre_sysbear.control']
        if _fan is None:
            spec = importlib.util.spec_from_file_location('_centre_fan', FAN_SOURCE)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _fan = module
        return _control, _fan


def capabilities():
    supported = os.name != 'nt' and SOURCE.joinpath('control.py').is_file() and FAN_SOURCE.is_file()
    return {'supported': supported, 'groups': GROUPS if supported else [],
            'message': '' if supported else 'SysBear integration is available on dcmini. Open Control Centre on dcmini for these controls.'}


def validate(body):
    if not isinstance(body, dict) or set(body) - {'domain','action','target','wait_seconds','duration_minutes','confirm'}:
        raise ControlError('Invalid control request.')
    domain, action = body.get('domain'), body.get('action')
    if not isinstance(domain, str) or not isinstance(action, str) or domain not in GROUP_MAP:
        raise ControlError('Unknown control group.')
    config = GROUP_MAP[domain]
    spec = next((a for a in config['actions'] if a['action'] == action), None)
    if spec is None:
        raise ControlError('Unknown action.')
    target = body.get('target', '')
    if not isinstance(target, str) or len(target) > 200 or any(ord(c) < 32 for c in target):
        raise ControlError('Invalid target.')
    target = target.strip()
    # Targets never enter a shell. Reject ignored fields to make intent unambiguous.
    if not config['target'] and target:
        raise ControlError('This control does not take a target.')
    if domain == 'image-model' and action == 'list' and target:
        raise ControlError('Leave the model name empty when listing models.')
    if domain == 'image-model' and action == 'set' and not target:
        raise ControlError('Choose an image model name first.')
    if domain == 'llm' and target not in ('', 'primary'):
        raise ControlError('Only the primary local model service is supported.')
    wait = body.get('wait_seconds', 90 if domain == 'predator' else 30)
    duration = body.get('duration_minutes', 60)
    if type(wait) is not int or not 5 <= wait <= 300:
        raise ControlError('Wait must be between 5 and 300 seconds.')
    if type(duration) is not int or not 5 <= duration <= 240:
        raise ControlError('Duration must be between 5 and 240 minutes.')
    if spec['mutates'] and body.get('confirm') is not True:
        raise ControlError('Confirm this action before applying it.')
    return dict(domain=domain, action=action, target=target, wait_seconds=wait, duration_minutes=duration), spec['mutates']


def execute(request):
    control, fan = load_handlers()
    domain, action, target = request['domain'], request['action'], request['target']
    if domain == 'fan':
        message = fan._handle_fan(action)
        return {'ok': not message.startswith(('Fan command failed', 'Usage:')), 'message': message}
    if domain == 'image-model':
        raw = control.predator_control('image-model', target=target)
    else:
        raw = control.dispatch(**request)
    result = json.loads(raw)
    if not isinstance(result, dict) or type(result.get('ok')) is not bool:
        raise RuntimeError('Invalid handler response.')
    return result


def submit(body, logger=None):
    global _active
    request, mutates = validate(body)
    if not capabilities()['supported']:
        raise ControlError(capabilities()['message'], 503)
    with _guard:
        if _active:
            raise ControlError('A system check or action is running. Wait for it to finish.', 409)
        ident = secrets.token_hex(12)
        job = dict(id=ident, domain=request['domain'], action=request['action'], running=True, started=time.time(), result=None)
        _jobs[ident] = job
        while len(_jobs) > 40:
            _jobs.popitem(last=False)
        _active = ident
        def run():
            global _active
            try:
                result = execute(request)
            except Exception as exc:
                result = {'ok': False, 'message': 'Control failed: ' + str(exc)[:300]}
            with _guard:
                job.update(running=False, finished=time.time(), result=result)
                _active = None
            if logger:
                # Never log targets, router secrets or authentication links.
                logger('system:' + request['domain'], request['action'], 'ok' if result['ok'] else 'failed')
        threading.Thread(target=run, daemon=True).start()
        return json.loads(json.dumps(job))


def jobs():
    with _guard:
        return {'jobs': json.loads(json.dumps(list(_jobs.values())[-20:]))}


def monitor_status():
    """Read the Discord monitor's existing state; never run it or send messages."""
    state = {}
    path = Path.home() / '.hermes/profiles/sysbear/systembear-monitor-state.json'
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            raise ValueError('Invalid monitor state')
        state = {k: raw.get(k) for k in ('connectivity','memory_alert','disk_alert','event_alerted_at')}
        state['updated'] = path.stat().st_mtime
    except (OSError, ValueError, TypeError):
        pass
    disk = shutil.disk_usage('/')
    temperatures = []
    for file in Path('/sys/class/thermal').glob('thermal_zone*/temp'):
        try:
            value = int(file.read_text()) / 1000
            if 0 <= value <= 150:
                temperatures.append(value)
        except (OSError, ValueError):
            pass
    return {'supported': capabilities()['supported'], 'sample_time': time.time(), 'monitor': state,
            'load': list(os.getloadavg()) if hasattr(os, 'getloadavg') else [],
            'root_used_pct': round(100 * disk.used / disk.total, 1),
            'temperature_c': max(temperatures) if temperatures else None}
