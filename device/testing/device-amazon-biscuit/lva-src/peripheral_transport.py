# SPDX-License-Identifier: Apache-2.0
#
# NEW FILE, not present upstream. Written by liamtw22 and contributors,
# 2026, for this port's fork of OHF-Voice/linux-voice-assistant
# (based on commit b0c53c41c11e),
#   https://github.com/OHF-Voice/linux-voice-assistant
# and licensed under the Apache License 2.0 like the rest of that fork
# (LICENSES/Apache-2.0.txt).
# Purpose: the root-only Unix-socket transport for the peripheral API.
"""Root-peer-only Linux peripheral transport. No TCP listener or bearer secret."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import errno
import json
import os
from pathlib import Path
import socket
import stat
import struct
import sys

SOCKET_PATH = '/run/biscuit-peripheral/control.sock'
MAX_MESSAGE = 16384
MAX_CLIENTS = 4


class TransportRefused(RuntimeError):
    """Fixed-code failure; never includes client data or credentials."""


def require_root():
    if sys.platform != 'linux' or not hasattr(socket, 'SO_PEERCRED'):
        raise TransportRefused('linux_peer_credentials_required')
    if os.geteuid() != 0:
        raise TransportRefused('root_required')


def check_stat(info, kind, mode=None):
    predicate = {'directory': stat.S_ISDIR, 'socket': stat.S_ISSOCK,
                 'file': stat.S_ISREG}[kind]
    if not predicate(info.st_mode) or info.st_uid != 0:
        raise TransportRefused('unsafe_' + kind)
    permissions = stat.S_IMODE(info.st_mode)
    if (mode is not None and permissions != mode) or (mode is None and permissions & 0o022):
        raise TransportRefused('unsafe_permissions')
    if kind == 'file' and info.st_nlink != 1:
        raise TransportRefused('unsafe_links')


def trusted_parent(path=None, *, create=False):
    path = SOCKET_PATH if path is None else path
    require_root()
    if str(path) != SOCKET_PATH:
        raise TransportRefused('unsupported_socket_path')
    parent = Path(path).parent
    # lstat rejects symlinks, including /run. No arbitrary path traversal.
    check_stat(os.lstat('/'), 'directory')
    check_stat(os.lstat('/run'), 'directory')
    if create:
        try:
            os.mkdir(parent, 0o700)
        except FileExistsError:
            pass
    check_stat(os.lstat(parent), 'directory', 0o700)
    return parent


def root_peer_pid(sock):
    try:
        if sock is None or sock.family != socket.AF_UNIX:
            return None
        credentials = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
        pid, uid, gid = struct.unpack('3i', credentials)
        return pid if pid > 0 and uid == 0 else None
    except (AttributeError, OSError, ValueError, struct.error):
        return None


def peer_is_root(sock):
    return root_peer_pid(sock) is not None


def authorized_websocket(ws):
    try:
        return (ws.path == '/' and ws.request_headers.get('Origin') is None
                and peer_is_root(ws.transport.get_extra_info('socket')))
    except (AttributeError, LookupError, ValueError):
        return False


def valid_command(raw):
    if type(raw) is not str or len(raw) > MAX_MESSAGE:
        return False
    try:
        message = json.loads(raw)
    except (ValueError, RecursionError):
        return False
    return (type(message) is dict and type(message.get('command')) is str
            and 0 < len(message['command']) <= 64
            and ('data' not in message or type(message['data']) is dict))


def configuration(clients=()):
    pids = set()
    for ws in tuple(clients):
        try:
            pid = root_peer_pid(ws.transport.get_extra_info('socket'))
            if pid is not None:
                pids.add(pid)
        except (AttributeError, OSError):
            pass
    return {'transport': 'unix-websocket', 'socket_path': SOCKET_PATH,
            'peer_policy': 'linux-so-peercred-uid-0', 'tcp_enabled': False,
            'max_message_bytes': MAX_MESSAGE, 'root_peer_pids': sorted(pids)}


class BoundSocket:
    """Own a lock, socket inode and listener. Never unlink an unowned path."""
    def __init__(self, path=None):
        self.path = SOCKET_PATH if path is None else str(path)
        self.sock = None
        self.lock_fd = None
        self.inode = None

    def bind(self):
        import fcntl
        parent = trusted_parent(self.path, create=True)
        try:
            self.lock_fd = os.open(parent / 'server.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            check_stat(os.fstat(self.lock_fd), 'file', 0o600)
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise TransportRefused('server_already_owned') from None
            try:
                previous = os.lstat(self.path)
            except FileNotFoundError:
                previous = None
            if previous is not None:
                check_stat(previous, 'socket', 0o600)
                # Only recover an exact root-owned stale socket under our exclusive lock.
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    probe.settimeout(0.1)
                    probe.connect(self.path)
                except OSError as exc:
                    if exc.errno != errno.ECONNREFUSED:
                        raise TransportRefused('socket_state_unknown') from None
                else:
                    raise TransportRefused('socket_still_served')
                finally:
                    probe.close()
                current = os.lstat(self.path)
                if (current.st_dev, current.st_ino) != (previous.st_dev, previous.st_ino):
                    raise TransportRefused('socket_changed')
                os.unlink(self.path)
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.bind(self.path)
            self.inode = (os.lstat(self.path).st_dev, os.lstat(self.path).st_ino)
            os.chmod(self.path, 0o600)
            check_stat(os.lstat(self.path), 'socket', 0o600)
            self.sock.setblocking(False)
            self.sock.listen(8)
            return self.sock
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        try:
            if self.inode is not None:
                try:
                    current = os.lstat(self.path)
                except FileNotFoundError:
                    current = None
                if (current is not None and stat.S_ISSOCK(current.st_mode)
                        and current.st_uid == 0
                        and (current.st_dev, current.st_ino) == self.inode):
                    os.unlink(self.path)
        finally:
            self.inode = None
            if self.lock_fd is not None:
                os.close(self.lock_fd)
                self.lock_fd = None


class PeripheralListener:
    """Owns the websockets server and its bound inode, and reports exact drain state.

    A resident drain coordinator previously inspected a raw websockets server via
    ``is_serving()``/``sockets``. Those two names are kept so an existing drain
    predicate composes unchanged, but for a Unix endpoint they are not sufficient:
    the server can be closed while this process still holds the listening socket,
    the exclusive lock and the socket inode. ``drain_status()`` reports every
    released resource so a composed drain can require the complete condition.
    Every accessor is read-only, safe before ``stop()``, after a successful
    ``stop()`` and after a partially failed one, and never raises.
    """

    def __init__(self, server, binding):
        self.server = server
        self.binding = binding

    def is_serving(self):
        """False once the wrapped server stopped accepting. Never raises."""
        try:
            return bool(self.server.is_serving())
        except (AttributeError, TypeError):
            # An unknown server object cannot be asserted closed.
            return True

    @property
    def sockets(self):
        """The wrapped server's sockets; empty once closed. Never raises."""
        try:
            sockets = self.server.sockets
        except AttributeError:
            return ()
        return () if sockets is None else sockets

    def endpoint_released(self):
        """True only when this listener no longer owns a bound socket inode.

        ``BoundSocket.close()`` clears ``inode`` after unlinking exactly its own
        inode, so a retained inode means the filesystem endpoint is still ours.
        """
        try:
            return self.binding.inode is None
        except AttributeError:
            return False

    def drain_status(self):
        """Exact per-resource release state for a composed drain decision."""
        binding = self.binding
        socket_closed = getattr(binding, 'sock', False) is None
        lock_released = getattr(binding, 'lock_fd', False) is None
        serving = self.is_serving()
        sockets_clear = not self.sockets
        endpoint_released = self.endpoint_released()
        return {'serving': serving, 'server_sockets': len(self.sockets),
                'listen_socket_closed': socket_closed,
                'lock_released': lock_released,
                'endpoint_released': endpoint_released,
                'complete': (serving is False and sockets_clear and socket_closed
                             and lock_released and endpoint_released)}

    async def stop(self):
        self.server.close()
        try:
            await self.server.wait_closed()
        finally:
            self.binding.close()


async def listen(handler):
    require_root()
    from websockets.legacy.server import unix_serve
    binding = BoundSocket()
    try:
        raw_socket = binding.bind()
        server = await unix_serve(handler, sock=raw_socket, origins=[None],
                                  max_size=MAX_MESSAGE, max_queue=4,
                                  close_timeout=2, ping_timeout=20)
        return PeripheralListener(server, binding)
    except BaseException:
        binding.close()
        raise


@asynccontextmanager
async def connect():
    """Validate the server before sending any registration, state or command."""
    trusted_parent()
    check_stat(os.lstat(SOCKET_PATH), 'socket', 0o600)
    from websockets.legacy.client import unix_connect
    raw_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    raw_socket.setblocking(False)
    try:
        await asyncio.wait_for(asyncio.get_running_loop().sock_connect(raw_socket, SOCKET_PATH), 3)
        if not peer_is_root(raw_socket):
            raise TransportRefused('server_peer_refused')
        async with unix_connect(sock=raw_socket, uri='ws://localhost/', open_timeout=3,
                                max_size=65536, max_queue=8, close_timeout=2) as ws:
            yield ws
    finally:
        raw_socket.close()
