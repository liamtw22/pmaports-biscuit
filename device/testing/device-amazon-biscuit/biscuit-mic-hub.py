#!/usr/bin/env python3
"""One capture owner, separate assistant and processed-call streams, bounded fan-out."""
import argparse
import asyncio
import contextlib
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import sys
import threading
import time
import biscuit_call_profile as profile

RUN=Path('/run/biscuit-mic')
MUTE=Path('/run/biscuit-audio/muted')
RAW_BYTES=128*8*3
MONO_BYTES=128*2

# aarch64 numbers. musl stubs os.sched_setscheduler with ENOSYS and hides
# os.gettid, so a thread can only reschedule itself through raw syscalls; the
# long version is in lva-src/qualification_v1.py.
_NR_GETTID_AARCH64=178
_NR_SCHED_SETSCHEDULER_AARCH64=119

def leave_realtime():
    """Move the CALLING thread to SCHED_OTHER. Best effort; True if it took."""
    try:
        import ctypes, platform
        if platform.machine() not in ('aarch64','arm64'): return False
        libc=ctypes.CDLL(None,use_errno=True)
        class Param(ctypes.Structure): _fields_=[('sched_priority',ctypes.c_int)]
        tid=libc.syscall(_NR_GETTID_AARCH64)
        return tid>0 and libc.syscall(_NR_SCHED_SETSCHEDULER_AARCH64,tid,0,ctypes.byref(Param(0)))==0
    except Exception:
        return False

class DiskLog:
    """A log that reaches the disk from a thread that is not realtime.

    Nothing in the audio path may write to a journalled filesystem itself. On
    2026-09-25 biscuit-call-dsp (FIFO 45) blocked for 0.6 s writing its own
    stderr, which was then a file on the ext4 root: ext4 was waiting on the jbd2
    journal while mkinitfs wrote. The call branch overflowed and restarted cold
    (3.8).
    Writers now get a pipe instead. A pipe never blocks while it has room, and
    64 KiB holds hours of status lines. This thread, at SCHED_OTHER, takes the
    disk's stalls, including the open() of a log file that does not exist yet.

    The thread keeps reading whatever happens to the file. If it stopped, the
    pipe would fill and the writer would block, which is the fault this exists
    to remove. A write that fails (ENOSPC) drops its data, and reading goes on.

    A file log is capped at ROTATE_BYTES and keeps one previous generation as
    <name>.1. The call DSP alone writes a status line every 10 s, about 0.5 MB
    a day, and nothing ever rotated it: the file stood at 2.9 MB after a few
    days. The rename and reopen happen here, so they stall nobody either.
    """
    ROTATE_BYTES=2*1024*1024

    def __init__(self,target,name):
        self.target=target   # a path, or an fd that this object now owns
        self.read_fd,self.write_fd=os.pipe()
        self.thread=threading.Thread(target=self._drain,name=name,daemon=True)
        self.thread.start()

    def _drain(self):
        leave_realtime()
        path=self.target if isinstance(self.target,str) else None
        out=self.target
        size=0
        while True:
            data=os.read(self.read_fd,65536)
            if not data: break
            try:
                if path and size>=self.ROTATE_BYTES and isinstance(out,int):
                    os.close(out)
                    out=path
                    os.replace(path,path+'.1')
                if isinstance(out,str):
                    out=os.open(out,os.O_WRONLY|os.O_APPEND|os.O_CREAT|os.O_CLOEXEC,0o666)
                    size=os.fstat(out).st_size
                while data:
                    n=os.write(out,data)
                    data=data[n:]
                    size+=n
            except OSError:
                pass
        os.close(self.read_fd)
        if isinstance(out,int):
            with contextlib.suppress(OSError): os.close(out)

def uptime():
    """Seconds since boot. The only clock in this log that cannot lie."""
    try:
        with open('/proc/uptime') as handle:
            return float(handle.read().split()[0])
    except (OSError,ValueError):
        return -1.0


