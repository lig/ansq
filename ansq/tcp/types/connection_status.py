from enum import Enum


class ConnectionStatus(Enum):
    CLOSED = 0
    CLOSING = 0
    INIT = 1
    CONNECTED = 2
    SUBSCRIBED = 3
    RECONNECTING = 4

    @property
    def is_closed(self) -> bool:
        return self == ConnectionStatus.CLOSED

    @property
    def is_closing(self) -> bool:
        return self == ConnectionStatus.CLOSING

    @property
    def is_init(self) -> bool:
        return self == ConnectionStatus.INIT

    @property
    def is_connected(self) -> bool:
        return self == ConnectionStatus.CONNECTED

    @property
    def is_subscribed(self) -> bool:
        return self == ConnectionStatus.SUBSCRIBED

    @property
    def is_reconnecting(self) -> bool:
        return self == ConnectionStatus.RECONNECTING

    def __bool__(self) -> bool:
        return not self.is_closed and not self.is_closing and not self.is_init
