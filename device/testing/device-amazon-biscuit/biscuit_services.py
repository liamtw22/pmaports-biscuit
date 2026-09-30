"""Allowlisted app controls. Core audio/network services are deliberately read-only."""
import json
import os
from pathlib import Path
import subprocess
import threading
import time

PERSIST = Path('/opt/persist/services')
RUN = Path('/run/biscuit-services')
DATA_ROOT = Path('/opt/persist')
REGISTRY = {
    'voice': dict(label='Voice assistant', service='biscuit-voice-assistant',
                  package='device-amazon-biscuit-voice', settings='mic',
                  data='/opt/persist/voice-assistant-prefs.json',
                  reset='Resets wake-word choices and assistant preferences. Wi-Fi, pairing, microphone calibration and audio tuning are kept.'),
    # `settings` is a route of the settings page. 'audio' and 'leds' were not
    # routes, so these two links fell through to the home page.
    'sendspin': dict(label='Music speaker', service='biscuit-sendspin',
                     package='device-amazon-biscuit-sendspin', settings='sound',
                     data='/opt/persist/sendspin/server-url',
                     reset='Forgets the Music Assistant server it last connected to. It finds one on the network again.'),
    'direction': dict(label='Direction light', service='biscuit-direction',
                      settings='ring', description='Locates sound for the light ring. This is separate from wake-word detection.'),
}
LOCK = threading.Lock()
JOB = None

def run(argv, timeout=30):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise ValueError((result.stderr or result.stdout or 'Operation failed').strip()[-600:])
    return result.stdout

def allowed(app):
    if app not in REGISTRY: return False
    return not (PERSIST/(app+'.disabled')).exists() and not (RUN/(app+'.paused')).exists()

def direction_mode():
    try: mode=(PERSIST/'direction-mode').read_text().strip()
    except OSError: mode='always'
    return mode if mode in ('always','wake') else 'wake'

def put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(text); os.replace(tmp,path)

def running_services():
    # OpenRC runtime markers avoid sourcing every init script on each page load.
    return {p.name for p in Path('/run/openrc/started').glob('*')}

def snapshot():
    running=running_services()
    items=[]
    for app,entry in REGISTRY.items():
        installed=Path('/etc/init.d',entry['service']).is_file()
        items.append(dict(entry,id=app,installed=installed,
            running=entry['service'] in running,
            enabled=not (PERSIST/(app+'.disabled')).exists(),
            paused=(RUN/(app+'.paused')).exists(),
            clear_data=bool(entry.get('data')), mode=direction_mode() if app=='direction' else None))
    temperatures=[]
    for zone in Path('/sys/class/thermal').glob('thermal_zone*'):
        try: temperatures.append(dict(name=(zone/'type').read_text().strip(),celsius=round(int((zone/'temp').read_text())/1000,1)))
        except (OSError,ValueError): pass
    return dict(services=items,temperatures=temperatures,job=JOB)

def validate(body):
    if not isinstance(body,dict): raise ValueError('Expected an object.')
    app=body.get('id'); action=body.get('action')
    if not isinstance(app,str) or app not in REGISTRY: raise ValueError('This system service is protected.')
    if action not in ('enable','pause','resume','clear','mode','state'): raise ValueError('Unknown action.')
    if action=='state' and body.get('state') not in ('on','paused','off'): raise ValueError('Unknown app state.')
    if not Path('/etc/init.d',REGISTRY[app]['service']).is_file(): raise ValueError('Install this app first.')
    if action=='enable' and type(body.get('enabled')) is not bool: raise ValueError('Enabled must be true or false.')
    if action=='mode' and (app!='direction' or body.get('mode') not in ('always','wake')): raise ValueError('Unknown detection mode.')
    if action=='clear' and (not REGISTRY[app].get('data') or body.get('confirm') is not True): raise ValueError('Confirm the listed app data reset.')
    if action=='resume' and (PERSIST/(app+'.disabled')).exists(): raise ValueError('Enable this app before resuming it.')

def perform(body):
    app=body['id']; action=body['action']; entry=REGISTRY[app]; service=entry['service']
    paused=RUN/(app+'.paused'); disabled=PERSIST/(app+'.disabled')
    if action=='mode':
        put(PERSIST/'direction-mode',body['mode']+'\n')
        return 'Detection timing saved.'
    if action=='clear':
        # Single known regular file only; no recursive delete or caller-supplied paths.
        path=Path(entry['data'])
        if path.is_symlink() or DATA_ROOT.resolve() not in path.resolve().parents:
            raise ValueError('Unexpected data path; reset refused.')
        if path.exists() and not path.is_file(): raise ValueError('Unexpected data type; reset refused.')
        was_running=service in running_services()
        if was_running: run(['rc-service',service,'stop'])
        try: path.unlink(missing_ok=True)
        finally:
            if was_running and allowed(app): run(['rc-service',service,'start'])
        return 'App preferences cleared.'
    if action=='state':
        # The settings page's one control for an app: on, paused until the
        # next restart, or off. One action rather than enable-then-pause,
        # because start() holds a lock for the length of a job and a second
        # request straight after the first is refused.
        want=body['state']
        if want=='off':
            return perform(dict(body,action='enable',enabled=False))
        disabled.unlink(missing_ok=True)
        if not Path('/etc/runlevels/default',service).is_symlink():
            run(['rc-update','add',service,'default'])
        if want=='paused':
            put(paused,'paused\n')
            if service in running_services(): run(['rc-service',service,'stop'])
            return 'Paused until the next restart. Saved data is kept.'
        paused.unlink(missing_ok=True)
        if service not in running_services(): run(['rc-service',service,'start'])
        return 'On.'
    if action=='enable':
        if body['enabled']:
            disabled.unlink(missing_ok=True); paused.unlink(missing_ok=True)
            run(['rc-update','add',service,'default'])
            run(['rc-service',service,'start'])
            return 'Enabled now and at startup.'
        put(disabled,'disabled\n')
        if Path('/etc/runlevels/default',service).is_symlink():
            run(['rc-update','del',service,'default'])
        if service in running_services(): run(['rc-service',service,'stop'])
        return 'Disabled now and at startup.'
    if action=='pause':
        put(paused,'paused\n')
        if service in running_services(): run(['rc-service',service,'stop'])
        return 'Paused until resumed or rebooted. Saved data is kept.'
    paused.unlink(missing_ok=True)
    run(['rc-service',service,'start'])
    return 'Resumed.'

