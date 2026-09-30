#!/usr/bin/env python3
"""Publish the hub call feed; stop only the children this process owns."""
import signal
import subprocess
import sys
import time

def main():
    children=[]
    def stop(*_): raise SystemExit(0)
    for sig in (signal.SIGTERM,signal.SIGINT): signal.signal(sig,stop)
    try:
        client=subprocess.Popen(['/usr/bin/python3','/usr/bin/biscuit-mic-client.py','call'],
                                stdout=subprocess.PIPE)
        children.append(client)
        sink=subprocess.Popen(['aplay','-D','hw:Loopback,1,0','-f','S16_LE',
                               '-r','16000','-c','1','-q'],stdin=client.stdout)
        children.append(sink); client.stdout.close()
        while all(child.poll() is None for child in children): time.sleep(.2)
        return 1
    finally:
        for child in children:
            if child.poll() is None: child.terminate()
        for child in children:
            try: child.wait(timeout=2)
            except subprocess.TimeoutExpired: child.kill(); child.wait()

if __name__=='__main__': sys.exit(main())
