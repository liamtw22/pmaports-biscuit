#!/usr/bin/env python3
"""Best-effort acoustic direction observer. Never owns or backpressures capture."""
from array import array
import json
import math
import operator
import os
from collections import deque
from pathlib import Path
import socket
import sys
import time

RUN = Path('/run/biscuit-direction')
SOCKET = '/run/biscuit-mic/direction.sock'
# Measured PCB positions / capture channel order, tools/beamform/biscuit-mic-array.json.
POSITIONS = [(-.03118,.018),(-.03118,-.018),(0,-.036),(.03118,-.018),(.03118,.018),(0,.036)]

def angle_delta(a,b):
    return (a-b+180)%360-180

class Tracker:
    """Reject isolated room reflections; smooth accepted movement on a circle."""
    def __init__(self):
        self.samples=deque(maxlen=5)
        self.angle=None
        self.last=0
        self.confirmed=0

    def update(self, candidate, now):
        if candidate is not None:
            self.samples.append((now,candidate))
        while self.samples and now-self.samples[0][0]>.8:
            self.samples.popleft()
        cluster=[]
        # Three agreeing readings are needed before a new location wins.
        for _,angle in reversed(self.samples):
            near=[(t,a) for t,a in self.samples if abs(angle_delta(a,angle))<=30]
            if len(near)>len(cluster):cluster=near
        if len(cluster)>=3 and now-cluster[-1][0]<.3:
            x=sum(math.cos(math.radians(a)) for t,a in cluster)
            y=sum(math.sin(math.radians(a)) for t,a in cluster)
            target=math.degrees(math.atan2(y,x))%360
            self.confirmed=now
            if self.angle is None:
                self.angle=target
            else:
                delta=angle_delta(target,self.angle)
                dt=max(0,min(.25,now-self.last))
                if abs(delta)>5:
                    step=delta*(1-math.exp(-dt/.25))
                    self.angle=(self.angle+max(-240*dt,min(240*dt,step)))%360
        if now-self.confirmed>.8:
            self.angle=None
        self.last=now
        return self.angle

# The observer runs all day in the default "always" direction mode, so its cost
# matters. The first version decoded and correlated sample by sample in Python:
# 58 ms a window on Device 1, 45% of a core at 7.8 windows/s. Here no Python code
# runs per sample. Samples stay integers, the mean is removed algebraically, and
# every sum is exact until the final division, so the scores equal the float
# version's to rounding (qual-captures/direction-0926/equivalence.py).
try:
    dot = math.sumprod                      # Python 3.12+: one C call per product
except AttributeError:
    def dot(a, b):
        return sum(map(operator.mul, a, b))

FULL_SCALE2 = 8388608 ** 2
# Top byte of a 24-bit sample -> its sign-extension byte.
SIGN = bytes(0xff if i & 0x80 else 0 for i in range(256))