class BranchInput:
    """Bounded paired-frame queue; overload resets only this DSP generation."""
    def __init__(self):
        self.queue=asyncio.Queue(maxsize=32) # 256 ms; includes 125 ms ALSA bursts.
        self.overflow=asyncio.Event()
        self.dropped_blocks=0
        self.offered=0
        self.written=0
        self.peak=0
        self.drain_max_ms=0.0
        self.transport_bytes=0
        self.last_write=time.monotonic()

    def since_write(self):
        """Milliseconds since a block last reached the child.

        An overflow with this near zero means the child was keeping up until
        the moment the queue filled - a producer burst. A large value means the
        branch had been starved first. The two have different causes and the
        log could not previously tell them apart.
        """
        return (time.monotonic()-self.last_write)*1000.0

    def offer(self, block):
        self.offered+=1
        if self.overflow.is_set():
            self.dropped_blocks+=1
            return
        try:
            self.queue.put_nowait(block)
            self.peak=max(self.peak,self.queue.qsize())
        except asyncio.QueueFull:
            self.dropped_blocks+=1
            self.overflow.set()

    def reset(self):
        while not self.queue.empty():
            self.queue.get_nowait()
            self.dropped_blocks+=1
        self.overflow.clear()

    async def write(self, child):
        while True:
            block=await self.queue.get()
            child.stdin.write(block)
            self.written+=1
            self.last_write=time.monotonic()
            self.transport_bytes=child.stdin.transport.get_write_buffer_size()
            start=time.monotonic()
            await child.stdin.drain()
            self.drain_max_ms=max(self.drain_max_ms,(time.monotonic()-start)*1000)

def read_settings(path='/opt/persist/mic.env'):
    result={}
    try:
        for line in Path(path).read_text().splitlines():
            key,sep,value=line.strip().partition('=')
            if sep and not key.startswith('#'):
                result[key.strip()]=value.strip().strip('"\'')
    except OSError:
        pass
    # Legacy centre means physical MK7, not logical channel 0.
    if result.get('BISCUIT_MIC_SOURCE')=='centre':
        result['BISCUIT_MIC_SOURCE']='single'
        result.setdefault('BISCUIT_MIC_CHANNEL','6')
    return result

# muted() runs for every block of every branch and client, about 500 times a
# second at SCHED_FIFO. Reading the file each time cost 262 us a call on this
# SoC, about a third of the hub's CPU. A stat costs 17 us, so the file is only
# re-read when it changes. biscuit-audio publishes it with os.replace, so every
# change is a new inode and the next block sees it, exactly as before. It is
# also re-read at least every MUTE_RECHECK_S as a backstop, and anything
# unreadable still counts as muted.
MUTE_RECHECK_S=0.25
_mute_cache=[None,True,0.0]   # (inode, mtime, size), value, when read

def muted():
    try:
        st=os.stat(MUTE)
    except OSError:
        _mute_cache[0]=None
        return True
    key=(st.st_ino,st.st_mtime_ns,st.st_size)
    now=time.monotonic()
    if key!=_mute_cache[0] or now-_mute_cache[2]>MUTE_RECHECK_S:
        try:
            value=MUTE.read_text().strip()!='0'
        except OSError:
            _mute_cache[0]=None
            return True
        _mute_cache[:]=[key,value,now]
    return _mute_cache[1]