def start(body, package_job=None):
    global JOB
    validate(body)
    if not LOCK.acquire(blocking=False): raise ValueError('Another app operation is running.')
    if package_job and package_job.get('state')=='running':
        LOCK.release(); raise ValueError('Wait for the package operation to finish.')
    JOB=dict(state='running',id=body['id'],action=body['action'],message='Working…')
    def worker():
        global JOB
        try: JOB=dict(JOB,state='ok',message=perform(body))
        except Exception as error: JOB=dict(JOB,state='failed',message=str(error))
        finally: LOCK.release()
    threading.Thread(target=worker,daemon=True).start()
    return dict(ok=True,message='Operation started.')

# ---------------------------------------------------------------------------
# Any service, for the Apps and Processes pages
#
# The three app services above keep their on / paused / off state machine, so
# a stop here is a pause and the boot switch is their "off". Everything else
# is rc-service, with two corrections for how OpenRC behaves: stopping a
# service stops everything that needs it, and starting it again does not bring
# those back - so a stop records what it took down, and the matching start
# restores them. Restart needs no help; OpenRC restarts the dependents itself.
# ---------------------------------------------------------------------------
SERVICE_APPS = {entry['service']: app for app, entry in REGISTRY.items()}
STOPPED_WITH = RUN / 'stopped-with'

def _dependents(name, services):
    """Running services that need `name`, directly or through others."""
    seen, todo = set(), [name]
    while todo:
        cur = todo.pop()
        for other in services.get(cur, {}).get('needed_by', []):
            if other not in seen and services.get(other, {}).get('started'):
                seen.add(other); todo.append(other)
    return sorted(seen)

def service_perform(name, action, services):
    app = SERVICE_APPS.get(name)
    if app:
        state = {'start': 'on', 'stop': 'paused', 'boot_on': 'on', 'boot_off': 'off'}.get(action)
        if state:
            return perform(dict(id=app, action='state', state=state))
    if name == 'biscuit-settings' and action == 'restart':
        # This process answers the request; restarting it has to wait until
        # the answer is out.
        subprocess.Popen(['setsid', 'sh', '-c', 'sleep 1; rc-service biscuit-settings restart'],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return 'Restarting this page. It is back in a few seconds.'
    if action == 'restart':
        run(['rc-service', name, 'restart'], timeout=90)
        also = _dependents(name, services)
        return 'Restarted' + (', with ' + ', '.join(also) if also else '') + '.'
    record = STOPPED_WITH / name
    if action == 'stop':
        also = _dependents(name, services)
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(' '.join(also) + ' ')
        run(['rc-service', name, 'stop'], timeout=90)
        return ('Stopped' + ('. This also stopped ' + ', '.join(also) if also else '') +
                '. It starts again at the next restart.')
    if action == 'start':
        run(['rc-service', name, 'start'], timeout=90)
        back, failed = [], []
        try:
            wanted = [x for x in record.read_text().split() if x]
        except OSError:
            wanted = []
        for other in wanted:
            dep_app = SERVICE_APPS.get(other)
            if dep_app and not allowed(dep_app):
                continue
            try:
                run(['rc-service', other, 'start'], timeout=90); back.append(other)
            except (ValueError, subprocess.SubprocessError):
                failed.append(other)
        record.unlink(missing_ok=True)
        msg = 'Started'
        if back: msg += ', with ' + ', '.join(back)
        if failed: msg += '. Could not start ' + ', '.join(failed)
        return msg + '.'
    raise ValueError('Unknown action.')

def service_start(name, action, services, package_job=None):
    """Validate against the inventory's view of the service, then run the
    action on the same single-job lock as the app controls."""
    global JOB
    svc = services.get(name)
    if svc is None:
        raise ValueError('Unknown service.')
    if action not in ('start', 'stop', 'restart', 'boot_on', 'boot_off'):
        raise ValueError('Unknown action.')
    if svc['control'] == 'none':
        raise ValueError('%s is part of the system and is not controlled from here.' % name)
    if svc['control'] == 'restart' and action != 'restart':
        raise ValueError('Only restart is offered for %s: stopping it would lock you out.' % name)
    if action.startswith('boot_') and name not in SERVICE_APPS:
        raise ValueError('Only an app can be kept from starting at boot.')
    if not LOCK.acquire(blocking=False):
        raise ValueError('Another change is still running.')
    if package_job and package_job.get('state') == 'running':
        LOCK.release(); raise ValueError('Wait for the package operation to finish.')
    JOB = dict(state='running', id=name, action=action, message='Working…')
    def worker():
        global JOB
        try: JOB = dict(JOB, state='ok', message=service_perform(name, action, services))
        except Exception as error: JOB = dict(JOB, state='failed', message=str(error))
        finally: LOCK.release()
    threading.Thread(target=worker, daemon=True).start()
    return dict(ok=True, message='Working…')

if __name__=='__main__':
    import sys
    sys.exit(0 if len(sys.argv)==2 and allowed(sys.argv[1]) else 1)
