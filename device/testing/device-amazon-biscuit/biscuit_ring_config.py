"""Shared persistent brightness and HA accent palette; no optional services."""
import json
import os
import tempfile
import time

AUTODIM_FILE = '/opt/persist/autodim'
BRIGHTNESS_FILE = '/opt/persist/ring-brightness'
LEGACY_BRIGHTNESS = '/run/biscuit-ring/brightness'
ALS_BRIGHTNESS = '/run/biscuit-als/brightness'
ALS_LUX = '/run/biscuit-als/lux'
PALETTE_FILE = '/opt/persist/ring-palette.json'
DIRECTION_FILE = '/run/biscuit-direction/state'
ALIGNMENT_FILE = '/opt/persist/ring-alignment.json'
# The music visualiser's settings. Plain key=value rather than JSON,
# because the reader is biscuit_sendspin_viz inside the sendspin venv,
# which imports nothing of ours - the FILE is the contract between them.
# It re-reads once a second, so a write here takes effect without
# restarting the player, which matters: restarting it drops Music
# Assistant's connection.
VIZ_FILE = '/opt/persist/viz.conf'
# name: (default, low, high). Everything is a fraction except gamma.
VIZ_SETTINGS = {
    'enabled': (1.0, 0.0, 1.0),
    'gamma': (1.6, 1.0, 3.0),
    'punch': (0.5, 0.0, 1.0),
    'motion': (0.5, 0.0, 1.0),
    'relative': (0.5, 0.0, 1.0),
    # Both edges hard is what was chosen on the hardware.
    'fade_top': (0.05, 0.0, 1.0),
    'fade_bottom': (0.05, 0.0, 1.0),
    'pump': (0.75, 0.0, 1.0),
    'balance': (0.0, 0.0, 1.0),
    'lummatch': (0.0, 0.0, 1.0),
    'fpeak': (0.0, 0.0, 1.0),
    'loudness': (0.0, 0.0, 1.0),
    'rawscale': (0.0, 0.0, 1.0),
    'reset': (1.0, 0.0, 1.0),
}
PALETTES = {'Pattern defaults': None, 'Custom accents': None}
LEGACY_PALETTES = {
    'Cool accents': [[0,255,255],[0,40,255]],
    'Warm accents': [[255,80,0],[255,220,120]],
    'Red and blue accents': [[255,0,0],[0,0,255]],
}