class Hub:
    def __init__(self):
        self.direction_socket=socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM)
        self.direction_socket.setblocking(False)
        self.direction_window=bytearray()
        self.direction_tick=0
        self.direction_requested=False
        self.clients={'assistant':set(),'call':set()}
        self.frames={'assistant':0,'call':0}
        self.branch_failed={}
        self.dropped_clients=0
        self.processes=[]
        self.client_tasks=set()
        self.inputs={name:BranchInput() for name in self.clients}
        self.branch_ready={name:False for name in self.clients}
        self.branch_restarts={name:0 for name in self.clients}

    async def client(self, name, reader, writer):
        task=asyncio.current_task()
        self.client_tasks.add(task)
        queue=asyncio.Queue(maxsize=16) # Disconnect lagging clients; never build stale speech.
        self.clients[name].add(queue)
        try:
            while True:
                data=await queue.get()
                if data is None:
                    break
                if muted(): data=bytes(len(data))
                writer.write(data)
                await asyncio.wait_for(writer.drain(),0.5)
        except (ConnectionError,asyncio.TimeoutError):
            pass
        finally:
            self.clients[name].discard(queue)
            writer.close()
            try:
                with contextlib.suppress(ConnectionError,asyncio.TimeoutError):
                    await asyncio.wait_for(writer.wait_closed(),0.5)
            finally:
                self.client_tasks.discard(task)

    async def spawn(self,argv,env=None,label=None):
        # The child writes stderr into a pipe; see DiskLog. The thread ends by
        # itself at EOF, once the child and anything it forked have exited.
        log=DiskLog('/var/log/biscuit-mic-'+label+'.log','log-'+label)
        if label in ('assistant','call'):
            # Python's sched_setscheduler returns ENOSYS on this device;
            # the installed util-linux chrt successfully sets FIFO policy.
            # Set policy before exec so every subsequently created DSP thread
            # inherits it; a parent-side sweep races worker creation.
            if label=='assistant':
                # Biscuit's USB/SPI interrupts execute on CPU 0. Bulk USB
                # transfers stalled array workers/barriers there despite FIFO
                # priority. Keep all array threads on the other three cores;
                # apply before exec so independent restarts inherit it too.
                argv=['/usr/bin/taskset','-c','1-3',*argv]
            argv=['/usr/bin/chrt','-f','45',*argv]
        try:
            child=await asyncio.create_subprocess_exec(*argv,stdin=asyncio.subprocess.PIPE,
                      stdout=asyncio.subprocess.PIPE,stderr=log.write_fd,env=env,
                      start_new_session=True)
        finally:
            os.close(log.write_fd)   # the child holds its own copy
        self.processes.append(child)
        return child

    async def tee(self,raw):
        while True:
            block=await asyncio.wait_for(raw.stdout.readexactly(RAW_BYTES),3)
            # A slow assistant must never backpressure hardware capture or calls.
            # Capsules and reference stay paired; a queue overflow resets that
            # branch's DSP rather than continuing adaptation across lost frames.
            for name in ('call','assistant'):
                self.inputs[name].offer(block)
            phase=self.direction_tick % 16
            self.direction_tick+=1
            if phase == 0:
                try:
                    self.direction_requested=time.monotonic()-float(Path('/run/biscuit-ring/direction-active').read_text())<2
                except (OSError,ValueError): self.direction_requested=False
            if phase < 4 and self.direction_requested:
                self.direction_window.extend(block)
            if phase == 3 and self.direction_window:
                if not muted():
                    try:
                        self.direction_socket.sendto(self.direction_window,str(RUN/'direction.sock'))
                    except OSError:
                        pass  # Missing/slow observer cannot hold up either audio branch.
                self.direction_window.clear()

    async def stop_child(self,child):
        with contextlib.suppress(ProcessLookupError): os.killpg(child.pid,signal.SIGTERM)
        try: await asyncio.wait_for(child.wait(),2)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError): os.killpg(child.pid,signal.SIGKILL)
            await child.wait()
        if child in self.processes: self.processes.remove(child)

    # A branch that never emits a frame is broken, not merely restarting. The loop
    # already ends with a 0.1s bound; this adds escalation on top of it and, more
    # importantly, a give-up. A working branch that resets on overflow is unaffected.
    BRANCH_BACKOFF=(0.25,0.5,1.0,2.0,4.0,8.0)
    BRANCH_GIVE_UP=12
    # Two restarts inside this window count as churn. The escalation above used
    # to apply only when a branch produced NOTHING between restarts (barren) -
    # the comment above says a working branch that resets on overflow is
    # unaffected, and that is exactly the case that hurts. A branch that keeps
    # producing AND keeps overflowing is 'working' by that test, so it restarts on
    # the 0.1s floor alone. The log carries episodes of eleven consecutive
    # restarts with dropped_blocks climbing 17 -> 5,607, which is 45 seconds of
    # audio discarded by the recovery rather than by the fault. Those episodes
    # cannot be dated - written= is not a clock, it advances only as blocks reach
    # the child and therefore stalls exactly when things go wrong - so how long
    # they took is unknown. The code path is the evidence here, not a timeline.
    #
    # A trigger should cost one restart. Churn escalates on the same ladder, so
    # a branch that genuinely cannot keep up ends up retrying every 8s instead
    # of four times a second, and one that recovers is unaffected.
    # A SHORT ladder on purpose, capped at a second. During a backoff no child is
    # consuming, so the raw queue keeps discarding - a long sleep trades a restart
    # storm for deafness, which is the wrong trade for a voice assistant. The
    # value of backing off here is not fewer dropped blocks: it is not spawning a
    # process four times a second and not sending every client a discontinuity
    # marker each time. One restart per second bounds that tenfold while never
    # being deaf for longer than the queue already covers.
    BRANCH_CHURN_BACKOFF=(0.25,0.5,1.0)
    BRANCH_CHURN_WINDOW=5.0

    async def branch(self,name,argv,env=None):
        feed=self.inputs[name]
        barren=0
        churn=0
        last_restart=0.0
        while True:
            delay=0.0
            if barren:
                if barren>=self.BRANCH_GIVE_UP:
                    self.branch_failed[name]=('abandoned after %d restarts with no output'
                                              % barren)
                    print(f'mic-hub: abandoning {name} at up={uptime():.1f}s: '
                          f'{self.branch_failed[name]}',file=sys.stderr,flush=True)
                    return
                delay=self.BRANCH_BACKOFF[min(barren-1,len(self.BRANCH_BACKOFF)-1)]
                print(f'mic-hub: {name} produced no output at up={uptime():.1f}s; '
                      f'backing off {delay}s (consecutive={barren})',
                      file=sys.stderr,flush=True)
            if churn:
                churn_delay=self.BRANCH_CHURN_BACKOFF[
                    min(churn-1,len(self.BRANCH_CHURN_BACKOFF)-1)]
                if churn_delay>delay:
                    delay=churn_delay
                    print(f'mic-hub: {name} restarting repeatedly at up={uptime():.1f}s; '
                          f'backing off {delay}s (churn={churn})',
                          file=sys.stderr,flush=True)
            if delay:
                await asyncio.sleep(delay)
            frames_before=self.frames[name]
            feed.reset()
            child=await self.spawn(argv,env,label=name)
            tasks=[asyncio.create_task(feed.write(child)),
                   asyncio.create_task(self.output(name,child)),
                   asyncio.create_task(feed.overflow.wait())]
            try:
                done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
                reason='raw queue overflow' if feed.overflow.is_set() else 'processing stream ended'
                for task in done:
                    if not task.cancelled() and task.exception() is not None:
                        reason=repr(task.exception())
                self.branch_restarts[name]+=1
                barren=barren+1 if self.frames[name]==frames_before else 0
                now=time.monotonic()
                churn=churn+1 if (now-last_restart)<self.BRANCH_CHURN_WINDOW else 0
                last_restart=now
                # up= is the real clock. written= is NOT one: it counts blocks
                # that reached the child, so it stalls exactly when things go
                # wrong, and a timeline derived from it is wrong in the one
                # place it matters. Without up= these lines could not be dated
                # at all - the log survives reboots, so 24 of 25 entries once
                # belonged to boots nobody could identify.
                print(f'mic-hub: restarting {name} only: {reason}; '
                      f'up={uptime():.1f}s queue={feed.queue.qsize()} '
                      f'written={feed.written} '
                      f'output={self.frames[name]} dropped={feed.dropped_blocks} '
                      f'drain_max_ms={feed.drain_max_ms:.1f} '
                      f'since_write={feed.since_write():.1f}ms '
                      f'transport_bytes={child.stdin.transport.get_write_buffer_size()}',
                      file=sys.stderr,flush=True)
            finally:
                self.branch_ready[name]=False
                # No consumer may unknowingly concatenate different DSP states.
                for queue in tuple(self.clients[name]):
                    while not queue.empty(): queue.get_nowait()
                    queue.put_nowait(None)
                for task in tasks: task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
                await self.stop_child(child)
            # Bound a repeatedly failing branch without blocking the other one.
            await asyncio.sleep(.1)

    async def output(self,name,child):
        while True:
            block=await asyncio.wait_for(child.stdout.readexactly(MONO_BYTES),3)
            if muted(): block=bytes(MONO_BYTES)
            self.frames[name]+=1
            self.branch_ready[name]=True
            for queue in tuple(self.clients[name]):
                if queue.full():
                    self.dropped_clients+=1
                    self.clients[name].discard(queue)
                    while not queue.empty(): queue.get_nowait()
                    queue.put_nowait(None)
                else: queue.put_nowait(block)

    async def status(self,settings):
        while True:
            data=dict(ready=all(self.branch_ready.values()),frames=self.frames,clients={k:len(v) for k,v in self.clients.items()},
                      dropped_clients=self.dropped_clients,muted=muted(),
                      branch_ready=self.branch_ready,branch_restarts=self.branch_restarts,
                      branch_dropped_blocks={k:v.dropped_blocks for k,v in self.inputs.items()},
                      branch_io={k:dict(offered=v.offered,written=v.written,
                          queue=v.queue.qsize(),peak=v.peak,drain_max_ms=round(v.drain_max_ms,3),
                          transport_bytes=v.transport_bytes) for k,v in self.inputs.items()},
                      assistant=settings.get('BISCUIT_MIC_SOURCE','array'),
                      call_microphone=settings.get('BISCUIT_CALL_MIC_CHANNEL','6'),
                      call_profile=settings.get('BISCUIT_CALL_PROFILE','open'))
            tmp=RUN/'status.tmp'; tmp.write_text(json.dumps(data)); os.replace(tmp,RUN/'status.json')
            await asyncio.sleep(1)

    async def run(self):
        RUN.mkdir(mode=0o750,parents=True,exist_ok=True)
        lock=open(RUN/'owner.lock','w')
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        (RUN/'status.json').unlink(missing_ok=True)
        settings=read_settings()
        env=dict(os.environ,**{k:v for k,v in settings.items() if k.startswith('BISCUIT_MIC_')})
        env.update(BISCUIT_DAC_REF='0',BISCUIT_MIC_GAIN=settings.get('MIC_GAIN','1'))
        call_mic=settings.get('BISCUIT_CALL_MIC_CHANNEL','6')
        if call_mic not in tuple(str(i) for i in range(7)): raise ValueError('invalid call mic')
        params=profile.resolve(settings.get('BISCUIT_CALL_PROFILE','open'))
        args=['/usr/bin/biscuit-call-dsp','--mic',call_mic,'--cal','/run/biscuit-miccal/gains',
              '--mute-file',str(MUTE)]
        for key in ('tail','target_db','max_gain_db','rise_db','fall_db'):
            args += ['--'+key.replace('_','-'),str(params[key])]
        if params['stock_hpf']: args.append('--stock-hpf')
        if settings.get('BISCUIT_CALL_PROCESSING')=='off': args.append('--bypass')
        servers=[]; tasks=[]
        try:
            # Calibration is read by both DSP children at startup, before raw capture.
            cal=await asyncio.create_subprocess_exec('/usr/bin/biscuit-miccal.py')
            await cal.wait()
            raw=await self.spawn(['/usr/bin/biscuit-mic-stream.sh','-raw'],
                                 dict(env,BISCUIT_MIC_INPUT='hardware'),label='capture')
            for name in self.clients:
                path=RUN/(name+'.sock')
                path.unlink(missing_ok=True)
                async def connected(r,w,name=name): await self.client(name,r,w)
                servers.append(await asyncio.start_unix_server(connected,path=str(path)))
                path.chmod(0o660)
            tasks=[asyncio.create_task(self.tee(raw)),
                   asyncio.create_task(self.branch('assistant',
                       ['/usr/bin/biscuit-mic-stream.sh',settings.get('MIC_MODE') or '-compose'],
                       dict(env,BISCUIT_MIC_INPUT='stdin'))),
                   asyncio.create_task(self.branch('call',args)),
                   asyncio.create_task(self.status(settings))]
            done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
            for task in done: task.result()
            raise RuntimeError('microphone stream ended')
        finally:
            for server in servers: server.close()
            # Close accepted streams before waiting for the listening servers;
            # newer asyncio waits for those streams in server.wait_closed().
            clients=tuple(self.client_tasks)
            for task in clients: task.cancel()
            await asyncio.gather(*clients,return_exceptions=True)
            for server in servers: await server.wait_closed()
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            for child in self.processes:
                with contextlib.suppress(ProcessLookupError): os.killpg(child.pid,signal.SIGTERM)
            for child in self.processes:
                try: await asyncio.wait_for(child.wait(),2)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError): os.killpg(child.pid,signal.SIGKILL)
                    await child.wait()
            for queues in self.clients.values():
                for queue in tuple(queues):
                    while not queue.empty(): queue.get_nowait()
                    queue.put_nowait(None)
            for name in self.clients: (RUN/(name+'.sock')).unlink(missing_ok=True)
            (RUN/'status.json').unlink(missing_ok=True)
            self.direction_socket.close()
            lock.close()

def main():
    # The hub runs at FIFO 44, and its own lines went to supervise-daemon's log
    # on the ext4 root straight from the event loop. It writes when a branch
    # restarts, which during heavy writeback is when the disk holds a writer
    # longest, and a stalled loop stops the tee for both branches. So its
    # stdout and stderr take the same route as the children's.
    saved=os.dup(2)
    own=DiskLog(os.dup(2),'log-hub')
    for fd in (1,2): os.dup2(own.write_fd,fd)
    os.close(own.write_fd)
    loop=asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    task=loop.create_task(Hub().run())
    for sig in (signal.SIGINT,signal.SIGTERM): loop.add_signal_handler(sig,task.cancel)
    try: loop.run_until_complete(task)
    except asyncio.CancelledError: pass
    finally:
        loop.close()
        # Give the last lines to the disk before exiting. Pointing 1 and 2 back
        # at the log closes the pipe, so the thread drains it, reaches EOF and
        # ends. Anything printed after this, such as a traceback, goes to the
        # log directly.
        with contextlib.suppress(Exception): sys.stdout.flush(); sys.stderr.flush()
        for fd in (1,2): os.dup2(saved,fd)
        os.close(saved)
        own.thread.join(2)

if __name__=='__main__': main()
