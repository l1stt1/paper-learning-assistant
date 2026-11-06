"""Expected client disconnects do not turn normal navigation into server errors."""
import io
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import Handler


class FailingWriter(io.BytesIO):
    def __init__(self, error, fail_on_write):
        super().__init__()
        self.error = error
        self.fail_on_write = fail_on_write
        self.writes = 0

    def write(self, data):
        self.writes += 1
        if self.writes == self.fail_on_write:
            raise self.error
        return super().write(data)


def handler_for(writer):
    handler = Handler.__new__(Handler)
    handler.request_version = "HTTP/1.1"
    handler.requestline = "GET /health HTTP/1.1"
    handler.command = "GET"
    handler.wfile = writer
    return handler


class HttpDisconnectTests(unittest.TestCase):
    def test_client_disconnect_during_headers_or_body_is_quiet(self):
        for error_type in (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            for write_number in (1, 2):
                with self.subTest(error=error_type.__name__, write=write_number):
                    writer = FailingWriter(error_type("client disconnected"), write_number)
                    handler_for(writer).send_data({"status": "ready"})
                    self.assertEqual(writer.writes, write_number)

    def test_unrelated_io_and_program_errors_remain_visible(self):
        for error in (OSError("unrelated I/O failure"), ValueError("program error")):
            for write_number in (1, 2):
                with self.subTest(error=type(error).__name__, write=write_number):
                    writer = FailingWriter(error, write_number)
                    with self.assertRaises(type(error)) as caught:
                        handler_for(writer).send_data({"status": "ready"})
                    self.assertIs(caught.exception, error)

    def test_successful_response_keeps_headers_and_utf8_body(self):
        writer = io.BytesIO()
        handler_for(writer).send_data("准备就绪", "text/plain; charset=utf-8")
        headers, body = writer.getvalue().split(b"\r\n\r\n", 1)
        self.assertIn(b"Content-Type: text/plain; charset=utf-8", headers)
        self.assertIn(b"Cache-Control: no-store", headers)
        self.assertIn(b"Content-Length: " + str(len(body)).encode("ascii"), headers)
        self.assertEqual(body.decode("utf-8"), "准备就绪")


if __name__ == "__main__":
    unittest.main()
