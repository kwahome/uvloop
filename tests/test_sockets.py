import asyncio
import pickle
import select
import socket
import sys
import time
import unittest

from uvloop import _testbase as tb


_SIZE = 1024 * 1024


class _TestSockets:

    async def recv_all(self, sock, nbytes):
        buf = b''
        while len(buf) < nbytes:
            buf += await self.loop.sock_recv(sock, nbytes - len(buf))
        return buf

    def test_socket_connect_recv_send(self):
        if self.is_asyncio_loop() and sys.version_info[:3] == (3, 5, 2):
            # See https://github.com/python/asyncio/pull/366 for details.
            raise unittest.SkipTest()

        def srv_gen(sock):
            sock.send(b'helo')
            data = sock.recv_all(4 * _SIZE)
            self.assertEqual(data, b'ehlo' * _SIZE)
            sock.send(b'O')
            sock.send(b'K')

        # We use @asyncio.coroutine & `yield from` to test
        # the compatibility of Cython's 'async def' coroutines.
        @asyncio.coroutine
        def client(sock, addr):
            yield from self.loop.sock_connect(sock, addr)
            data = yield from self.recv_all(sock, 4)
            self.assertEqual(data, b'helo')
            yield from self.loop.sock_sendall(sock, b'ehlo' * _SIZE)
            data = yield from self.recv_all(sock, 2)
            self.assertEqual(data, b'OK')

        with self.tcp_server(srv_gen) as srv:

            sock = socket.socket()
            with sock:
                sock.setblocking(False)
                self.loop.run_until_complete(client(sock, srv.addr))

    def test_socket_accept_recv_send(self):
        async def server():
            sock = socket.socket()
            sock.setblocking(False)

            with sock:
                sock.bind(('127.0.0.1', 0))
                sock.listen()

                fut = self.loop.run_in_executor(None, client,
                                                sock.getsockname())

                client_sock, _ = await self.loop.sock_accept(sock)

                with client_sock:
                    data = await self.recv_all(client_sock, _SIZE)
                    self.assertEqual(data, b'a' * _SIZE)

                await fut

        def client(addr):
            sock = socket.socket()
            with sock:
                sock.connect(addr)
                sock.sendall(b'a' * _SIZE)

        self.loop.run_until_complete(server())

    def test_socket_failed_connect(self):
        sock = socket.socket()
        with sock:
            sock.bind(('127.0.0.1', 0))
            addr = sock.getsockname()

        async def run():
            sock = socket.socket()
            with sock:
                sock.setblocking(False)
                with self.assertRaises(ConnectionRefusedError):
                    await self.loop.sock_connect(sock, addr)

        self.loop.run_until_complete(run())

    def test_socket_ipv6_addr(self):
        server_sock = socket.socket(socket.AF_INET6)
        with server_sock:
            try:
                server_sock.bind(('::1', 0))
            except OSError:
                raise unittest.SkipTest('IPv6 is not supported')

            addr = server_sock.getsockname()  # tuple of 4 elements for IPv6

            async def run():
                sock = socket.socket(socket.AF_INET6)
                with sock:
                    sock.setblocking(False)
                    # Check that sock_connect accepts 4-element address tuple
                    # for IPv6 sockets.
                    f = self.loop.sock_connect(sock, addr)
                    try:
                        await asyncio.wait_for(f, timeout=0.1, loop=self.loop)
                    except (asyncio.TimeoutError, ConnectionRefusedError):
                        # TimeoutError is expected.
                        pass

            self.loop.run_until_complete(run())

    def test_socket_blocking_error(self):
        self.loop.set_debug(True)
        sock = socket.socket()

        with sock:
            with self.assertRaisesRegex(ValueError, 'must be non-blocking'):
                self.loop.run_until_complete(
                    self.loop.sock_recv(sock, 0))

            with self.assertRaisesRegex(ValueError, 'must be non-blocking'):
                self.loop.run_until_complete(
                    self.loop.sock_sendall(sock, b''))

            with self.assertRaisesRegex(ValueError, 'must be non-blocking'):
                self.loop.run_until_complete(
                    self.loop.sock_accept(sock))

            with self.assertRaisesRegex(ValueError, 'must be non-blocking'):
                self.loop.run_until_complete(
                    self.loop.sock_connect(sock, (b'', 0)))

    def test_socket_fileno(self):
        rsock, wsock = socket.socketpair()
        f = asyncio.Future(loop=self.loop)

        def reader():
            rsock.recv(100)
            # We are done: unregister the file descriptor
            self.loop.remove_reader(rsock)
            f.set_result(None)

        def writer():
            wsock.send(b'abc')
            self.loop.remove_writer(wsock)

        with rsock, wsock:
            self.loop.add_reader(rsock, reader)
            self.loop.add_writer(wsock, writer)
            self.loop.run_until_complete(f)


