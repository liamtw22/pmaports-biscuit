#!/usr/bin/env python3
"""Import compatible stock Voice tuning without loading vendor executable code."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re

STOCK_FILE = '/opt/persist/call-stock.json'
PROFILE_LABELS = ['Open-source defaults', 'Stock-derived tuning']
OPEN = dict(tail=3840, target_db=-20.0, max_gain_db=30, rise_db=3, fall_db=6,
            stock_hpf=False)

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


def bounded(value, low, high, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('stock tuning must contain numeric values')
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError('stock tuning is outside the supported range')
    if integer and int(value) != value:
        raise ValueError('stock tuning requires an integer')
    return int(value) if integer else float(value)

def validate(data):
    if not isinstance(data, dict):
        raise ValueError('call profile must be an object')
    if data.get('schema') != 1 or data.get('hardware') != 'Biscuit':
        raise ValueError('not a supported Biscuit call profile')
    p = data['parameters']
    result = dict(tail=bounded(p['tail'],512,8192,True),
                  target_db=bounded(p['target_db'],-40,-10),
                  max_gain_db=bounded(p['max_gain_db'],0,40,True),
                  rise_db=bounded(p['rise_db'],1,12,True),
                  fall_db=bounded(p['fall_db'],1,24,True), stock_hpf=True)
    if result['tail'] % 128:
        raise ValueError('AEC tail must be a multiple of 128 samples')
    return result

def import_stock(text, destination=STOCK_FILE):
    if not isinstance(text,str) or len(text.encode('utf-8')) > 60000:
        raise ValueError('AFE.cfg must be at most 60000 bytes')
    # Preserve quoted strings while removing C/JSON comments.
    clean = re.sub(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/',
                   lambda m: m.group(0) if m.group(0).startswith('"') else '', text)
    try:
        cfg=json.loads(clean)
        hw=cfg['Hardware Definition']
        if hw['Name']!='Biscuit' or hw['Num Mics']!=7 or hw['Mics SamplingRate']!=16000:
            raise ValueError('AFE.cfg is not a seven-mic, 16 kHz Biscuit profile')
        alg=cfg['Algorithm Definition']; voice=cfg['Path Definition']['Voice']['Algorithms']
        if voice['AGC']!='FB Automatic Gain Control' or voice['HPF']!='HPF 80Hz @ 16K':
            raise ValueError('unsupported stock Voice algorithm selection')
        expected_hpf=[-1.978964742877471,-1.920823752121581,-1.995031240858690,0,
                      .980148787818626,.923195725460049,.995935784603137,0,
                      2.061764283274676,12.481101519210988,.036465860122281,1,
                      -4.12286229274,-24.9615622297,-.07291290428,0,
                      2.061764283274676,12.481101519210988,.036465860122281,0]
        actual_hpf=alg[voice['HPF']]['Coefficients']
        if len(actual_hpf)!=20 or any(abs(bounded(a,-30,30)-b)>1e-8 for a,b in zip(actual_hpf,expected_hpf)):
            raise ValueError('unsupported stock HPF coefficients')
        aec=alg[voice['AEC']]; agc=alg[voice['AGC']]; fb=alg[voice['FilterBank']]
        if agc['sampleRate']!=16000 or agc['channelCount']!=1 or fb['Decimation Rate']!=128:
            raise ValueError('unsupported stock call rate/channel/frame configuration')
        if agc.get('bypass') is not False or aec['Num Refs Per Input']!=1:
            raise ValueError('stock call profile must enable single-reference processing')
        # Raw input stays at ADC level to preserve AEC headroom. Constant gain
        # contributes to the available AGC gain budget, not a pre-AEC boost.
        maximum=bounded(agc['constantGaindB'],0,30)+bounded(agc['maxGaindB'],0,20)
        params=dict(tail=aec['TailLen'], target_db=agc['targetLeveldB'],
                    max_gain_db=maximum, rise_db=agc['maxGainIncRate']/100,
                    fall_db=agc['maxGainDecRate']/100, stock_hpf=True)
        data=dict(schema=1, hardware='Biscuit', parameters=params,
                  source_sha256=hashlib.sha256(text.encode()).hexdigest(),
                  backend='SpeexDSP',
                  applied=['AEC tail','AGC target','AGC gain budget','AGC slew limits','stock 80 Hz HPF'],
                  not_reproduced=['Amazon AEC/VSS internals','stock analysis/synthesis filterbank',
                                  'volume-indexed Frequency Masking RES','stock NR/AGC internals',
                                  'AGC minimum/constant gain trajectory','VoIP transmit EQ/ramp',
                                  'VoIP receive processing'])
        validate(data)
    except (KeyError, TypeError, ZeroDivisionError, json.JSONDecodeError) as exc:
        raise ValueError('invalid or unsupported AFE.cfg') from exc
    path=Path(destination); path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.tmp')
    with tmp.open('w') as f:
        json.dump(data,f,indent=2); f.write('\n'); f.flush(); os.fsync(f.fileno())
    _durable_replace(tmp,path)
    return data

def stock_info(path=STOCK_FILE):
    try:
        data=json.loads(Path(path).read_text())
        validate(data)
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return None

# Where the installer leaves this Echo's own Fire OS 6 files, from its backup.
BACKUP_AFE = '/usr/share/biscuit/fireos6/AFE.cfg'

def ensure_stock(path=STOCK_FILE, source=BACKUP_AFE):
    """The imported stock tuning, importing it from the backup first if needed.

    Import used to be a file upload on the settings page, so "Stock-derived
    tuning" was never offered until someone found their own AFE.cfg - although
    the installer had already put it on the device. Returns None when there is
    neither an import nor a usable backup file.
    """
    info = stock_info(path)
    if info is not None:
        return info
    try:
        with open(source, encoding='utf-8', errors='replace') as f:
            text = f.read()
    except OSError:
        return None
    try:
        info = import_stock(text, destination=path)
    except ValueError:
        return None
    info['from_backup'] = True
    return info

def resolve(profile, path=STOCK_FILE):
    if profile=='open':
        return dict(OPEN)
    if profile!='stock':
        raise ValueError('unknown call profile')
    data=stock_info(path)
    if data is None:
        raise ValueError('stock call tuning has not been imported or is invalid')
    return validate(data)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--import-stock',required=True,help='Path to extracted stock AFE.cfg')
    parser.add_argument('--output',default=STOCK_FILE)
    args=parser.parse_args()
    try:
        print(json.dumps(import_stock(Path(args.import_stock).read_text(),args.output),indent=2))
    except (OSError,ValueError) as exc:
        parser.exit(1,str(exc)+'\n')
