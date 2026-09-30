#!/usr/bin/env python3
"""Apply bounded audio scheduling to each newly opened Biscuit capture owner."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time

FAIR = Path('/sys/kernel/debug/sched/fair_server')
POLL = Path('/sys/module/dough_fpga/parameters/capture_poll_us')
PCM = Path('/proc/asound/card0/pcm2c/sub0/status')
FAIR_TARGET = {'runtime': 500000, 'period': 10000000}  # Same 5% reserve.

def fair_read(cpu):
    return {k:int((FAIR/cpu/k).read_text()) for k in ('runtime','period')}

def fair_write(cpu, values):
    if not 0 < values['runtime'] <= values['period']:
        raise ValueError('invalid nonzero fair-server reserve')
    old=fair_read(cpu)
    order=('runtime','period') if values['period']<old['period'] else ('period','runtime')
    for key in order:
        if old[key] != values[key]: (FAIR/cpu/key).write_text(str(values[key])+'\n')
    if fair_read(cpu)!=values: raise RuntimeError('fair-server readback mismatch')

def task_state(pid):
    p=Path('/proc',str(pid)); fields=(p/'stat').read_text().rsplit(')',1)[1].split()
    mask=next(l.split(':',1)[1].strip() for l in (p/'status').read_text().splitlines() if l.startswith('Cpus_allowed_list:'))
    return {'pid':pid,'start_ticks':int(fields[19]),'priority':int(fields[37]),
            'policy':int(fields[38]),'cpus':mask}

def find_tasks(irq):
    result={}
    for p in Path('/proc').iterdir():
        if not p.name.isdigit(): continue
        try: comm=(p/'comm').read_text().strip()
        except OSError: continue
        role='reader' if comm=='dough-capture' else 'irq' if comm.startswith(f'irq/{irq}-') and '1100a000' in comm else None
        if role:
            if role in result: raise RuntimeError('multiple '+role+' tasks')
            result[role]=int(p.name)
    return result

def check_identity(item):
    if task_state(item['pid'])['start_ticks']!=item['start_ticks']:
        raise RuntimeError('capture task identity changed')

def command(*args):
    subprocess.run(args,check=True,stdout=subprocess.DEVNULL,timeout=3)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture-pid',type=int,required=True)
    args=parser.parse_args()
    if os.geteuid()!=0: raise RuntimeError('audio policy requires root')
    compatible=Path('/sys/firmware/devicetree/base/compatible').read_bytes().split(b'\0')
    if b'amazon,biscuit' not in compatible: raise RuntimeError('unsupported board')
    if Path('/sys/devices/system/cpu/online').read_text().strip()!='0-3':
        raise RuntimeError('audio policy requires the four Biscuit CPUs online')
    argv=Path('/proc',str(args.capture_pid),'cmdline').read_bytes().split(b'\0')
    if not argv or Path(os.fsdecode(argv[0])).name!='arecord' or b'hw:0,2' not in argv:
        raise RuntimeError('unexpected capture owner')
    matches=re.findall(r'^\s*(\d+):[^\n]*\b1100a000\.spi\s*$',Path('/proc/interrupts').read_text(),re.M)
    if len(matches)!=1: raise RuntimeError('cannot identify FPGA SPI interrupt')
    irq=int(matches[0]); irq_path=Path('/proc/irq',str(irq))
    deadline=time.monotonic()+3
    while True:
        tasks=find_tasks(irq)
        try: owner=re.search(r'(?m)^owner_pid\s*:\s*(\d+)',PCM.read_text())
        except OSError: owner=None
        if set(tasks)=={'reader','irq'} and owner and int(owner[1])==args.capture_pid:break
        if time.monotonic()>deadline: raise RuntimeError('capture owner/reader did not become ready')
        time.sleep(.02)
    before={'fair':{f'cpu{i}':fair_read(f'cpu{i}') for i in range(4)},
            'poll_us':int(POLL.read_text()),
            'irq_mask':(irq_path/'smp_affinity_list').read_text().strip(),
            'tasks':{k:task_state(pid) for k,pid in tasks.items()}}
    try:
        for cpu in before['fair']:fair_write(cpu,FAIR_TARGET)
        if (irq_path/'smp_affinity_list').read_text().strip()!='3':
            (irq_path/'smp_affinity_list').write_text('3\n')
        for role,priority in (('irq',92),('reader',90)):
            item=before['tasks'][role];check_identity(item)
            if role=='reader' and item['cpus']!='3':
                command('/usr/bin/taskset','-pc','3',str(item['pid']))
            if item['policy']!=1 or item['priority']!=priority:
                command('/usr/bin/chrt','-f','-p',str(priority),str(item['pid']))
        if int(POLL.read_text())!=1000:POLL.write_text('1000\n')
        after={k:task_state(pid) for k,pid in tasks.items()}
        if (irq_path/'effective_affinity_list').read_text().strip()!='3':
            raise RuntimeError('SPI interrupt affinity not applied')
        for role,priority in (('irq',92),('reader',90)):
            check_identity(before['tasks'][role])
            if after[role]['policy']!=1 or after[role]['priority']!=priority:
                raise RuntimeError('audio priority readback mismatch')
        if after['reader']['cpus']!='3' or int(POLL.read_text())!=1000:
            raise RuntimeError('capture policy readback mismatch')
        report={'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                'capture_pid':args.capture_pid,'irq':irq,'tasks':after,'poll_us':1000,
                'fair':{cpu:fair_read(cpu) for cpu in before['fair']}}
        dest=Path('/run/biscuit-mic/audio-policy.json');dest.parent.mkdir(exist_ok=True)
        temp=dest.with_suffix('.tmp');temp.write_text(json.dumps(report)+'\n');temp.replace(dest)
        print('biscuit-audio-policy: capture scheduling verified',flush=True)
    except BaseException:
        # Return surviving original tasks and global controls to pre-call values.
        POLL.write_text(str(before['poll_us'])+'\n')
        (irq_path/'smp_affinity_list').write_text(before['irq_mask']+'\n')
        for role,item in before['tasks'].items():
            try:check_identity(item)
            except (OSError,RuntimeError):continue # Never touch a reused/dead PID.
            command('/usr/bin/chrt','-f' if item['policy']==1 else '-o','-p',str(item['priority']),str(item['pid']))
            if role=='reader':command('/usr/bin/taskset','-pc',item['cpus'],str(item['pid']))
        for cpu,values in before['fair'].items():fair_write(cpu,values)
        raise

if __name__=='__main__':main()