def _durable_replace(tmp, dest):
    """os.replace, then make the rename ITSELF durable.

    os.replace is atomic - a reader never sees a half-written file - but it is
    not durable. After it returns, the new directory entry can still be only in
    page cache, and this device is power-cycled rather than shut down: every log
    on it carries NUL runs from exactly that. fsync on the containing directory
    is what commits the rename.

    The caller is expected to have fsynced the file's own contents first; this
    closes the other half. Best effort on purpose - the rename has already
    happened and succeeded by this point, so failing to sync is not worth
    raising over, and a directory that cannot be opened read-only is not a
    situation this can improve.

    Only used for destinations that must survive a power cut. Writes under /run
    are tmpfs, discarded at every boot, and deliberately still use os.replace.
    """
    os.replace(tmp, dest)
    try:
        fd = os.open(os.path.dirname(os.fspath(dest)) or ".", os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _read(path, fallback=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except (OSError, ValueError):   # ValueError: not UTF-8
        return fallback

def _write(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + os.path.basename(path) + '.',
                               dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(value)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        _durable_replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def load_autodim():
    return _read(AUTODIM_FILE, '1') != '0'

def save_autodim(on):
    _write(AUTODIM_FILE, '1' if on else '0')

def load_brightness():
    for path in (BRIGHTNESS_FILE, LEGACY_BRIGHTNESS):
        try:
            value = int(_read(path))
            return max(0, min(255, value))
        except (ValueError, TypeError):
            pass
    return 255

def save_brightness(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255:
        raise ValueError('brightness must be an integer from 0 to 255')
    _write(BRIGHTNESS_FILE, str(value))

def effective_brightness():
    if load_autodim():
        try:
            # A dead ALS publisher must not leave the ring dim indefinitely.
            if time.time() - os.path.getmtime(ALS_BRIGHTNESS) <= 15:
                return max(0, min(255, int(_read(ALS_BRIGHTNESS))))
        except (OSError, ValueError, TypeError):
            pass
    return load_brightness()

def lux():
    try:
        if time.time() - os.path.getmtime(ALS_LUX) > 15:
            return None
        value = float(_read(ALS_LUX))
        return round(value, 1) if 0 <= value < float('inf') else None
    except (OSError, ValueError, TypeError):
        return None

def load_palette():
    default = {'preset': 'Pattern defaults', 'colours': [[0,255,255],[0,0,255]], 'enabled':[True,True], 'levels':[255,255]}
    try:
        value = json.loads(_read(PALETTE_FILE, '{}'))
        if value.get('preset') in LEGACY_PALETTES:
            value['colours'] = LEGACY_PALETTES[value['preset']]
            value['preset'] = 'Custom accents'
        if value.get('preset') not in PALETTES:
            return default
        colours = validate_colours(value.get('colours', default['colours']))
        return {'preset': value['preset'], 'colours': colours,
                'enabled': [bool(x) for x in value.get('enabled', [True, True])][:2],
                'levels': [max(0,min(255,int(x))) for x in value.get('levels',[255,255])][:2]}
    except (ValueError, TypeError, AttributeError):
        return default

def validate_colours(colours):
    if not isinstance(colours, list) or len(colours) != 2:
        raise ValueError('two accent colours are required')
    if any(not isinstance(c, list) or len(c) != 3 or any(
           isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 255 for v in c) for c in colours):
        raise ValueError('accent colours must be RGB triples')
    return colours

def save_palette(preset, colours=None):
    if preset not in PALETTES:
        raise ValueError('unknown accent palette')
    value = load_palette()
    value['preset'] = preset
    if colours is not None:
        value['colours'] = validate_colours(colours)
        value['enabled'] = [True,True]
        value['levels'] = [255,255]
    _write(PALETTE_FILE, json.dumps(value))

def accents():
    value = load_palette()
    if value['preset'] != 'Custom accents': return None
    return [[round(c*value['levels'][i]/255) if value['enabled'][i] else 0 for c in colour]
            for i,colour in enumerate(value['colours'])]

def save_accent(index, data):
    import math
    value=load_palette()
    if data.get('rgb_changed', True):
        rgb=[data.get(k) for k in ('red','green','blue')]
        if any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) or not 0<=x<=1 for x in rgb):
            raise ValueError('invalid RGB command')
        value['colours'][index]=[round(x*255) for x in rgb]
    if data.get('brightness_changed', False):
        level=data.get('brightness')
        if isinstance(level,bool) or not isinstance(level,(int,float)) or not math.isfinite(level) or not 0<=level<=1:
            raise ValueError('invalid accent level')
        value['levels'][index]=round(level*255)
    if data.get('state_changed', True) and 'state' in data:
        if not isinstance(data['state'],bool): raise ValueError('invalid accent state')
        value['enabled'][index]=data['state']
    value['preset']='Custom accents'
    _write(PALETTE_FILE,json.dumps(value))

def load_alignment():
    try:
        v=json.loads(_read(ALIGNMENT_FILE,'{}'))
        return {'offset': float(v.get('offset',0))%360, 'reverse':bool(v.get('reverse',False))}
    except (ValueError,TypeError,AttributeError): return {'offset':0,'reverse':False}

def save_alignment(value):
    import math
    if not isinstance(value,dict) or isinstance(value.get('offset'),bool) or not isinstance(value.get('offset'),(int,float)) or not math.isfinite(value['offset']) or not isinstance(value.get('reverse'),bool):
        raise ValueError('alignment requires a finite offset and reverse boolean')
    _write(ALIGNMENT_FILE,json.dumps({'offset':value['offset']%360,'reverse':value['reverse']}))

def direction():
    try:
        if _read('/run/biscuit-audio/muted','1')!='0': return {}
        v=json.loads(_read(DIRECTION_FILE,'{}'))
        age=time.monotonic()-float(v['monotonic'])
        if not 0<=age<1.5: return {}
        return {k: float(v[k])%360 if isinstance(v.get(k),(int,float)) and 0<=v[k]<360 else None for k in ('speaker','noise')}
    except (ValueError,TypeError,KeyError): return {}

def direction_segment(kind):
    angle=direction().get(kind)
    if angle is None: return None
    align=load_alignment()
    return ((-angle if align['reverse'] else angle)+align['offset'])/30 %12


def state():
    # `viz` belongs here rather than only in the settings page's own
    # richer state: GET /api/ring answers from THIS function, so a
    # panel built on the other one would have loaded every control at
    # zero without erroring.
    return {'autodim': load_autodim(), 'brightness': load_brightness(),
            'effective_brightness': effective_brightness(), 'lux': lux(),
            'palette': load_palette(), 'palettes': list(PALETTES),
            'direction':direction(), 'alignment':load_alignment(),
            'viz': load_viz()}


def load_viz():
    """The visualiser settings, defaults filled in."""
    values = {k: v[0] for k, v in VIZ_SETTINGS.items()}
    for line in _read(VIZ_FILE, '').splitlines():
        key, _, raw = line.split('#', 1)[0].partition('=')
        key = key.strip()
        if key in values:
            try:
                lo, hi = VIZ_SETTINGS[key][1], VIZ_SETTINGS[key][2]
                values[key] = max(lo, min(hi, float(raw)))
            except ValueError:
                pass
    return values


def save_viz(changes):
    """Merge changes into the visualiser settings and write them out."""
    values = load_viz()
    for key, value in (changes or {}).items():
        if key not in VIZ_SETTINGS:
            raise ValueError('unknown visualiser setting %r' % (key,))
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError('%s must be a number' % (key,))
        if number != number or number in (float('inf'), float('-inf')):
            raise ValueError('%s must be finite' % (key,))
        lo, hi = VIZ_SETTINGS[key][1], VIZ_SETTINGS[key][2]
        values[key] = max(lo, min(hi, number))
    body = ['# Written by the settings page and Home Assistant.']
    body += ['%s=%g' % (k, values[k]) for k in sorted(values)]
    _write(VIZ_FILE, chr(10).join(body) + chr(10))
    return values
