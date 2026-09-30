#!/bin/sh
# LVA receives the assistant branch; the hub alone owns the hardware PCM.
exec /usr/bin/python3 /usr/bin/biscuit-mic-client.py assistant