def decode(packet):
    """S24_3LE frames of 8 channels -> the 6 capsule channels as lists of ints.

    Extended-slice copies widen each sample to a little-endian int32 with its
    sign byte, all in C.
    """
    wide = bytearray(len(packet) // 3 * 4)
    wide[0::4] = packet[0::3]
    wide[1::4] = packet[1::3]
    wide[2::4] = packet[2::3]
    wide[3::4] = packet[2::3].translate(SIGN)
    ints = array('i')
    ints.frombytes(wide)
    if sys.byteorder != 'little':
        ints.byteswap()
    return [ints[c::8].tolist() for c in range(6)]

def estimate(packet):
    if len(packet) != 128 * 8 * 3 * 4:
        return None
    channels = decode(packet)
    n = len(channels[0])
    sums = [sum(c) for c in channels]
    # DC rejection, exactly: n * sum((x - S/n)^2) = n * sum(x^2) - S^2. A common
    # positive gain does not affect normalized delays.
    power = sum(n*dot(c, c) - s*s for c, s in zip(channels, sums)) / (6*n*n*FULL_SCALE2)
    if power < 1e-12:
        return None
    delays, confidence = [], []
    for c in range(3):
        a, b = channels[c], channels[c+3]
        sa, sb = sums[c], sums[c+3]
        # Correlate a[8:504] with b[8+lag:504+lag] about the whole window's means
        # sa/n and sb/n, every term scaled by n*n to stay in integers.
        aa = a[8:504]
        w = len(aa)
        suma = sum(aa)
        ea = n*n*dot(aa, aa) - 2*n*sa*suma + w*sa*sa
        bb = b[3:499]
        sumb, sumbb = sum(bb), dot(bb, bb)
        scores = []
        for lag in range(-5, 6):
            if lag > -5:                    # slide the b window on by one sample
                out, new = b[7+lag], b[503+lag]
                sumb += new - out
                sumbb += new*new - out*out
            eb = n*n*sumbb - 2*n*sb*sumb + w*sb*sb
            cross = n*n*dot(aa, b[8+lag:504+lag]) - n*sb*suma - n*sa*sumb + w*sa*sb
            scores.append(cross / max(math.sqrt(ea * eb), 1e-30 * n*n*FULL_SCALE2))
        peak = max(range(11), key=scores.__getitem__)
        if peak in (0,10):
            return None
        l,m,r = scores[peak-1:peak+2]
        if m < .45:
            return None     # fails the confidence test below whatever the other pairs say
        delta = .5*(l-r)/(l-2*m+r) if abs(l-2*m+r)>1e-9 else 0
        delays.append(peak-5+max(-.5,min(.5,delta)))
        confidence.append(m)
    # Opposite-pair least squares: b[t+lag] aligns with a[t]. The source
    # reaches the nearer capsule first, so lag=(pos_a-pos_b).u * rate/c.
    vectors = [(2*x*16000/343,2*y*16000/343) for x,y in POSITIONS[:3]]
    xx=sum(x*x for x,y in vectors); yy=sum(y*y for x,y in vectors)
    xy=sum(x*y for x,y in vectors)
    xd=sum(x*d for (x,y),d in zip(vectors,delays))
    yd=sum(y*d for (x,y),d in zip(vectors,delays))
    determinant=xx*yy-xy*xy
    ux=(xd*yy-yd*xy)/determinant; uy=(yd*xx-xd*xy)/determinant
    norm=math.hypot(ux,uy)
    residual=math.sqrt(sum((x*ux+y*uy-d)**2 for (x,y),d in zip(vectors,delays))/3)
    if min(confidence)<.45 or norm<.25 or norm>1.3 or residual>.65:
        return None
    return dict(angle=math.degrees(math.atan2(uy,ux))%360,
                confidence=round(min(confidence),3), power=power)

def main():
    RUN.mkdir(exist_ok=True)
    sock=socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM)
    sock.settimeout(1)
    sock.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,32768)
    Path(SOCKET).unlink(missing_ok=True)
    sock.bind(SOCKET); os.chmod(SOCKET,0o600)
    floor=None; speaker=None; speaker_at=0
    tracker=Tracker()
    try:
        while True:
            try:
                packet=sock.recv(16384)
                # Prefer the newest window if this low-priority observer was delayed.
                sock.setblocking(False)
                while True:
                    try: packet=sock.recv(16384)
                    except BlockingIOError: break
                sock.settimeout(1)
            except socket.timeout:
                packet=b''
            now=time.monotonic()
            try: muted=Path('/run/biscuit-audio/muted').read_text().strip()!='0'
            except OSError: muted=True
            found=None if muted else estimate(packet)
            noise=tracker.update(found['angle'] if found else None,now)
            if found:
                power=found['power']
                if floor is None: floor=power*.25
                # Speech/activity estimate, not semantic speech recognition.
                # Steady coherent sound is the noise direction; a new source
                # rising above its recent floor also updates the speaker pointer.
                if noise is not None and power > max(1e-12, floor*1.8):
                    speaker=noise; speaker_at=now
                floor=min(power, floor*1.02+1e-13)
            elif floor is not None:
                floor*=.95
            if muted or now-speaker_at>2:
                speaker=None
            if muted:
                noise=None
                tracker=Tracker()
            doc=dict(monotonic=now, speaker=speaker, noise=noise,
                     confidence=found['confidence'] if found else 0,
                     method='opposite-pair TDOA; speaker uses activity gate')
            tmp=RUN/'state.tmp'; tmp.write_text(json.dumps(doc)); os.replace(tmp,RUN/'state')
    finally:
        sock.close(); Path(SOCKET).unlink(missing_ok=True)

if __name__=='__main__': main()
