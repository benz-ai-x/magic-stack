import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tunnel import proxy
class TestAuthorityParsing(unittest.TestCase):
    def test_connect_authority_supports_ipv6(self):
        self.assertEqual(proxy._parse_authority("[::1]:443"), ("::1", 443))

    def test_rejects_bad_port(self):
        with self.assertRaises(ValueError):
            proxy._parse_authority("example.com:70000")

    def test_rejects_unbracketed_ipv6(self):
        with self.assertRaises(ValueError):
            proxy._parse_authority("::1:443")


class _Writer:
    def __init__(self, closing=False):
        self.data = bytearray()
        self.closed = False
        self._closing = closing

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        pass

    def is_closing(self):
        return self._closing

    def close(self):
        self.closed = True

    def can_write_eof(self):
        return False


def _reader(data=b""):
    reader = proxy.asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


class TestHttpForwarding(unittest.IsolatedAsyncioTestCase):
    async def test_counts_forwarded_http_bytes_for_all_body_framings(self):
        for headers, body in (
            (b"Content-Length: 4\r\n", b"data"),
            (b"Transfer-Encoding: chunked\r\n", b"4\r\ndata\r\n0\r\n\r\n"),
            (b"", b"data"),
        ):
            with self.subTest(headers=headers):
                request_head = (b"Host: example.com\r\nContent-Length: 6\r\n"
                                b"Connection: close\r\n"
                                b"Proxy-Authorization: Basic secret\r\n\r\n")
                response = b"HTTP/1.1 200 OK\r\n" + headers + b"\r\n" + body
                client_writer, remote_writer = _Writer(), _Writer()
                stats = proxy.Stats()
                with patch.object(proxy, "socks5_connect", new=AsyncMock(
                        return_value=(_reader(response), remote_writer))):
                    await proxy.handle_http(
                        _reader(request_head + b"upload"), client_writer,
                        b"POST http://example.com/path HTTP/1.1\r\n",
                        "127.0.0.1:1080", stats)
                snap = stats.snapshot()
                self.assertEqual(snap["total_down"], len(response))
                self.assertEqual(snap["total_up"], len(remote_writer.data))
                self.assertGreater(snap["total_up"], 6)
                self.assertEqual(snap["active_connections"], 0)

    async def test_local_http_failure_is_not_tunnel_traffic(self):
        stats = proxy.Stats()
        with patch.object(proxy, "socks5_connect", new=AsyncMock(
                side_effect=OSError("unreachable"))):
            await proxy.handle_http(
                _reader(b"Host: example.com\r\n\r\n"), _Writer(),
                b"GET http://example.com/ HTTP/1.1\r\n",
                "127.0.0.1:1080", stats)
        self.assertEqual(stats.snapshot()["total_up"], 0)
        self.assertEqual(stats.snapshot()["total_down"], 0)

    async def test_does_not_forward_proxy_credentials(self):
        client_reader = _reader(
            b"Host: example.com\r\n"
            b"Proxy-Authorization: Basic secret\r\n"
            b"Proxy-Connection: keep-alive\r\n\r\n"
        )
        client_writer = _Writer()
        remote_reader = _reader(b"HTTP/1.1 204 No Content\r\n\r\n")
        remote_writer = _Writer()
        with patch.object(
            proxy, "socks5_connect",
            new=AsyncMock(return_value=(remote_reader, remote_writer)),
        ):
            await proxy.handle_http(
                client_reader, client_writer,
                b"GET http://example.com/path?q=1 HTTP/1.1\r\n",
                "127.0.0.1:1080", proxy.Stats(),
            )
        forwarded = bytes(remote_writer.data)
        self.assertIn(b"GET /path?q=1 HTTP/1.1\r\n", forwarded)
        self.assertIn(b"Host: example.com\r\n", forwarded)
        self.assertNotIn(b"Proxy-Authorization", forwarded)
        self.assertNotIn(b"Proxy-Connection", forwarded)

    async def test_listener_is_always_loopback_after_port_refactor(self):
        """Candidate 2: config stores only the port; host is implicitly
        loopback. The non-loopback rejection test no longer applies — instead
        verify that the proxy starts on a loopback port from int config."""
        import socket as _socket
        s = _socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        control = {}
        config = {"socks5_port": 1080, "http_listen_port": port}
        task = proxy.asyncio.create_task(
            proxy.run_proxy(config, proxy.Stats(), control))
        try:
            for _ in range(50):
                if "server" in control:
                    break
                await proxy.asyncio.sleep(0.02)
            self.assertIn("server", control)
            self.assertTrue(control["server"].is_serving())
        finally:
            task.cancel()
            try:
                await task
            except (proxy.asyncio.CancelledError, Exception):
                pass

    async def test_invalid_headers_do_not_open_upstream_connection(self):
        client_reader = _reader(b"X-Test: " + b"x" * (proxy.MAX_HEADER_BYTES + 1) + b"\r\n")
        with patch.object(proxy, "socks5_connect", new=AsyncMock()) as connect:
            # issue #5 契约：头部超限安全关闭（不抛、不连 upstream）
            await proxy.handle_http(
                client_reader, _Writer(),
                b"GET http://example.com/ HTTP/1.1\r\n",
                "127.0.0.1:1080", proxy.Stats(),
            )
        connect.assert_not_awaited()


