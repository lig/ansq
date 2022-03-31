import abc
import asyncio
import logging
import warnings
from asyncio.events import AbstractEventLoop
from asyncio.streams import StreamReader, StreamWriter
from collections import deque
from datetime import datetime
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Deque,
    Mapping,
    Optional,
    Tuple,
    Union,
)

import attr

from ansq.typedefs import TCPResponse

if TYPE_CHECKING:
    from ansq.tcp.types import ConnectionStatus, NSQMessage, NSQMessageSchema


@attr.define(auto_attribs=True, kw_only=True)
class ConnectionFeatures:
    deflate: bool = False
    deflate_level: int = 6
    feature_negotiation: bool = True
    heartbeat_interval: int = 30000
    sample_rate: int = 0
    snappy: bool = False
    tls_v1: bool = False


@attr.define(auto_attribs=True, kw_only=True)
class ConnectionOptions:
    message_queue: Optional["asyncio.Queue[Optional[NSQMessage]]"] = None
    # TODO: define more strict type for `on_message`
    on_message: Optional[Callable] = None
    # TODO: define more strict type for `on_exception`
    on_exception: Optional[Callable] = None
    on_close: Optional[Callable[["TCPConnection"], None]] = None
    loop: Optional[AbstractEventLoop] = None
    auto_reconnect: bool = True
    features: ConnectionFeatures = ConnectionFeatures()
    debug: bool = False
    logger: Optional[logging.Logger] = None

    def _update(self, **kwargs: Any) -> None:
        options = set(attr.fields_dict(type(self)))
        features = set(attr.fields_dict(type(self.features)))

        for param, value in kwargs.items():
            if param in options:
                setattr(self, param, value)
                break

            if param in features:
                setattr(self.features, param, value)
                break

            raise TypeError(f"got an unexpected keyword argument: '{param}'")


