"""Account and balance management service via WebSocket for QuotexAPI."""

import asyncio
from typing import List, Optional, Dict, Any, Callable

from ..config import QuotexConfig
from ..enums import AccountType
from ..exceptions import QuotexAPIError
from ..models import Balance
from .base import BaseService
from .connection import ConnectionService


class AccountService(BaseService):
    """Service for managing accounts and balances via WebSocket."""

    def __init__(self, config: QuotexConfig, connection: ConnectionService):
        """
        Initialize account service.

        Args:
            config: Configuration instance.
            connection: WebSocket connection service.
        """
        super().__init__(config)
        self._connection = connection
        self._balances: List[Balance] = []
        self._active_account: Optional[AccountType] = None
        self._balance_callbacks: List[Callable] = []

    async def initialize(self) -> None:
        """Initialize the account service and subscribe to balance updates."""
        await super().initialize()
        
        # Subscribe to balance update events
        self._connection.subscribe("balance_update", self._handle_balance_update)

    async def get_balances(self) -> List[Balance]:
        """
        Get all account balances (Demo and Real) via WebSocket.
        
        Quotex sends balance in binary message:
        {"liveBalance":0,"demoBalance":10000,"tournamentsBalances":{},"dayLimit":0,"dayBalance":0}

        Returns:
            List[Balance]: List of balance information.

        Raises:
            QuotexAPIError: If fetching balances fails.
        """
        self._validate_initialized()
        self.logger.info("Fetching account balances")

        try:
            # Quotex pushes the balance right after authorization (event "s_balance/list",
            # payload {"liveBalance":..,"demoBalance":..}), so use the latest pushed value.
            response = self._connection.last_event("s_balance/list")

            if not isinstance(response, dict):
                # Not received yet: ask for it and wait
                balance_future = asyncio.Future()

                def handle_balance(data):
                    if not balance_future.done():
                        balance_future.set_result(data)

                self._connection.subscribe("s_balance/list", handle_balance)
                try:
                    await self._connection.send_socketio_event(
                        event="s_balance/list",
                        data={"_placeholder": True, "num": 0},
                        expect_response=False
                    )
                    response = await asyncio.wait_for(balance_future, timeout=10.0)
                except asyncio.TimeoutError:
                    seen = sorted(self._connection._last_events)[:20]
                    raise QuotexAPIError(
                        f"Timed out waiting for the balance from Quotex (events received: {seen})"
                    )
                finally:
                    self._connection.unsubscribe("s_balance/list", handle_balance)

            if not isinstance(response, dict):
                raise QuotexAPIError(f"Unexpected balance payload: {response!r}")
            
            # Parse Quotex balance format
            # Response: {"liveBalance":0,"demoBalance":10000,"tournamentsBalances":{},"dayLimit":0,"dayBalance":0}
            demo_balance = float(response.get("demoBalance", 0.0))
            real_balance = float(response.get("liveBalance", 0.0))
            tournaments_balances = response.get("tournamentsBalances", {})
            
            # Determine active account based on config
            active = "demo" if self.config.is_demo else "real"
            
            self._balances = [
                Balance(
                    account_type=AccountType.DEMO,
                    amount=demo_balance,
                    currency="USD",
                    is_active=(active == "demo"),
                ),
                Balance(
                    account_type=AccountType.REAL,
                    amount=real_balance,
                    currency="USD",
                    is_active=(active == "real"),
                ),
            ]

            self._active_account = AccountType.DEMO if active == "demo" else AccountType.REAL
            self.logger.info(f"Fetched balances - Demo: ${demo_balance:.2f}, Live: ${real_balance:.2f}")
            return self._balances

        except Exception as e:
            self.logger.error(f"Failed to fetch balances: {str(e)}")
            raise QuotexAPIError(f"Failed to fetch balances: {str(e)}") from e

    async def get_active_balance(self) -> Balance:
        """
        Get the active account balance.

        Returns:
            Balance: Active account balance.

        Raises:
            QuotexAPIError: If no active account or fetch fails.
        """
        self._validate_initialized()

        if not self._balances:
            await self.get_balances()

        active_balance = next(
            (b for b in self._balances if b.is_active), None
        )

        if not active_balance:
            raise QuotexAPIError("No active account found")

        return active_balance

    async def switch_account(self, account_type: AccountType) -> Balance:
        """
        Switch between Demo and Real accounts via WebSocket.

        Args:
            account_type: Account type to switch to.

        Returns:
            Balance: New active account balance.

        Raises:
            QuotexAPIError: If switching fails.
        """
        self._validate_initialized()
        self.logger.info(f"Switching to {account_type.value} account")

        try:
            # Send switch account request via WebSocket
            response = await self._connection.send_request(
                message_type="switch_account",
                data={"account_type": account_type.value},
                timeout=10.0
            )
            
            if not response or not response.get("success"):
                error = response.get("error", "Switch failed") if response else "No response"
                raise QuotexAPIError(error)

            # Update active status
            for balance in self._balances:
                balance.is_active = balance.account_type == account_type

            self._active_account = account_type

            active_balance = await self.get_active_balance()
            self.logger.info(
                f"Switched to {account_type.value} account: "
                f"{active_balance.amount} {active_balance.currency}"
            )

            return active_balance

        except Exception as e:
            self.logger.error(f"Failed to switch account: {str(e)}")
            raise QuotexAPIError(f"Failed to switch account: {str(e)}") from e
    
    def on_balance_update(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """
        Register callback for balance updates.
        
        Args:
            callback: Function to call when balance updates
        """
        self._balance_callbacks.append(callback)
    
    async def _handle_balance_update(self, data: Dict[str, Any]) -> None:
        """
        Handle incoming balance update from WebSocket.
        
        Quotex format: {"liveBalance":0,"demoBalance":10000,"tournamentsBalances":{},"dayLimit":0,"dayBalance":0}
        
        Args:
            data: Balance update data
        """
        self.logger.debug(f"Balance update received: {data}")
        
        # Update cached balances (Quotex format)
        demo_balance = data.get("demoBalance")
        real_balance = data.get("liveBalance")
        
        for balance in self._balances:
            if balance.account_type == AccountType.DEMO and demo_balance is not None:
                balance.amount = float(demo_balance)
            elif balance.account_type == AccountType.REAL and real_balance is not None:
                balance.amount = float(real_balance)
        
        # Call registered callbacks
        for callback in self._balance_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(data)
                else:
                    callback(data)
            except Exception as e:
                self.logger.error(f"Balance callback error: {e}")

    @property
    def active_account_type(self) -> Optional[AccountType]:
        """Get the currently active account type."""
        return self._active_account