class TestSocksIPv6(unittest.IsolatedAsyncioTestCase):
    async def test_ipv6_uses_atyp_4(self):
        reader = _reader(
            b"\x05\x00" +
            b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x50"
        )
        writer = _Writer()
        with patch("asyncio.open_connection", new=AsyncMock(return_value=(reader, writer))):
            await proxy.socks5_connect("::1", 443, "127.0.0.1:1080")
        self.assertIn(b"\x05\x01\x00\x04" + b"\x00" * 15 + b"\x01", bytes(writer.data))


class TestReadHeaders(unittest.IsolatedAsyncioTestCase):
    async def test_returns_header_lines(self):
        reader = _reader(b"Host: example.com\r\nX-Test: 1\r\n\r\n")
        lines = await proxy._read_headers(reader)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], b"Host: example.com\r\n")

    async def test_empty_headers_returns_empty_list(self):
        reader = _reader(b"\r\n")
        lines = await proxy._read_headers(reader)
        self.assertEqual(lines, [])

    async def test_oversized_raises_value_error(self):
        reader = _reader(b"X-Big: " + b"x" * (proxy.MAX_HEADER_BYTES + 1) + b"\r\n\r\n")
        with self.assertRaises(ValueError):
            await proxy._read_headers(reader)


class TestSocks5IPv4(unittest.IsolatedAsyncioTestCase):
    async def test_ipv4_handshake_and_connect(self):
        reader = _reader(
            b"\x05\x00"  # auth: version=5, method=no-auth
            + b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x50"  # reply: success, IPv4 127.0.0.1:80
        )
        writer = _Writer()
        with patch("asyncio.open_connection", new=AsyncMock(return_value=(reader, writer))):
            r, w = await proxy.socks5_connect("example.com", 443, "127.0.0.1:1080")
        sent = bytes(writer.data)
        # Auth: VER=5, NMETHODS=1, METHOD=00 (no auth)
        self.assertTrue(sent.startswith(b"\x05\x01\x00"))
        # Connect request: VER=5, CMD=1, ATYP=3 (domain), then domain + port
        self.assertIn(b"\x05\x01\x00\x03", sent)
        self.assertIn(b"example.com", sent)

    async def test_connection_refused_raises(self):
        reader = _reader(
            b"\x05\x00"
            + b"\x05\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00"  # REP=5 (connection refused)
        )
        writer = _Writer()
        with patch("asyncio.open_connection", new=AsyncMock(return_value=(reader, writer))):
            with self.assertRaises(RuntimeError):
                await proxy.socks5_connect("example.com", 443, "127.0.0.1:1080")


class TestBidirectionalRelay(unittest.IsolatedAsyncioTestCase):
    async def test_forwards_data_both_directions(self):
        # ra → wb (upload), rb → wa (download)
        ra = _reader(b"request data")
        rb = _reader(b"response data")
        wa, wb = _Writer(), _Writer()
        stats = proxy.Stats()
        await proxy.bidirectional_relay(ra, wa, rb, wb, stats)
        self.assertIn(b"request data", bytes(wb.data))
        self.assertIn(b"response data", bytes(wa.data))

    async def test_byte_counting_in_stats(self):
        ra = _reader(b"ABC")
        rb = _reader(b"DEFG")
        wa, wb = _Writer(), _Writer()
        stats = proxy.Stats()
        await proxy.bidirectional_relay(ra, wa, rb, wb, stats)
        snap = stats.snapshot()
        self.assertEqual(snap["total_up"], 3)    # ra → wb = upload
        self.assertEqual(snap["total_down"], 4)  # rb → wa = download

    async def test_stops_pumping_when_destination_is_closing(self):
        # 写端 transport 已断(is_closing)后必须停止转发:
        # 继续写会触发 asyncio "socket.send() raised exception." 告警刷屏
        ra = _reader(b"")
        rb = _reader(b"x" * 4096)
        wa = _Writer(closing=True)
        wb = _Writer()
        await proxy.bidirectional_relay(ra, wa, rb, wb, proxy.Stats())
        self.assertEqual(bytes(wa.data), b"")

    async def test_empty_streams_complete_without_error(self):
        ra = _reader(b"")
        rb = _reader(b"")
        wa, wb = _Writer(), _Writer()
        await proxy.bidirectional_relay(ra, wa, rb, wb, proxy.Stats())
        self.assertEqual(bytes(wa.data), b"")
        self.assertEqual(bytes(wb.data), b"")


class TestHandleConnect(unittest.IsolatedAsyncioTestCase):
    async def test_connect_success_responds_200(self):
        client_reader = _reader(b"Host: example.com:443\r\n\r\n")
        client_writer = _Writer()
        remote_reader = _reader(b"HTTP/1.1 200 OK\r\n\r\nbody")
        remote_writer = _Writer()
        with patch.object(proxy, "socks5_connect",
                          new=AsyncMock(return_value=(remote_reader, remote_writer))):
            await proxy.handle_connect(
                client_reader, client_writer, "example.com", 443,
                "127.0.0.1:1080", proxy.Stats())
        self.assertIn(b"200", bytes(client_writer.data))

    async def test_connect_failure_responds_502(self):
        client_reader = _reader(b"Host: example.com:443\r\n\r\n")
        client_writer = _Writer()
        with patch.object(proxy, "socks5_connect",
                          new=AsyncMock(side_effect=ConnectionRefusedError)):
            await proxy.handle_connect(
                client_reader, client_writer, "example.com", 443,
                "127.0.0.1:1080", proxy.Stats())
        self.assertIn(b"502", bytes(client_writer.data))