class TestUVSockets(_TestSockets, tb.UVTestCase):

    @unittest.skipUnless(hasattr(select, 'epoll'), 'Linux only test')
    def test_socket_sync_remove(self):
        # See https://github.com/MagicStack/uvloop/issues/61 for details

        sock = socket.socket()
        epoll = select.epoll.fromfd(self.loop._get_backend_id())

        try:
            cb = lambda: None

            sock.bind(('127.0.0.1', 0))
            sock.listen(0)
            fd = sock.fileno()
            self.loop.add_reader(fd, cb)
            self.loop.run_until_complete(asyncio.sleep(0.01, loop=self.loop))
            self.loop.remove_reader(fd)
            with self.assertRaises(FileNotFoundError):
                epoll.modify(fd, 0)

        finally:
            sock.close()
            self.loop.close()
            epoll.close()

    def test_add_reader_or_writer_transport_fd(self):
        def assert_raises():
            return self.assertRaisesRegex(
                RuntimeError,
                r'File descriptor .* is used by transport')

        async def runner():
            tr, pr = await self.loop.create_connection(
                lambda: asyncio.Protocol(), sock=rsock)

            try:
                cb = lambda: None
                sock = tr.get_extra_info('socket')

                with assert_raises():
                    self.loop.add_reader(sock, cb)
                with assert_raises():
                    self.loop.add_reader(sock.fileno(), cb)

                with assert_raises():
                    self.loop.remove_reader(sock)
                with assert_raises():
                    self.loop.remove_reader(sock.fileno())

                with assert_raises():
                    self.loop.add_writer(sock, cb)
                with assert_raises():
                    self.loop.add_writer(sock.fileno(), cb)

                with assert_raises():
                    self.loop.remove_writer(sock)
                with assert_raises():
                    self.loop.remove_writer(sock.fileno())

            finally:
                tr.close()

        rsock, wsock = socket.socketpair()
        try:
            self.loop.run_until_complete(runner())
        finally:
            rsock.close()
            wsock.close()

    def test_pseudosocket(self):
        def assert_raises():
            return self.assertRaisesRegex(
                RuntimeError,
                r'File descriptor .* is used by transport')

        def test_pseudo(real_sock, pseudo_sock, *, is_dup=False):
            self.assertIn('AF_UNIX', repr(pseudo_sock))

            self.assertEqual(pseudo_sock.family, real_sock.family)
            self.assertEqual(pseudo_sock.type, real_sock.type)
            self.assertEqual(pseudo_sock.proto, real_sock.proto)

            with self.assertRaises(TypeError):
                pickle.dumps(pseudo_sock)

            na_meths = {
                'accept', 'connect', 'connect_ex', 'bind', 'listen',
                'makefile', 'sendfile', 'close', 'detach', 'shutdown',
                'sendmsg_afalg', 'sendmsg', 'sendto', 'send', 'sendall',
                'recv_into', 'recvfrom_into', 'recvmsg_into', 'recvmsg',
                'recvfrom', 'recv'
            }
            for methname in na_meths:
                meth = getattr(pseudo_sock, methname)
                with self.assertRaisesRegex(
                        TypeError,
                        r'.*not support ' + methname + r'\(\) method'):
                    meth()

            eq_meths = {
                'getsockname', 'getpeername', 'get_inheritable', 'gettimeout'
            }
            for methname in eq_meths:
                pmeth = getattr(pseudo_sock, methname)
                rmeth = getattr(real_sock, methname)

                # Call 2x to check caching paths
                self.assertEqual(pmeth(), rmeth())
                self.assertEqual(pmeth(), rmeth())

            self.assertEqual(
                pseudo_sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR),
                0)

            if not is_dup:
                self.assertEqual(pseudo_sock.fileno(), real_sock.fileno())

                duped = pseudo_sock.dup()
                with duped:
                    test_pseudo(duped, pseudo_sock, is_dup=True)

            with self.assertRaises(TypeError):
                with pseudo_sock:
                    pass

        async def runner():
            tr, pr = await self.loop.create_connection(
                lambda: asyncio.Protocol(), sock=rsock)

            try:
                sock = tr.get_extra_info('socket')
                test_pseudo(rsock, sock)
            finally:
                tr.close()

        rsock, wsock = socket.socketpair()
        try:
            self.loop.run_until_complete(runner())
        finally:
            rsock.close()
            wsock.close()

    def test_socket_connect_and_close(self):
        def srv_gen(sock):
            sock.send(b'helo')

        async def client(sock, addr):
            f = asyncio.ensure_future(self.loop.sock_connect(sock, addr),
                                      loop=self.loop)
            self.loop.call_soon(sock.close)
            await f
            return 'ok'

        with self.tcp_server(srv_gen) as srv:

            sock = socket.socket()
            with sock:
                sock.setblocking(False)
                r = self.loop.run_until_complete(client(sock, srv.addr))
                self.assertEqual(r, 'ok')

    def test_socket_recv_and_close(self):
        def srv_gen(sock):
            time.sleep(1.2)
            sock.send(b'helo')

        async def kill(sock):
            await asyncio.sleep(0.2, loop=self.loop)
            sock.close()

        async def client(sock, addr):
            await self.loop.sock_connect(sock, addr)

            f = asyncio.ensure_future(self.loop.sock_recv(sock, 10),
                                      loop=self.loop)
            self.loop.create_task(kill(sock))
            res = await f
            self.assertEqual(sock.fileno(), -1)
            return res

        with self.tcp_server(srv_gen) as srv:

            sock = socket.socket()
            with sock:
                sock.setblocking(False)
                c = client(sock, srv.addr)
                w = asyncio.wait_for(c, timeout=5.0, loop=self.loop)
                r = self.loop.run_until_complete(w)
                self.assertEqual(r, b'helo')

    def test_socket_recv_into_and_close(self):
        def srv_gen(sock):
            time.sleep(1.2)
            sock.send(b'helo')

        async def kill(sock):
            await asyncio.sleep(0.2, loop=self.loop)
            sock.close()

        async def client(sock, addr):
            await self.loop.sock_connect(sock, addr)

            data = bytearray(10)
            with memoryview(data) as buf:
                f = asyncio.ensure_future(self.loop.sock_recv_into(sock, buf),
                                          loop=self.loop)
                self.loop.create_task(kill(sock))
                rcvd = await f
                data = data[:rcvd]
            self.assertEqual(sock.fileno(), -1)
            return bytes(data)

        with self.tcp_server(srv_gen) as srv:

            sock = socket.socket()
            with sock:
                sock.setblocking(False)
                c = client(sock, srv.addr)
                w = asyncio.wait_for(c, timeout=5.0, loop=self.loop)
                r = self.loop.run_until_complete(w)
                self.assertEqual(r, b'helo')

    def test_socket_send_and_close(self):
        ok = False

        def srv_gen(sock):
            nonlocal ok
            b = sock.recv_all(2)
            if b == b'hi':
                ok = True
            sock.send(b'ii')

        async def client(sock, addr):
            await self.loop.sock_connect(sock, addr)

            s2 = sock.dup()  # Don't let it drop connection until `f` is done
            with s2:
                f = asyncio.ensure_future(self.loop.sock_sendall(sock, b'hi'),
                                          loop=self.loop)
                self.loop.call_soon(sock.close)
                await f

                return await self.loop.sock_recv(s2, 2)

        with self.tcp_server(srv_gen) as srv:

            sock = socket.socket()
            with sock:
                sock.setblocking(False)
                r = self.loop.run_until_complete(client(sock, srv.addr))
                self.assertEqual(r, b'ii')

        self.assertTrue(ok)

    def test_socket_close_loop_and_close(self):
        class Abort(Exception):
            pass

        def srv_gen(sock):
            time.sleep(1.2)

        async def client(sock, addr):
            await self.loop.sock_connect(sock, addr)

            asyncio.ensure_future(self.loop.sock_recv(sock, 10),
                                  loop=self.loop)
            await asyncio.sleep(0.2, loop=self.loop)
            raise Abort

        with self.tcp_server(srv_gen) as srv:

            sock = socket.socket()
            with sock:
                sock.setblocking(False)

                c = client(sock, srv.addr)
                w = asyncio.wait_for(c, timeout=5.0, loop=self.loop)
                try:
                    sock = self.loop.run_until_complete(w)
                except Abort:
                    pass

                # `loop` still owns `sock`, so closing `sock` shouldn't
                # do anything.
                sock.close()
                self.assertNotEqual(sock.fileno(), -1)

                # `loop.close()` should io-decref all sockets that the
                # loop owns, including our `sock`.
                self.loop.close()
                self.assertEqual(sock.fileno(), -1)


class TestAIOSockets(_TestSockets, tb.AIOTestCase):
    pass
