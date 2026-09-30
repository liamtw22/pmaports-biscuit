#!/usr/bin/env python3
"""Keep snd-aloop on the unslewed audio clock while chrony adjusts system time.

The physical DAC/ADCs follow crystal clocks, while snd-aloop's jiffies timer
follows adjusted kernel time. Correct both loopback cables using ALSA's rate
control. This neither sets system time nor changes audio gain/DSP samples.
"""
from collections import deque
import ctypes
import json
import os
from pathlib import Path
import signal
import sys
import time

NOMINAL = 100000
RUN = Path('/run/biscuit-audio-clock')


class ClockRatio:
    """Short window catches clock slews before a small speaker buffer drains."""
    def __init__(self):
        self.samples = deque()

    def update(self, mono_ns, raw_ns, sampling_ns=0):
        if sampling_ns > 1_000_000:
            return None
        if self.samples and (raw_ns <= self.samples[-1][1] or
                             mono_ns <= self.samples[-1][0] or
                             raw_ns-self.samples[-1][1] > 1_000_000_000):
            self.samples.clear()
        self.samples.append((mono_ns, raw_ns))
        while len(self.samples)>2 and raw_ns-self.samples[0][1]>400_000_000:
            self.samples.popleft()
        first_m,first_r=self.samples[0]
        if raw_ns-first_r<200_000_000:
            return None
        ratio=(mono_ns-first_m)/(raw_ns-first_r)
        if not .9<=ratio<=1.1:
            self.samples.clear()
            raise ValueError(f'implausible adjusted/raw clock ratio: {ratio}')
        return round(NOMINAL*ratio)


class ALSARates:
    """One control handle; no subprocesses in the clock tracking loop."""
    def __init__(self):
        self.lib=ctypes.CDLL('libasound.so.2')
        ptr=ctypes.c_void_p;uint=ctypes.c_uint
        signatures={
            'snd_ctl_open':([ctypes.POINTER(ptr),ctypes.c_char_p,ctypes.c_int],ctypes.c_int),
            'snd_ctl_close':([ptr],ctypes.c_int),
            'snd_ctl_elem_value_malloc':([ctypes.POINTER(ptr)],ctypes.c_int),
            'snd_ctl_elem_value_free':([ptr],None),
            'snd_ctl_elem_value_set_interface':([ptr,ctypes.c_int],None),
            'snd_ctl_elem_value_set_name':([ptr,ctypes.c_char_p],None),
            'snd_ctl_elem_value_set_device':([ptr,uint],None),
            'snd_ctl_elem_value_set_subdevice':([ptr,uint],None),
            'snd_ctl_elem_value_set_index':([ptr,uint],None),
            'snd_ctl_elem_value_get_integer':([ptr,uint],ctypes.c_long),
            'snd_ctl_elem_value_set_integer':([ptr,uint,ctypes.c_long],None),
            'snd_ctl_elem_read':([ptr,ptr],ctypes.c_int),
            'snd_ctl_elem_write':([ptr,ptr],ctypes.c_int),
            'snd_strerror':([ctypes.c_int],ctypes.c_char_p),
        }
        for name,(args,ret) in signatures.items():
            fn=getattr(self.lib,name);fn.argtypes=args;fn.restype=ret
        self.handle=ptr();self.elements=[];self.original=None
        try:
            self.check(self.lib.snd_ctl_open(ctypes.byref(self.handle),b'hw:Loopback',0))
            for device in (0,1):
                value=ptr();self.check(self.lib.snd_ctl_elem_value_malloc(ctypes.byref(value)))
                self.elements.append(value)
                self.lib.snd_ctl_elem_value_set_interface(value,3) # SND_CTL_ELEM_IFACE_PCM
                self.lib.snd_ctl_elem_value_set_name(value,b'PCM Rate Shift 100000')
                self.lib.snd_ctl_elem_value_set_device(value,device)
                self.lib.snd_ctl_elem_value_set_subdevice(value,0)
                self.lib.snd_ctl_elem_value_set_index(value,0)
            self.original=self.read()
            if any(not 80000<=v<=120000 for v in self.original):
                raise ValueError(f'invalid ALSA rate controls: {self.original}')
        except BaseException:
            self.close();raise

    def check(self,ret):
        if ret<0:
            raise OSError(self.lib.snd_strerror(ret).decode())

    def read(self):
        result=[]
        for value in self.elements:
            self.check(self.lib.snd_ctl_elem_read(self.handle,value))
            result.append(self.lib.snd_ctl_elem_value_get_integer(value,0))
        return result

    def write(self,values):
        for elem,value in zip(self.elements,values):
            self.lib.snd_ctl_elem_value_set_integer(elem,0,value)
            self.check(self.lib.snd_ctl_elem_write(self.handle,elem))

    def close(self):
        for value in self.elements:self.lib.snd_ctl_elem_value_free(value)
        self.elements=[]
        if self.handle.value:self.lib.snd_ctl_close(self.handle);self.handle=ctypes.c_void_p()


def stamp():
    m1=time.monotonic_ns();raw=time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW);m2=time.monotonic_ns()
    return (m1+m2)//2,raw,m2-m1


def main():
    import fcntl
    RUN.mkdir(mode=0o755,parents=True,exist_ok=True)
    with (RUN/'owner.lock').open('w') as owner:
        fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
        rates=ALSARates();tracker=ClockRatio();value=None;updates=0;last_status=0
        def stop(*_):raise SystemExit(0)
        for sig in (signal.SIGTERM,signal.SIGINT,signal.SIGHUP):signal.signal(sig,stop)
        print('audio-clock: tracking MONOTONIC_RAW with both ALSA loopback rate controls',flush=True)
        try:
            while True:
                target=tracker.update(*stamp())
                if target is not None and (value is None or abs(target-value)>=2):
                    rates.write([target,target]);value=target;updates+=1
                now=time.monotonic()
                if now-last_status>=1:
                    state={'ready':value is not None,'rate_shift':value,
                           'correction_ppm':None if value is None else (value-NOMINAL)*10,
                           'updates':updates,'monotonic':now,'pid':os.getpid()}
                    tmp=RUN/'status.tmp';tmp.write_text(json.dumps(state)+'\n');os.replace(tmp,RUN/'status.json')
                    last_status=now
                time.sleep(.05)
        finally:
            try:
                # Return to the nominal standalone driver policy on stop.
                # A restarted service must not inherit a killed instance's correction.
                rates.write([NOMINAL,NOMINAL])
            finally:
                rates.close();(RUN/'status.json').unlink(missing_ok=True)


if __name__=='__main__':
    sys.exit(main())
