"""DownloadClient parity: basket operations against the SAP Support Portal.

Async-free, synchronous core; the FastAPI layer will run it in worker threads
Busy-state tracking mirrors the original DownloadClient operations.
"""

from __future__ import annotations

from enum import Enum

from sapdlm.client.basket import BasketContent, DownloadEnd, DownloadStart, ObjectVersion
from sapdlm.client.connection import (
    RefreshCancelled,
    AuthenticationConfigError,
    ConnectionConfig,
    SapConnection,
    SapIOError,
)
from sapdlm.client.parsers import (
    parse_basket_content,
    parse_download_end,
    parse_download_start,
    parse_object_version,
)


class Operation(Enum):
    NONE = 0
    CHECK_BASKET_CONTENT = 1  # DownloadClient.OPERATION_CHECK_BASKETCONTENT
    DOWNLOAD_ITEM = 2
    ADD_ITEM = 3


class DownloadClientBusyError(Exception):
    """Original: DownloadClientBusyException."""


class ItemNotFoundError(Exception):
    """Original: ItemNotFoundException."""


class DownloadClient:
    """Orchestrates portal communication (DownloadClient parity)."""

    def __init__(self, config: ConnectionConfig | None = None,
                 http_client=None):
        self.config = config
        self._http_client = http_client  # optional httpx.Client (tests / DI)
        self.busy = False
        self.connected = False
        self.current_operation = Operation.NONE
        self.last_error: Exception | None = None
        self.basket: BasketContent | None = None

    def _connection(self) -> SapConnection:
        return SapConnection(self.config, client=self._http_client)

    # ------------------------------------------------------------------ operations

    def _check_busy(self) -> None:
        if self.busy:
            raise DownloadClientBusyError("The download client is already busy.")

    def check_basket_content(self, stop=None) -> BasketContent:
        """Fetch and parse the basket (synchronousCheckBasketContent parity).

        Retries up to 5 times except on HTTP 401/403 (GetBasketContentThread).
        """
        self._check_busy()
        self.busy = True
        self.current_operation = Operation.CHECK_BASKET_CONTENT
        try:
            self.config.validate_auth()
            connection = self._connection()
            response = connection.get_with_retries(
                connection.endpoints.basket_content(), stop=stop)
            self.connected = True
            self.basket = parse_basket_content(response.text, connection.style)
            return self.basket
        except RefreshCancelled:
            raise
        except Exception as exc:
            self.connected = False
            self.last_error = exc
            if isinstance(exc, SapIOError):
                raise SapIOError(f"Unabled to read the basket content: {exc}") from exc
            raise SapIOError(f"Unabled to read the basket content: {exc}") from exc
        finally:
            self.busy = False
            self.current_operation = Operation.NONE

    def get_object_version(self, dockey: str) -> ObjectVersion:
        connection = self._connection()
        response = connection.get(connection.endpoints.object_version(dockey))
        return parse_object_version(response.text, connection.style)

    def start_download(self, dockey: str) -> DownloadStart:
        connection = self._connection()
        response = connection.get(connection.endpoints.download_start(dockey))
        return parse_download_start(response.text, connection.style)

    def end_download(self, dockey: str) -> DownloadEnd:
        connection = self._connection()
        response = connection.get(connection.endpoints.download_end(dockey))
        return parse_download_end(response.text, connection.style)

    def remove_from_basket(self, dockey: str) -> None:
        """Original: BasketItem.removeFromBasket (OBJDEL). Local cleanup is the
        caller's responsibility (engine/state, M2)."""
        if self.basket is None or dockey not in self.basket.items:
            # Mirror of removeFromDownloadBasket: empty basket -> ItemNotFound.
            raise ItemNotFoundError(
                f"There is no item with the object id '{dockey}' in your download basket."
            )
        connection = self._connection()
        connection.get(connection.endpoints.object_delete(dockey))