class TCPConnection(abc.ABC):
    instances_count = 0

    def __init__(
        self,
        host: str = "localhost",
        port: int = 4150,
        *,
        connection_options: ConnectionOptions = ConnectionOptions(),
        **kwargs: Mapping[str, Any],
    ):
        from ansq.tcp.protocol import Reader
        from ansq.tcp.types import ConnectionStatus
        from ansq.utils import get_logger

        if kwargs:
            warnings.warn(
                message=(
                    "Passing connection options to `TCPConnection` using keyword "
                    "arguments is deprecated: use `ConnectionOptions` structure instead"
                ),
                category=DeprecationWarning,
            )
            connection_options._update(**kwargs)

        self._options: ConnectionOptions = connection_options

        self.instance_number = self.__class__.instances_count
        self.__class__.instances_count += 1

        self._host, self._port = host, port
        self._loop: AbstractEventLoop = self._options.loop or asyncio.get_event_loop()
        self._debug = self._options.debug
        self.logger = self._options.logger or get_logger(
            self._debug, f"{self._host}:{self._port}.{self.instance_number}"
        )

        self._message_queue: "asyncio.Queue[Optional[NSQMessage]]" = (
            self._options.message_queue or asyncio.Queue()
        )
        self._status: ConnectionStatus = ConnectionStatus.INIT
        self._reader: Optional[StreamReader] = None
        self._writer: Optional[StreamWriter] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._auto_reconnect = self._options.auto_reconnect

        self._parser = Reader()

        self._last_message_time: Optional[datetime] = None
        # Next queue is used for nsq commands
        self._cmd_waiters: Deque[
            Tuple[asyncio.Future, Optional[Callable[[TCPResponse], Any]]]
        ] = deque()
        # Mark connection in upgrading state to ssl socket
        self._is_upgrading = False
        # Number of received but not acknowledged or req messages
        self._in_flight = 0
        self._secret: Optional[str] = None
        self._is_auth_required = False
        self._is_authorized = False

        # Handlers
        self._on_message = self._options.on_message
        self._on_exception = self._options.on_exception
        self._on_close = self._options.on_close

        # Reader setup
        self._topic: Optional[str] = None
        self._channel: Optional[str] = None
        self.rdy_messages_count: int = 1
        self._is_subscribed = False

    def __repr__(self) -> str:
        return "<{class_name}: endpoint={endpoint}, status={status}>".format(
            class_name=self.__class__.__name__,
            endpoint=self.endpoint,
            status=self.status,
        )

    @property
    def id(self) -> str:
        return f"{self._host}:{self._port}"

    @property
    def status(self) -> "ConnectionStatus":
        return self._status

    @property
    def endpoint(self) -> str:
        return f"tcp://{self._host}:{self._port}"

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def message_queue(self) -> "asyncio.Queue[Optional[NSQMessage]]":
        return self._message_queue

    @property
    def last_message(self) -> Optional[datetime]:
        return self._last_message_time

    @property
    def is_subscribed(self) -> bool:
        return self._is_subscribed

    @property
    def subscribed_topic(self) -> Optional[str]:
        return self._topic

    @property
    def subscribed_channel(self) -> Optional[str]:
        return self._channel

    @property
    def is_auth_required(self) -> bool:
        return self._is_auth_required

    @property
    def is_authorized(self) -> bool:
        return self._is_authorized

    @property
    def is_connected(self) -> bool:
        """Return true if connection is connected."""
        return self.status.is_connected

    @property
    def is_closed(self) -> bool:
        """True if connection is closed or closing."""
        return self.status.is_closed or self._status.is_closing

    @abc.abstractmethod
    async def connect(self) -> bool:
        raise NotImplementedError()

    @abc.abstractmethod
    async def reconnect(self) -> bool:
        raise NotImplementedError()

    async def close(self) -> None:
        """Cleanly close your connection (no more messages are sent)"""
        await self._do_close()

    async def cls(self) -> None:
        """Alias command for ``close()``."""
        await self.close()

    @abc.abstractmethod
    async def _do_close(
        self,
        exception: Optional[Exception] = None,
        change_status: bool = True,
        silent: bool = False,
    ) -> None:
        raise NotImplementedError()

    @abc.abstractmethod
    async def execute(
        self,
        command: Union[str, bytes],
        *args: Any,
        data: Optional[Any] = None,
        callback: Callable[[TCPResponse], Any] = None,
    ) -> TCPResponse:
        raise NotImplementedError()

    @abc.abstractmethod
    async def identify(
        self,
        config: Optional[Union[dict, str]] = None,
        *,
        features: Optional[ConnectionFeatures] = None,
        **kwargs: Any,
    ) -> TCPResponse:
        raise NotImplementedError()

    async def _pulse(self) -> None:
        from ansq.tcp.types import NSQCommands

        await self.execute(NSQCommands.NOP)

    @abc.abstractmethod
    async def _upgrade_to_tls(self) -> None:
        raise NotImplementedError()

    @abc.abstractmethod
    def _upgrade_to_snappy(self) -> asyncio.Future:
        raise NotImplementedError()

    @abc.abstractmethod
    def _upgrade_to_deflate(self) -> asyncio.Future:
        raise NotImplementedError()

    @abc.abstractmethod
    async def _read_data_task(self) -> None:
        raise NotImplementedError()

    @abc.abstractmethod
    async def _parse_data(self) -> bool:
        raise NotImplementedError()

    @abc.abstractmethod
    def _on_message_hook(self, response: "NSQMessageSchema") -> None:
        raise NotImplementedError()

    @abc.abstractmethod
    async def _read_buffer(self) -> None:
        raise NotImplementedError()

    @abc.abstractmethod
    def _start_upgrading(self, resp: Optional[TCPResponse] = None) -> None:
        raise NotImplementedError()

    @abc.abstractmethod
    async def _finish_upgrading(self, resp: Optional[TCPResponse] = None) -> None:
        raise NotImplementedError()
