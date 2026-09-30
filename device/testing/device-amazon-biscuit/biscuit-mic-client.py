#!/usr/bin/env python3
"""Read one live hub feed. Reconnect with silence; never restart hardware capture."""
import argparse
import socket
import sys
import time

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('feed',choices=('assistant','call'))
    args=p.parse_args()
    try:
        while True:
            try:
                with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
                    sock.settimeout(2)
                    sock.connect('/run/biscuit-mic/'+args.feed+'.sock')
                    pending=b''
                    while True:
                        data=sock.recv(2048)
                        if not data: break
                        pending+=data
                        size=len(pending)//2*2
                        if size: sys.stdout.buffer.write(pending[:size]); sys.stdout.buffer.flush()
                        pending=pending[size:]
            except (OSError,TimeoutError):
                pass
            # Keep an existing consumer alive during bounded hub restarts.
            for _ in range(10):
                sys.stdout.buffer.write(bytes(256)); sys.stdout.buffer.flush(); time.sleep(.008)
    except BrokenPipeError:
        return 0

if __name__=='__main__': raise SystemExit(main())
