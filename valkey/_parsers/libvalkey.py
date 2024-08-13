import asyncio
import socket
import sys
from typing import Callable, List, Optional, TypedDict, Union

if sys.version_info.major >= 3 and sys.version_info.minor >= 11:
    from asyncio import timeout as async_timeout
else:
    from async_timeout import timeout as async_timeout

from ..exceptions import ConnectionError, InvalidResponse, ValkeyError
from ..typing import EncodableT
from ..utils import LIBVALKEY_AVAILABLE
from .base import AsyncBaseParser, BaseParser
from .socket import (
    NONBLOCKING_EXCEPTION_ERROR_NUMBERS,
    NONBLOCKING_EXCEPTIONS,
    SENTINEL,
    SERVER_CLOSED_CONNECTION_ERROR,
)

# Used to signal that libvalkey-py does not have enough data to parse.
# Using `False` or `None` is not reliable, given that the parser can
# return `False` or `None` for legitimate reasons from RESP payloads.
NOT_ENOUGH_DATA = object()


class _LibvalkeyReaderArgs(TypedDict, total=False):
    protocolError: Callable[[str], Exception]
    replyError: Callable[[str], Exception]
    encoding: Optional[str]
    errors: Optional[str]


class _LibvalkeyParser(BaseParser):
    "Parser class for connections using libvalkey"

    def __init__(self, socket_read_size):
        if not LIBVALKEY_AVAILABLE:
            raise ValkeyError("libvalkey is not installed")
        self.socket_read_size = socket_read_size
        self._buffer = bytearray(socket_read_size)

    def __del__(self):
        try:
            self.on_disconnect()
        except Exception:
            pass

    def on_connect(self, connection, **kwargs):
        import libvalkey

        self._sock = connection._sock
        self._socket_timeout = connection.socket_timeout
        kwargs = {
            "protocolError": InvalidResponse,
            "replyError": self.parse_error,
            "errors": connection.encoder.encoding_errors,
            "notEnoughData": NOT_ENOUGH_DATA,
            "convertSetsToLists": True,
        }

        if connection.encoder.decode_responses:
            kwargs["encoding"] = connection.encoder.encoding
        self._reader = libvalkey.Reader(**kwargs)
        self._next_response = NOT_ENOUGH_DATA

    def on_disconnect(self):
        self._sock = None
        self._reader = None
        self._next_response = NOT_ENOUGH_DATA

    def can_read(self, timeout):
        if not self._reader:
            raise ConnectionError(SERVER_CLOSED_CONNECTION_ERROR)

        if self._next_response is NOT_ENOUGH_DATA:
            self._next_response = self._reader.gets()
            if self._next_response is NOT_ENOUGH_DATA:
                return self.read_from_socket(timeout=timeout, raise_on_timeout=False)
        return True

    def read_from_socket(self, timeout=SENTINEL, raise_on_timeout=True):
        sock = self._sock
        custom_timeout = timeout is not SENTINEL
        try:
            if custom_timeout:
                sock.settimeout(timeout)
            bufflen = self._sock.recv_into(self._buffer)
            if bufflen == 0:
                raise ConnectionError(SERVER_CLOSED_CONNECTION_ERROR)
            self._reader.feed(self._buffer, 0, bufflen)
            # data was read from the socket and added to the buffer.
            # return True to indicate that data was read.
            return True
        except socket.timeout:
            if raise_on_timeout:
                raise TimeoutError("Timeout reading from socket")
            return False
        except NONBLOCKING_EXCEPTIONS as ex:
            # if we're in nonblocking mode and the recv raises a
            # blocking error, simply return False indicating that
            # there's no data to be read. otherwise raise the
            # original exception.
            allowed = NONBLOCKING_EXCEPTION_ERROR_NUMBERS.get(ex.__class__, -1)
            if not raise_on_timeout and ex.errno == allowed:
                return False
            raise ConnectionError(f"Error while reading from socket: {ex.args}")
        finally:
            if custom_timeout:
                sock.settimeout(self._socket_timeout)

    def read_response(self, disable_decoding=False):
        if not self._reader:
            raise ConnectionError(SERVER_CLOSED_CONNECTION_ERROR)

        # _next_response might be cached from a can_read() call
        if self._next_response is not NOT_ENOUGH_DATA:
            response = self._next_response
            self._next_response = NOT_ENOUGH_DATA
            return response

        if disable_decoding:
            response = self._reader.gets(False)
        else:
            response = self._reader.gets()

        while response is NOT_ENOUGH_DATA:
            self.read_from_socket()
            if disable_decoding:
                response = self._reader.gets(False)
            else:
                response = self._reader.gets()
        # if the response is a ConnectionError or the response is a list and
        # the first item is a ConnectionError, raise it as something bad
        # happened
        if isinstance(response, ConnectionError):
            raise response
        elif (
            isinstance(response, list)
            and response
            and isinstance(response[0], ConnectionError)
        ):
            raise response[0]
        return response


class _AsyncLibvalkeyParser(AsyncBaseParser):
    """Async implementation of parser class for connections using libvalkey"""

    __slots__ = ("_reader",)

    def __init__(self, socket_read_size: int):
        if not LIBVALKEY_AVAILABLE:
            raise ValkeyError("libvalkey is not available.")
        super().__init__(socket_read_size=socket_read_size)
        self._reader = None

    def on_connect(self, connection):
        import libvalkey

        self._stream = connection._reader
        kwargs: _LibvalkeyReaderArgs = {
            "protocolError": InvalidResponse,
            "replyError": self.parse_error,
            "notEnoughData": NOT_ENOUGH_DATA,
            "convertSetsToLists": True,
        }
        if connection.encoder.decode_responses:
            kwargs["encoding"] = connection.encoder.encoding
            kwargs["errors"] = connection.encoder.encoding_errors

        self._reader = libvalkey.Reader(**kwargs)
        self._connected = True

    def on_disconnect(self):
        self._connected = False

    async def can_read_destructive(self):
        if not self._connected:
            raise ConnectionError(SERVER_CLOSED_CONNECTION_ERROR)
        if self._reader.gets() is not NOT_ENOUGH_DATA:
            return True
        try:
            async with async_timeout(0):
                return await self.read_from_socket()
        except asyncio.TimeoutError:
            return False

    async def read_from_socket(self):
        buffer = await self._stream.read(self._read_size)
        if not buffer or not isinstance(buffer, bytes):
            raise ConnectionError(SERVER_CLOSED_CONNECTION_ERROR) from None
        self._reader.feed(buffer)
        # data was read from the socket and added to the buffer.
        # return True to indicate that data was read.
        return True

    async def read_response(
        self, disable_decoding: bool = False
    ) -> Union[EncodableT, List[EncodableT]]:
        # If `on_disconnect()` has been called, prohibit any more reads
        # even if they could happen because data might be present.
        # We still allow reads in progress to finish
        if not self._connected:
            raise ConnectionError(SERVER_CLOSED_CONNECTION_ERROR) from None

        if disable_decoding:
            response = self._reader.gets(False)
        else:
            response = self._reader.gets()
        while response is NOT_ENOUGH_DATA:
            await self.read_from_socket()
            if disable_decoding:
                response = self._reader.gets(False)
            else:
                response = self._reader.gets()

        # if the response is a ConnectionError or the response is a list and
        # the first item is a ConnectionError, raise it as something bad
        # happened
        if isinstance(response, ConnectionError):
            raise response
        elif (
            isinstance(response, list)
            and response
            and isinstance(response[0], ConnectionError)
        ):
            raise response[0]
        return response