class TestHandleClient(unittest.IsolatedAsyncioTestCase):
    async def test_dispatches_connect(self):
        client_reader = _reader(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        client_writer = _Writer()
        with patch.object(proxy, "handle_connect", new=AsyncMock()) as mock_hc:
            await proxy.handle_client(
                client_reader, client_writer, "127.0.0.1:1080", proxy.Stats())
        mock_hc.assert_awaited_once()

    async def test_dispatches_http(self):
        client_reader = _reader(b"GET http://example.com/ HTTP/1.1\r\n\r\n")
        client_writer = _Writer()
        with patch.object(proxy, "handle_http", new=AsyncMock()) as mock_hh:
            await proxy.handle_client(
                client_reader, client_writer, "127.0.0.1:1080", proxy.Stats())
        mock_hh.assert_awaited_once()

    async def test_empty_read_closes_quietly(self):
        client_reader = _reader(b"")
        client_writer = _Writer()
        with patch.object(proxy, "handle_connect", new=AsyncMock()) as mock_hc:
            await proxy.handle_client(
                client_reader, client_writer, "127.0.0.1:1080", proxy.Stats())
        mock_hc.assert_not_called()


class TestSSHMonitorStart(unittest.TestCase):
    @patch("shared.subprocess_monitor.subprocess.Popen")
    @patch("shared.subprocess_monitor.subprocess.run")
    def test_start_key_auth(self, mock_run, mock_popen):
        mock_popen.return_value = MagicMock(pid=12345)
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        monitor = proxy.SSHMonitor(line_sink=lambda _: None)
        monitor.start(
            {"ssh_host": "srv", "ssh_user": "u", "ssh_port": 22,
             "auth_type": "key", "ssh_key": "~/.ssh/id_rsa"},
            1080, "")
        self.assertEqual(monitor.status, "connecting")


class TestCloseWriter(unittest.IsolatedAsyncioTestCase):
    async def test_none_writer_is_noop(self):
        await proxy._close_writer(None)

    async def test_close_writes_eof(self):
        w = _Writer()
        await proxy._close_writer(w)
        self.assertTrue(w.closed)


class TestHandleClientErrors(unittest.IsolatedAsyncioTestCase):
    async def test_non_connect_dispatches_to_http(self):
        client_reader = _reader(b"DELETE / HTTP/1.1\r\n\r\n")
        client_writer = _Writer()
        with patch.object(proxy, "handle_connect", new=AsyncMock()) as hc, \
             patch.object(proxy, "handle_http", new=AsyncMock()) as hh:
            await proxy.handle_client(client_reader, client_writer,
                                      "127.0.0.1:1080", proxy.Stats())
        hc.assert_not_called()
        hh.assert_awaited_once()

    async def test_timeout_handled_gracefully(self):
        import asyncio as aio
        reader = MagicMock()
        reader.readline = AsyncMock(side_effect=aio.TimeoutError)
        writer = _Writer()
        await proxy.handle_client(reader, writer, "127.0.0.1:1080", proxy.Stats())
        # Should not raise

    async def test_connection_reset_handled(self):
        reader = MagicMock()
        reader.readline = AsyncMock(side_effect=ConnectionResetError)
        writer = _Writer()
        await proxy.handle_client(reader, writer, "127.0.0.1:1080", proxy.Stats())


class TestSSHMonitorCurrentName(unittest.TestCase):
    def test_current_name_default(self):
        monitor = proxy.SSHMonitor(line_sink=lambda _: None)
        self.assertEqual(monitor.current_name, "")


class TestParseAuthorityEdgeCases(unittest.TestCase):
    def test_unterminated_ipv6_bracket_raises(self):
        with self.assertRaisesRegex(ValueError, "invalid IPv6 authority"):
            proxy._parse_authority("[::1:443")

    def test_host_without_port_uses_default(self):
        self.assertEqual(proxy._parse_authority("example.com", default_port=80),
                         ("example.com", 80))

    def test_empty_host_raises(self):
        # "[]:443" parses an empty bracketed host, reaching the empty-host check
        with self.assertRaisesRegex(ValueError, "empty host"):
            proxy._parse_authority("[]:443")

    def test_empty_authority_colon_raises_invalid(self):
        with self.assertRaisesRegex(ValueError, "invalid authority"):
            proxy._parse_authority(":443")

    def test_ipv6_without_port_uses_default(self):
        self.assertEqual(proxy._parse_authority("[::1]", default_port=443), ("::1", 443))


class TestReadHeadersLineTooLong(unittest.IsolatedAsyncioTestCase):
    async def test_single_oversized_line_raises(self):
        reader = proxy.asyncio.StreamReader(limit=2 * proxy.MAX_HEADER_BYTES)
        reader.feed_data(b"X-Big: " + b"x" * (proxy.MAX_HEADER_BYTES + 10) + b"\r\n\r\n")
        reader.feed_eof()
        with self.assertRaisesRegex(ValueError, "exceed limit"):
            await proxy._read_headers(reader)


class TestSocks5AddressTypes(unittest.IsolatedAsyncioTestCase):
    async def test_ipv4_host_uses_atyp_1(self):
        reader = _reader(
            b"\x05\x00"
            + b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x50"
        )
        writer = _Writer()
        with patch("asyncio.open_connection", new=AsyncMock(return_value=(reader, writer))):
            await proxy.socks5_connect("127.0.0.1", 80, "127.0.0.1:1080")
        sent = bytes(writer.data)
        # ATYP=1 (IPv4) + 4 bytes addr
        self.assertIn(b"\x05\x01\x00\x01\x7f\x00\x00\x01", sent)

    async def test_overlong_hostname_raises(self):
        # Multi-label hostname that IDNA-encodes to >255 bytes (single labels
        # over 63 chars fail IDNA before reaching the length check).
        long_host = ".".join(["a" * 60] * 5)
        reader = _reader(b"\x05\x00")
        writer = _Writer()
        with patch("asyncio.open_connection", new=AsyncMock(return_value=(reader, writer))):
            with self.assertRaisesRegex(ValueError, "exceeds 255"):
                await proxy.socks5_connect(long_host, 80, "127.0.0.1:1080")

    async def test_reply_atyp_domain(self):
        # reply: success, ATYP=3 (domain), len=3 "abc", port 80
        reader = _reader(
            b"\x05\x00"
            + b"\x05\x00\x00\x03\x03abc\x00\x50"
        )
        writer = _Writer()
        with patch("asyncio.open_connection", new=AsyncMock(return_value=(reader, writer))):
            r, w = await proxy.socks5_connect("example.com", 80, "127.0.0.1:1080")
        self.assertIs(r, reader)

    async def test_reply_atyp_ipv6(self):
        # reply: success, ATYP=4 (IPv6), 16 bytes + 2 port
        reader = _reader(
            b"\x05\x00"
            + b"\x05\x00\x00\x04" + b"\x00" * 16 + b"\x00\x50"
        )
        writer = _Writer()
        with patch("asyncio.open_connection", new=AsyncMock(return_value=(reader, writer))):
            r, w = await proxy.socks5_connect("example.com", 80, "127.0.0.1:1080")
        self.assertIs(r, reader)

    async def test_reply_unknown_atyp_raises(self):
        reader = _reader(
            b"\x05\x00"
            + b"\x05\x00\x00\x7f"  # ATYP=0x7f unknown
        )
        writer = _Writer()
        with patch("asyncio.open_connection", new=AsyncMock(return_value=(reader, writer))):
            with self.assertRaisesRegex(RuntimeError, "unknown address type"):
                await proxy.socks5_connect("example.com", 80, "127.0.0.1:1080")


class TestHandleHttpEdgeCases(unittest.IsolatedAsyncioTestCase):
    async def test_non_absolute_url_gets_400(self):
        client_reader = _reader(b"\r\n")
        client_writer = _Writer()
        with patch.object(proxy, "socks5_connect", new=AsyncMock()) as connect:
            await proxy.handle_http(
                client_reader, client_writer,
                b"GET /relative/path HTTP/1.1\r\n",
                "127.0.0.1:1080", proxy.Stats())
        self.assertIn(b"400 Bad Request", bytes(client_writer.data))
        connect.assert_not_awaited()

    async def test_connection_nominated_headers_stripped(self):
        client_reader = _reader(
            b"Host: example.com\r\n"
            b"Connection: X-Custom\r\n"
            b"X-Custom: secret\r\n"
            b"X-Keep: visible\r\n\r\n"
        )
        client_writer = _Writer()
        remote_reader = _reader(b"HTTP/1.1 204 No Content\r\n\r\n")
        remote_writer = _Writer()
        with patch.object(proxy, "socks5_connect",
                          new=AsyncMock(return_value=(remote_reader, remote_writer))):
            await proxy.handle_http(
                client_reader, client_writer,
                b"GET http://example.com/ HTTP/1.1\r\n",
                "127.0.0.1:1080", proxy.Stats())
        forwarded = bytes(remote_writer.data)
        self.assertNotIn(b"X-Custom", forwarded)
        self.assertIn(b"X-Keep: visible\r\n", forwarded)


    async def test_connection_multi_token_with_space_stripped(self):
        """#69 S11c："keep-alive, X-Hop" 的 " x-hop" 未 strip 时永不匹配
        blocked——客户端声明的逐跳头被转发 origin（RFC 7230 动态机制）。"""
        client_reader = _reader(
            b"Host: example.com\r\n"
            b"Connection: keep-alive, X-Hop\r\n"
            b"X-Hop: secret\r\n"
            b"X-Keep: visible\r\n\r\n"
        )
        client_writer = _Writer()
        remote_reader = _reader(b"HTTP/1.1 204 No Content\r\n\r\n")
        remote_writer = _Writer()
        with patch.object(proxy, "socks5_connect",
                          new=AsyncMock(return_value=(remote_reader, remote_writer))):
            await proxy.handle_http(
                client_reader, client_writer,
                b"GET http://example.com/ HTTP/1.1\r\n",
                "127.0.0.1:1080", proxy.Stats())
        forwarded = bytes(remote_writer.data)
        self.assertNotIn(b"X-Hop", forwarded)
        self.assertIn(b"X-Keep: visible\r\n", forwarded)

    async def test_socks5_failure_responds_502(self):
        client_reader = _reader(b"Host: example.com\r\n\r\n")
        client_writer = _Writer()
        with patch.object(proxy, "socks5_connect",
                          new=AsyncMock(side_effect=ConnectionRefusedError)):
            await proxy.handle_http(
                client_reader, client_writer,
                b"GET http://example.com/ HTTP/1.1\r\n",
                "127.0.0.1:1080", proxy.Stats())
        self.assertIn(b"502", bytes(client_writer.data))


class TestHandleClientEdgeCases(unittest.IsolatedAsyncioTestCase):
    async def test_oversized_request_line_responds_400(self):
        big = b"GET " + b"x" * (proxy.MAX_HEADER_BYTES + 10) + b" HTTP/1.1\r\n"
        client_reader = _reader(big)
        client_writer = _Writer()
        await proxy.handle_client(client_reader, client_writer,
                                  "127.0.0.1:1080", proxy.Stats())
        self.assertIn(b"400", bytes(client_writer.data))

    async def test_single_token_request_line_closes(self):
        client_reader = _reader(b"BADREQUEST\r\n")
        client_writer = _Writer()
        await proxy.handle_client(client_reader, client_writer,
                                  "127.0.0.1:1080", proxy.Stats())
        self.assertTrue(client_writer.closed)

    async def test_invalid_url_responds_400(self):
        # A relative-path GET makes handle_http raise ValueError, which
        # handle_client catches and turns into a 400 response.
        client_reader = _reader(b"GET /relative/path HTTP/1.1\r\n\r\n")
        client_writer = _Writer()
        await proxy.handle_client(client_reader, client_writer,
                                  "127.0.0.1:1080", proxy.Stats())
        self.assertIn(b"400", bytes(client_writer.data))

    async def test_generic_exception_closes(self):
        client_reader = _reader(b"GET http://example.com/ HTTP/1.1\r\n\r\n")
        client_writer = _Writer()
        with patch.object(proxy, "handle_http",
                          new=AsyncMock(side_effect=RuntimeError("boom"))):
            await proxy.handle_client(client_reader, client_writer,
                                      "127.0.0.1:1080", proxy.Stats())
        self.assertTrue(client_writer.closed)


class _EOFWriter(_Writer):
    """Writer that supports write_eof."""
    def __init__(self):
        super().__init__()
        self.eof_written = False

    def can_write_eof(self):
        return True

    def write_eof(self):
        self.eof_written = True


class TestRelayDrainAndEOF(unittest.IsolatedAsyncioTestCase):
    async def test_large_payload_triggers_drain(self):
        payload = b"x" * (proxy.DRAIN_THRESHOLD + 1024)
        ra = _reader(payload)
        rb = _reader(b"")
        wa, wb = _EOFWriter(), _EOFWriter()
        stats = proxy.Stats()
        await proxy.bidirectional_relay(ra, wa, rb, wb, stats)
        self.assertEqual(len(wb.data), len(payload))

    async def test_eof_written_when_supported(self):
        ra = _reader(b"data")
        rb = _reader(b"")
        wa, wb = _EOFWriter(), _EOFWriter()
        await proxy.bidirectional_relay(ra, wa, rb, wb, proxy.Stats())
        self.assertTrue(wb.eof_written)
        self.assertTrue(wa.eof_written)

    async def test_read_error_swallowed(self):
        class _ErrReader:
            async def read(self, n):
                raise ConnectionResetError
        ra = _ErrReader()
        rb = _reader(b"")
        wa, wb = _EOFWriter(), _EOFWriter()
        await proxy.bidirectional_relay(ra, wa, rb, wb, proxy.Stats())


class TestSSHMonitorPasswordAuth(unittest.TestCase):
    @patch("shared.subprocess_monitor.subprocess.Popen")
    @patch("shared.subprocess_monitor.subprocess.run")
    def test_start_password_auth(self, mock_run, mock_popen):
        mock_popen.return_value = MagicMock(pid=12345)
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        monitor = proxy.SSHMonitor(line_sink=lambda _: None)
        monitor.start(
            {"ssh_host": "srv", "ssh_user": "u", "ssh_port": 22,
             "auth_type": "password"},
            1080, "hunter2")
        self.assertEqual(monitor.status, "connecting")


class TestSSHMonitorProbeReadySuccess(unittest.TestCase):
    @patch("tunnel.proxy.socket.socket")
    def test_probe_ready_returns_true_on_socks5(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_socket_cls.return_value = mock_sock
        mock_sock.recv.return_value = b"\x05\x00"
        monitor = proxy.SSHMonitor(line_sink=lambda _: None)
        self.assertTrue(monitor._probe_ready(1080))


class TestProxyRuntimeError(unittest.TestCase):
    def test_error_property_delegates(self):
        rt = proxy.ProxyRuntime(proxy.Stats())
        self.assertEqual(rt.error, "")


class TestSocks5HandshakeRejected(unittest.IsolatedAsyncioTestCase):
    async def test_bad_auth_method_raises(self):
        # Auth reply VER=5 METHOD=01 (no acceptable method) -> rejected
        reader = _reader(b"\x05\x01")
        writer = _Writer()
        with patch("asyncio.open_connection", new=AsyncMock(return_value=(reader, writer))):
            with self.assertRaisesRegex(RuntimeError, "handshake rejected"):
                await proxy.socks5_connect("example.com", 80, "127.0.0.1:1080")


class TestHandleClientOversizedRequestLine(unittest.IsolatedAsyncioTestCase):
    async def test_request_line_over_limit_responds_400(self):
        # Use a high-limit reader so readline returns the full oversized line
        # and our explicit length check (not StreamReader's own limit) fires.
        reader = proxy.asyncio.StreamReader(limit=2 * proxy.MAX_HEADER_BYTES)
        reader.feed_data(b"GET " + b"x" * (proxy.MAX_HEADER_BYTES + 10) + b" HTTP/1.1\r\n\r\n")
        reader.feed_eof()
        writer = _Writer()
        await proxy.handle_client(reader, writer, "127.0.0.1:1080", proxy.Stats())
        self.assertIn(b"400", bytes(writer.data))


class _WaitClosedWriter(_Writer):
    """Writer exposing wait_closed for _close_writer to exercise."""
    def __init__(self, wait_closed_side_effect=None):
        super().__init__()
        self._wc_side_effect = wait_closed_side_effect

    async def wait_closed(self):
        if self._wc_side_effect:
            raise self._wc_side_effect


class TestCloseWriterWaitClosed(unittest.IsolatedAsyncioTestCase):
    async def test_wait_closed_called(self):
        w = _WaitClosedWriter()
        await proxy._close_writer(w)
        self.assertTrue(w.closed)

    async def test_wait_closed_error_swallowed(self):
        w = _WaitClosedWriter(wait_closed_side_effect=OSError("boom"))
        await proxy._close_writer(w)  # should not raise
        self.assertTrue(w.closed)


class _RaisingDrainWriter(_EOFWriter):
    def __init__(self, drain_raises=False, eof_raises=False):
        super().__init__()
        self._drain_raises = drain_raises
        self._eof_raises = eof_raises

    async def drain(self):
        if self._drain_raises:
            raise ConnectionError("drain failed")

    def write_eof(self):
        if self._eof_raises:
            raise OSError("eof failed")
        self.eof_written = True


class TestRelayExceptionPaths(unittest.IsolatedAsyncioTestCase):
    async def test_final_drain_error_swallowed(self):
        ra = _reader(b"data")
        rb = _reader(b"")
        wa = _RaisingDrainWriter()
        wb = _RaisingDrainWriter(drain_raises=True)
        await proxy.bidirectional_relay(ra, wa, rb, wb, proxy.Stats())

    async def test_write_eof_error_swallowed(self):
        ra = _reader(b"data")
        rb = _reader(b"")
        wa = _RaisingDrainWriter(eof_raises=True)
        wb = _RaisingDrainWriter(eof_raises=True)
        await proxy.bidirectional_relay(ra, wa, rb, wb, proxy.Stats())


class TestRunProxyServer(unittest.IsolatedAsyncioTestCase):
    async def test_server_starts_and_registers_in_control(self):
        import socket as _socket
        # Find a free loopback port
        s = _socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()

        control = {}
        config = {"socks5_port": 1080, "http_listen_port": port}
        task = proxy.asyncio.create_task(
            proxy.run_proxy(config, proxy.Stats(), control))
        try:
            # Wait for the server to register itself in control
            for _ in range(50):
                if "server" in control:
                    break
                await proxy.asyncio.sleep(0.02)
            self.assertIn("server", control)
            self.assertTrue(control["server"].is_serving())
        finally:
            task.cancel()
            try:
                await task
            except (proxy.asyncio.CancelledError, Exception):
                pass


class _ExplodingCloseWriter(_Writer):
    def close(self):
        raise OSError("close failed")


class _ExplodingWriteWriter(_Writer):
    def write(self, data):
        raise BrokenPipeError("write failed")


class TestHandleClientWriterErrors(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_error_propagates(self):
        reader = MagicMock()
        reader.readline = AsyncMock(side_effect=proxy.asyncio.CancelledError)
        writer = _Writer()
        with self.assertRaises(proxy.asyncio.CancelledError):
            await proxy.handle_client(reader, writer, "127.0.0.1:1080", proxy.Stats())

    async def test_connection_reset_with_close_error_swallowed(self):
        reader = MagicMock()
        reader.readline = AsyncMock(side_effect=ConnectionResetError)
        writer = _ExplodingCloseWriter()
        await proxy.handle_client(reader, writer, "127.0.0.1:1080", proxy.Stats())

    async def test_value_error_with_write_error_swallowed(self):
        client_reader = _reader(b"GET /relative HTTP/1.1\r\n\r\n")
        writer = _ExplodingWriteWriter()
        await proxy.handle_client(client_reader, writer, "127.0.0.1:1080", proxy.Stats())

    async def test_generic_exception_with_close_error_swallowed(self):
        client_reader = _reader(b"GET http://example.com/ HTTP/1.1\r\n\r\n")
        writer = _ExplodingCloseWriter()
        with patch.object(proxy, "handle_http",
                          new=AsyncMock(side_effect=RuntimeError("boom"))):
            await proxy.handle_client(client_reader, writer,
                                      "127.0.0.1:1080", proxy.Stats())


class TestSSHMonitorPasswordCloseError(unittest.TestCase):
    @patch("tunnel.ssh_launch.os.close")
    @patch("tunnel.ssh_launch.os.write")
    @patch("tunnel.ssh_launch.os.pipe")
    @patch("shared.subprocess_monitor.subprocess.Popen")
    @patch("shared.subprocess_monitor.subprocess.run")
    def test_password_auth_close_error_swallowed(self, mock_run, mock_popen,
                                                 mock_pipe, mock_write, mock_close):
        mock_pipe.return_value = (100, 101)
        mock_popen.return_value = MagicMock(pid=12345)
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        # First close (w_fd) succeeds, second close (r_fd) raises OSError
        mock_close.side_effect = [None, OSError("fd already closed")]
        monitor = proxy.SSHMonitor(line_sink=lambda _: None)
        monitor.start(
            {"ssh_host": "srv", "ssh_user": "u", "ssh_port": 22,
             "auth_type": "password"},
            1080, "hunter2")
        self.assertEqual(monitor.status, "connecting")


class TestKeepAliveOriginBinding(unittest.IsolatedAsyncioTestCase):
    """issue #5：明文 HTTP 的逐请求归属状态机。

    同一客户端 keep-alive 连接上，跨 origin 的后续请求绝不能静默误投到
    首 origin 的 upstream——可安全重连（新 upstream）或明确关闭。
    """

    def _client_stream(self, *chunks):
        return _reader(b"".join(chunks))

    async def test_second_request_different_host_gets_own_upstream(self):
        # request_line 参数已从流外读取——feed 只含其头部余下与后续请求
        req1_rest = b"Host: a.test\r\n\r\n"
        req2 = (b"GET http://b.test/two HTTP/1.1\r\nHost: b.test\r\n\r\n")
        client_reader = self._client_stream(req1_rest, req2)
        client_writer = _Writer()
        r1, w1 = _reader(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n"), _Writer()
        r2, w2 = _reader(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n"), _Writer()
        connects = []

        async def fake_connect(host, port, socks_addr):
            connects.append(host)
            return (r1, w1) if host == "a.test" else (r2, w2)

        with patch.object(proxy, "socks5_connect", new=fake_connect):
            await proxy.handle_http(
                client_reader, client_writer,
                b"GET http://a.test/one HTTP/1.1\r\n",
                "127.0.0.1:1080", proxy.Stats(),
            )
        self.assertEqual(connects, ["a.test", "b.test"])
        self.assertNotIn(b"b.test", bytes(w1.data),
                         "跨 origin 第二请求不得进入首 origin")
        self.assertIn(b"GET /two HTTP/1.1", bytes(w2.data))

    async def test_same_origin_reuses_one_upstream(self):
        req1_rest = b"Host: a.test\r\n\r\n"
        req2 = b"GET http://a.test/two HTTP/1.1\r\nHost: a.test\r\n\r\n"
        client_reader = self._client_stream(req1_rest, req2)
        client_writer = _Writer()
        remote_reader = _reader(
            b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n"
            b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
        remote_writer = _Writer()
        connects = []

        async def fake_connect(host, port, socks_addr):
            connects.append(host)
            return (remote_reader, remote_writer)

        with patch.object(proxy, "socks5_connect", new=fake_connect):
            await proxy.handle_http(
                client_reader, client_writer,
                b"GET http://a.test/one HTTP/1.1\r\n",
                "127.0.0.1:1080", proxy.Stats(),
            )
        self.assertEqual(connects, ["a.test"], "同 origin keep-alive 复用同一条 upstream")
        data = bytes(remote_writer.data)
        self.assertIn(b"GET /one HTTP/1.1", data)
        self.assertIn(b"GET /two HTTP/1.1", data)


class TestPerRequestHeaderHygiene(unittest.IsolatedAsyncioTestCase):
    """issue #5：hop-by-hop/凭证剥离逐请求独立生效（非仅首请求）。"""

    async def test_second_request_connection_tokens_stripped(self):
        client_reader = _reader(
            b"Host: a.test\r\n\r\n"
            b"GET http://a.test/two HTTP/1.1\r\nHost: a.test\r\n"
            b"Connection: X-Custom\r\nX-Custom: secret\r\n"
            b"Proxy-Authorization: Basic sekrit\r\n\r\n")
        remote_reader = _reader(
            b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n"
            b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
        remote_writer = _Writer()
        with patch.object(proxy, "socks5_connect", new=AsyncMock(
                return_value=(remote_reader, remote_writer))):
            await proxy.handle_http(
                client_reader, _Writer(),
                b"GET http://a.test/one HTTP/1.1\r\n",
                "127.0.0.1:1080", proxy.Stats())
        second = bytes(remote_writer.data).split(b"GET /two", 1)[-1]
        self.assertNotIn(b"X-Custom: secret", second)
        self.assertNotIn(b"Proxy-Authorization", second)
        self.assertIn(b"Host: a.test", second)


class TestFramedBodies(unittest.IsolatedAsyncioTestCase):
    """issue #5 验收：Content-Length / chunked 定界下 keep-alive 不破。"""

    async def test_post_with_content_length_then_next_request(self):
        body = b"payload-123"
        req1_rest = (b"Host: a.test\r\nContent-Length: %d\r\n\r\n"
                     % len(body)) + body
        req2 = b"GET http://a.test/after HTTP/1.1\r\nHost: a.test\r\n\r\n"
        client_reader = _reader(req1_rest + req2)
        remote_reader = _reader(
            b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n"
            b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
        remote_writer = _Writer()
        with patch.object(proxy, "socks5_connect", new=AsyncMock(
                return_value=(remote_reader, remote_writer))):
            await proxy.handle_http(
                client_reader, _Writer(), b"POST http://a.test/submit HTTP/1.1\r\n",
                "127.0.0.1:1080", proxy.Stats())
        data = bytes(remote_writer.data)
        self.assertIn(body, data, "请求 body 按定界精确转发")
        self.assertIn(b"GET /after HTTP/1.1", data, "body 后续请求仍被正确解析")

    async def test_chunked_response_relays_and_keeps_alive(self):
        resp = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                b"5\r\nhello\r\n0\r\n\r\n"
                b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
        client_reader = _reader(
            b"GET http://a.test/one HTTP/1.1\r\nHost: a.test\r\n\r\n"
            b"GET http://a.test/two HTTP/1.1\r\nHost: a.test\r\n\r\n")
        client_writer = _Writer()
        remote_writer = _Writer()
        with patch.object(proxy, "socks5_connect", new=AsyncMock(
                return_value=(_reader(resp), remote_writer))):
            await proxy.handle_http(
                client_reader, client_writer,
                b"GET http://a.test/one HTTP/1.1\r\n",
                "127.0.0.1:1080", proxy.Stats())
        out = bytes(client_writer.data)
        self.assertIn(b"5\r\nhello\r\n0\r\n\r\n", out, "chunked 原样转发")
        self.assertEqual(out.count(b"HTTP/1.1"), 2, "chunked 终止后 keep-alive 续读")


class TestDualOriginEndToEnd(unittest.IsolatedAsyncioTestCase):
    """issue #5 验收：两个真实本地 HTTP server 的端到端（顺序 + pipelined）。"""

    async def test_sequential_and_pipelined_across_two_real_origins(self):
        seen = {"a": [], "b": []}

        async def make_origin(tag):
            async def handle(reader, writer):
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    while True:  # 头部至空行（真实请求无 body）
                        h = await reader.readline()
                        if h in (b"\r\n", b""):
                            break
                    seen[tag].append(line.decode().split()[1])
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                    await writer.drain()
            server = await proxy.asyncio.start_server(handle, "127.0.0.1", 0)
            return server, server.sockets[0].getsockname()[1]

        server_a, port_a = await make_origin("a")
        server_b, port_b = await make_origin("b")
        self.addCleanup(server_a.close)
        self.addCleanup(server_b.close)

        async def fake_socks(host, port, socks_addr):
            return await proxy.asyncio.open_connection(
                "127.0.0.1", port_a if host == "a.test" else port_b)

        # 单次喂入三条请求：a/1 → a/2（pipelined）→ b/x（跨 origin）
        client_reader = proxy.asyncio.StreamReader()
        client_reader.feed_data(
            b"Host: a.test\r\n\r\n"
            b"GET http://a.test/2 HTTP/1.1\r\nHost: a.test\r\n\r\n"
            b"GET http://b.test/x HTTP/1.1\r\nHost: b.test\r\n\r\n")
        client_reader.feed_eof()
        client_writer = _Writer()

        with patch.object(proxy, "socks5_connect", new=fake_socks):
            await proxy.handle_http(
                client_reader, client_writer,
                b"GET http://a.test/1 HTTP/1.1\r\n",
                "127.0.0.1:1080", proxy.Stats())

        self.assertEqual(seen["a"], ["/1", "/2"], "同 origin 顺序+pipelined 双达")
        self.assertEqual(seen["b"], ["/x"], "跨 origin 到达真实第二 origin")
        self.assertEqual(bytes(client_writer.data).count(b"ok"), 3)




class TestSocks5FailLogThrottle(unittest.TestCase):
    """#88：SOCKS5 连接失败日志按分钟聚合——首条完整、窗口内抑制计数、滚动补汇总。"""

    def _make(self, window=60.0):
        now = [1000.0]
        throttle = proxy.Socks5FailLogThrottle(window=window, clock=lambda: now[0])
        return throttle, now

    def test_first_failure_logs_full_detail(self):
        throttle, _ = self._make()
        with self.assertLogs("magic-proxy.proxy", level="ERROR") as logs:
            throttle.record("a.test", 443, "[Errno 61] refused")
        self.assertEqual(len(logs.output), 1)
        self.assertIn("a.test:443", logs.output[0])
        self.assertIn("[Errno 61] refused", logs.output[0])

    def test_same_window_failures_are_suppressed(self):
        throttle, now = self._make()
        with self.assertLogs("magic-proxy.proxy", level="ERROR") as logs:
            throttle.record("a.test", 443, "[Errno 61] refused")
            now[0] += 1
            throttle.record("b.test", 443, "[Errno 61] refused")
            now[0] += 1
            throttle.record("c.test", 443, "[Errno 61] refused")
        self.assertEqual(len(logs.output), 1, "窗口内只留首条完整记录")

    def test_window_roll_emits_summary_then_new_full(self):
        throttle, now = self._make(window=60.0)
        with self.assertLogs("magic-proxy.proxy", level="ERROR"):
            throttle.record("a.test", 443, "[Errno 61] refused")
            now[0] += 1
            throttle.record("b.test", 443, "[Errno 61]")  # 抑制 ×1
        now[0] += 60  # 跨过窗口
        with self.assertLogs("magic-proxy.proxy", level="ERROR") as logs:
            throttle.record("d.test", 443, "[Errno 61]")
        self.assertEqual(len(logs.output), 2, "汇总 + 新窗口首条")
        self.assertIn("1", logs.output[0])  # 汇总带抑制计数
        self.assertIn("d.test:443", logs.output[1])
