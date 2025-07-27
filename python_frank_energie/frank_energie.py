"""FrankEnergie API implementation."""

# python_frank_energie/frank_energie.py

import asyncio
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
import re
from typing import Any
import logging
import traceback

_LOGGER = logging.getLogger(__name__)

import aiohttp
import requests
import sys
import platform
from aiohttp import ClientSession, ClientError

from .authentication import Authentication
from .exceptions import (
    AuthException,
    AuthRequiredException,
    NetworkError,
    RequestException,
    SmartTradingNotEnabledException,
    SmartChargingNotEnabledException,
)
from .models import (
    EnergyConsumption,
    EnodeChargers,
    Invoices,
    MarketPrices,
    Me,
    MonthSummary,
    PeriodUsageAndCosts,
    SmartBatteries,
    SmartBattery,
    SmartBatteryDetails,
    SmartBatterySessions,
    User,
    UserSites,
)

VERSION = "2025.6.17"

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class FrankEnergieQuery:
    """Represents a GraphQL query for the FrankEnergie API."""

    def __init__(
        self,
        query: str,
        operation_name: str,
        variables: dict[str, Any] | None = None,
    ) -> None:
        if variables is not None and not isinstance(variables, dict):
            raise TypeError(
                "The 'variables' argument must be a dictionary if provided."
            )

        self.query = query
        self.operation_name = operation_name
        self.variables = variables if variables is not None else {}

    def to_dict(self) -> dict[str, Any]:
        """Convert the query to a dictionary suitable for GraphQL API calls."""
        return {
            "query": self.query,
            "operationName": self.operation_name,
            "variables": self.variables,
        }


def sanitize_query(query: FrankEnergieQuery) -> dict[str, Any]:
    sanitized_query = query.to_dict()
    if "password" in sanitized_query["variables"]:
        sanitized_query["variables"]["password"] = "****"
    return sanitized_query


class FrankEnergie:
    """FrankEnergie API client."""

    DATA_URL = "https://frank-graphql-prod.graphcdn.app/"
    # DATA_URL = "https://graphql.frankenergie.nl/"

    def __init__(
        self,
        clientsession: ClientSession | None = None,
        auth_token: str | None = None,
        refresh_token: str | None = None,
    ) -> None:
        """Initialize the FrankEnergie client."""
        self._session: ClientSession | None = clientsession
        self._close_session: bool = clientsession is None
        self._auth: Authentication | None = None

        if auth_token or refresh_token:
            self._auth = Authentication(auth_token, refresh_token)

    @property
    def auth(self) -> Authentication | None:
        """Backwards compatibility for integrations accessing .auth directly."""
        _LOGGER.error(
            "Using .auth directly is deprecated. Use .is_authenticated instead."
        )
        return self._auth

    @property
    def is_authenticated(self) -> bool:
        """Check if the client is authenticated."""
        return self._auth is not None and self._auth.authToken is not None

    @staticmethod
    def generate_system_user_agent() -> str:
        """Generate the system user-agent string for API requests."""
        system = platform.system()  # e.g., 'Darwin' for macOS, 'Windows' for Windows
        system_platform = sys.platform  # e.g., 'win32', 'linux', 'darwin'
        release = platform.release()  # OS version (e.g., '10.15.7')
        version = VERSION  # App version

        user_agent = f"FrankEnergie/{version} {system}/{release} {system_platform}"
        return user_agent

    async def _ensure_session(self) -> None:
        """Ensure that a ClientSession is available."""
        if self._session is None:
            self._session = ClientSession()
            self._close_session = True

    async def _query(
        self, query: FrankEnergieQuery, extra_headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Send a query to the FrankEnergie API.

        Args:
            query: The GraphQL query as a dictionary.

        Returns:
            The response from the API as a dictionary.

        Raises:
            NetworkError: If the network request fails.
            FrankEnergieException: If the request fails.
        """

        # "User-Agent": self.generate_system_user_agent(), # not working properly
        headers = {"Content-Type": "application/json", "Accept": "application/json"}

        if self._auth is not None and self._auth.authToken is not None:
            headers["Authorization"] = f"Bearer {self._auth.authToken}"

        if extra_headers:
            headers.update(extra_headers)

        _LOGGER.debug("Request headers: %s", headers)
        if isinstance(query, dict):
            _LOGGER.debug("Request payload: %s", query)
        else:
            _LOGGER.debug("Request payload: %s", query.to_dict())

        await self._ensure_session()

        try:
            if hasattr(query, "to_dict") and callable(query.to_dict):
                payload = query.to_dict()
            else:
                _LOGGER.error(
                    "Query object does not implement to_dict() method: %s", query
                )
                raise TypeError(
                    "Query object must implement a to_dict() method to be JSON serializable.",
                    query,
                )
            async with self._session.post(
                self.DATA_URL, json=payload, headers=headers, timeout=30
            ) as resp:
                resp.raise_for_status()
                response_data: dict[str, Any] = await resp.json()

            if not response_data:
                _LOGGER.debug("No response data.")
                return {}

            logging.debug("Response body: %s", response_data)
            self._handle_errors(response_data)

            return response_data

        except (asyncio.TimeoutError, ClientError, KeyError) as error:
            _LOGGER.error("Request failed: %s", error)
            raise NetworkError(f"Request failed: {error}") from error
        except aiohttp.ClientResponseError as error:
            if error.status == HTTPStatus.UNAUTHORIZED:
                raise AuthRequiredException("Authentication required.") from error
            elif error.status == HTTPStatus.FORBIDDEN:
                raise AuthException("Forbidden: Invalid credentials.") from error
            elif error.status == HTTPStatus.BAD_REQUEST:
                raise RequestException("Bad request: Invalid query.") from error
            elif error.status == HTTPStatus.INTERNAL_SERVER_ERROR:
                raise AuthException("Internal server error.") from error
            else:
                raise AuthException(f"Unexpected response: {error}") from error
        except Exception as error:
            traceback.print_exc()
            raise error

    def _process_diagnostic_data(self, response: dict[str, Any]) -> None:
        """Process the diagnostic data and update the sensor state.

        Args:
            response: The API response as a dictionary.
        """
        diagnostic_data = response.get("diagnostic_data")
        if diagnostic_data:
            self._frank_energie_diagnostic_sensor.update_diagnostic_data(
                diagnostic_data
            )

    def _handle_errors(self, response: dict[str, Any]) -> None:
        """Catch common error messages and raise a more specific exception.

        Args:
            response: The API response as a dictionary.
        """
        if not response:
            _LOGGER.debug("No response data.")
            return

        errors = response.get("errors")
        if not errors:
            return

        for error in errors:
            message = error["message"]
            path = error.get("path")
            if message == "user-error:password-invalid":
                raise AuthException("Invalid password")
            elif message == "user-error:auth-not-authorised":
                raise AuthException("Not authorized")
            elif message == "user-error:auth-required":
                raise AuthRequiredException("Authentication required")
            elif message == "Graphql validation error":
                raise AuthException("Request failed: Graphql validation error")
            elif message.startswith("No marketprices found for segment"):
                return
            elif message.startswith("No connections found for user"):
                raise AuthException(f"Request failed: {message}")
            elif message == "user-error:smart-trading-not-enabled":
                _LOGGER.debug("Smart trading is not enabled for this user.")
                raise SmartTradingNotEnabledException(
                    "Smart trading is not enabled for this user."
                )
            elif message == "user-error:smart-charging-not-enabled":
                _LOGGER.debug("Smart charging is not enabled for this user.")
                raise SmartChargingNotEnabledException(
                    "Smart charging is not enabled for this user."
                )
            elif message == "'Base' niet aanwezig in prijzen verzameling":
                _LOGGER.debug("'Base' niet aanwezig in prijzen verzameling %s.", path)
            elif message == "request-error:request-not-supported-in-country":
                _LOGGER.error("Request not supported in the user's country: %s", error)
                raise AuthException(
                    "Request not supported in the user's country"
                )
            else:
                _LOGGER.error("Unhandled error: %s", message)
                _LOGGER.error("Unhandled error in GraphQL response: %s", error)

    LOGIN_QUERY = """
        mutation Login($email: String!, $password: String!) {
            login(email: $email, password: $password) {
                authToken
                refreshToken
            }
            version
            __typename
        }
    """

    async def login(self, username: str, password: str) -> Authentication:
        """Login and retrieve the authentication token.

        Args:
            username: The user's email.
            password: The user's password.

        Returns:
            The authentication information.

        Raises:
            AuthException: If the login fails.
        """
        if not username or not password:
            raise ValueError("Username and password must be provided.")

        query = FrankEnergieQuery(
            self.LOGIN_QUERY, "Login", {"email": username, "password": password}
        )

        try:
            response = await self._query(query)
            if response is not None:
                data = response["data"]
                if data is not None:
                    self._auth = Authentication.from_dict(response)
            return self._auth
        except Exception as error:
            traceback.print_exc()
            raise error

    async def renew_token(self) -> Authentication:
        """Renew the authentication token.

        Returns:
            The renewed authentication information.

        Raises:
            AuthRequiredException: If the client is not authenticated.
            AuthException: If the token renewal fails.
        """
        if self._auth is None or not self.is_authenticated:
            raise AuthRequiredException("Authentication is required.")

        query = FrankEnergieQuery(
            """
            mutation RenewToken($authToken: String!, $refreshToken: String!) {
                renewToken(authToken: $authToken, refreshToken: $refreshToken) {
                    authToken
                    refreshToken
                }
            }
            """,
            "RenewToken",
            {
                "authToken": self._auth.authToken,
                "refreshToken": self._auth.refreshToken,
            },
        )

        response = await self._query(query)
        self._auth = Authentication.from_dict(response)
        return self._auth

    async def meter_readings(self, site_reference: str) -> EnergyConsumption:
        """Retrieve the meter_readings.

        Args:
            site_reference: The site reference.

        Returns:
            The Meter Readings.

        Raises:
            AuthRequiredException: If the client is not authenticated.
            FrankEnergieException: If the request fails.
        """
        if self._auth is None or not self.is_authenticated:
            raise AuthRequiredException("Authentication is required.")

        query = FrankEnergieQuery(
            """
            query ActualAndExpectedMeterReadings($siteReference: String!) {
            completenessPercentage
            actualMeterReadings {
                date
                consumptionKwh
            }
            expectedMeterReadings {
                date
                consumptionKwh
            }
            }
            """,
            "ActualAndExpectedMeterReadings",
            {"siteReference": site_reference},
        )

        response = await self._query(query)
        return EnergyConsumption.from_dict(response)

    async def month_summary(self, site_reference: str) -> MonthSummary:
        """Retrieve the month summary for the specified month.

        Args:
            site_reference: The site reference.

        Returns:
            The month summary information.

        Raises:
            AuthRequiredException: If the client is not authenticated.
            FrankEnergieException: If the request fails.
        """
        if self._auth is None or not self.is_authenticated:
            raise AuthRequiredException("Authentication is required.")

        query = FrankEnergieQuery(
            """
            query MonthSummary($siteReference: String!) {
                monthSummary(siteReference: $siteReference) {
                    _id
                    actualCostsUntilLastMeterReadingDate
                    expectedCostsUntilLastMeterReadingDate
                    expectedCosts
                    lastMeterReadingDate
                    meterReadingDayCompleteness
                    gasExcluded
                    __typename
                }
                version
                __typename
            }
            """,
            "MonthSummary",
            {"siteReference": site_reference},
        )

        try:
            response = await self._query(query)
            return MonthSummary.from_dict(response)
        except Exception as e:
            raise AuthException(f"Failed to fetch month summary: {e}") from e

    async def enode_chargers(
        self, site_reference: str, start_date: date
    ) -> dict[str, EnodeChargers]:
        """Retrieve the enode charger information."""
        if self._auth is None or not self.is_authenticated:
            _LOGGER.debug("Skipping Enode Chargers: not authenticated.")
            return {}

        query = FrankEnergieQuery(
            """
            query EnodeChargers {
                enodeChargers {
                    ...Lots of fields...
                }
            }
            """,
            "EnodeChargers",
            {"siteReference": site_reference},
        )

        try:
            response: dict[str, Any] = await self._query(query)
            if response is None:
                _LOGGER.debug("No response data for 'enodeChargers'")
                return {}
            if "data" not in response:
                _LOGGER.debug("No data found in response for chargers: %s", response)
                return {}
            if response["data"] is None:
                _LOGGER.debug("No data for chargers found: %s", response)
                return {}
            if "enodeChargers" not in response["data"]:
                _LOGGER.debug("No chargers found in data: %s", response)
                return {}
            chargers_data = response["data"]["enodeChargers"]
            _LOGGER.info("%s Enode Chargers Found", len(chargers_data))
            _LOGGER.debug("Enode Chargers data: %s", chargers_data)
            return EnodeChargers.from_dict(chargers_data)
        except Exception as error:
            _LOGGER.debug("Error in enode_chargers: %s", error)
            _LOGGER.exception("Unexpected error during query: %s", error)
            return {}

    async def invoices(self, site_reference: str) -> Invoices:
        """Retrieve the invoices data."""
        if self._auth is None or not self.is_authenticated:
            raise AuthRequiredException("Authentication is required.")

        query = FrankEnergieQuery(
            """
            query Invoices($siteReference: String!) {
                invoices(siteReference: $siteReference) {
                    ...Lots of fields...
                }
            }
            """,
            "Invoices",
            {"siteReference": site_reference},
        )

        response = await self._query(query)
        return Invoices.from_dict(response)

    async def me(self, site_reference: str | None = None) -> Me:
        if self._auth is None:
            raise AuthRequiredException

        query = FrankEnergieQuery(
            """
            query Me($siteReference: String) {
                me {
                    ...UserFields
                }
            }
            fragment UserFields on User {
                ...Lots of fields...
            }
            """,
            "Me",
            {"siteReference": site_reference},
        )

        response = await self._query(query)
        return Me.from_dict(response)

    async def UserSites(self, site_reference: str | None = None) -> UserSites:
        if self._auth is None:
            raise AuthRequiredException

        query = FrankEnergieQuery(
            """
            query UserSites {
                userSites {
                    ...Lots of fields...
                }
            }
            """,
            "UserSites",
            {},
        )

        response = await self._query(query)
        return UserSites.from_dict(response)

    async def user_country(self) -> Me:
        if self._auth is None:
            raise AuthRequiredException

        query = FrankEnergieQuery(
            """
            query UserCountry {
                me {
                    countryCode
                }
            }
            """,
            "UserCountry",
            {},
        )

        response = await self._query(query)
        return Me.from_dict(response)

    async def user(self, site_reference: str | None = None) -> User:
        if self._auth is None:
            raise AuthRequiredException

        query = FrankEnergieQuery(
            """
            query Me($siteReference: String) {
                me {
                    ...UserFields
                }
            }
            fragment UserFields on User {
                ...Lots of fields...
            }
            """,
            "Me",
            {"siteReference": site_reference},
        )

        response = await self._query(query)
        return User.from_dict(response)

    async def be_prices(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> MarketPrices:
        """Get belgium market prices."""
        if start_date is None:
            start_date = datetime.now(timezone.utc).date()
        if end_date is None:
            end_date = start_date + timedelta(days=1)

        headers = {"x-country": "BE"}

        query = FrankEnergieQuery(
            """
            query MarketPrices ($date: String!) {
                marketPrices(date: $date) {
                    ...Lots of fields...
                }
            }
            """,
            "MarketPrices",
            {"date": str(start_date)},
        )
        response = await self._query(query, extra_headers=headers)
        return MarketPrices.from_be_dict(response)

    async def prices(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> MarketPrices:
        """Get market prices."""
        if not start_date:
            start_date = date.today()
        if not end_date:
            end_date = date.today() + timedelta(days=1)

        query = FrankEnergieQuery(
            """
            query MarketPrices($startDate: Date!, $endDate: Date!) {
                ...Lots of fields...
            }
            """,
            "MarketPrices",
            {"startDate": str(start_date), "endDate": str(end_date)},
        )
        response = await self._query(query)
        return MarketPrices.from_dict(response)

    async def user_prices(
        self,
        site_reference: str,
        start_date: date,
        end_date: date | None = None,
    ) -> MarketPrices:
        """Get customer market prices."""
        if self._auth is None:
            raise AuthRequiredException

        if not start_date:
            start_date = date.today()
        if not end_date:
            end_date = date.today() + timedelta(days=1)

        query = FrankEnergieQuery(
            """
            query MarketPrices($date: String!, $siteReference: String!) {
                customerMarketPrices(date: $date, siteReference: $siteReference) {
                    ...Lots of fields...
                }
            }
            """,
            "MarketPrices",
            {"date": str(start_date), "siteReference": site_reference},
        )
        response = await self._query(query)
        return MarketPrices.from_userprices_dict(response)

    async def period_usage_and_costs(
        self,
        site_reference: str,
        start_date: str,
    ) -> PeriodUsageAndCosts:
        """
        Haalt het verbruik en de kosten op voor een specifieke periode en locatie.
        Dit is net als op de factuur de marktprijs+
        """
        if not site_reference:
            raise ValueError("De 'site_reference' mag niet leeg zijn.")

        if self._auth is None:
            raise AuthRequiredException(
                "Authenticatie is vereist om deze query uit te voeren."
            )

        query = FrankEnergieQuery(
            """
            query PeriodUsageAndCosts($date: String!, $siteReference: String!) {
                periodUsageAndCosts(date: $date, siteReference: $siteReference) {
                    ...Lots of fields...
                }
            }
            """,
            "PeriodUsageAndCosts",
            {
                "siteReference": site_reference,
                "date": str(start_date),
            },
        )

        try:
            response = await self._query(query)
            return PeriodUsageAndCosts.from_dict(response)
        except Exception as err:
            _LOGGER.exception(
                "Fout bij ophalen van periodUsageAndCosts voor site %s op %s: %s",
                site_reference,
                start_date,
                err,
            )
            raise AuthException(
                "Kon verbruik en kosten niet ophalen voor opgegeven periode."
            ) from err

    async def smart_batteries(self) -> SmartBatteries:
        """Get the users smart batteries."""
        if self._auth is None:
            raise AuthRequiredException

        query = FrankEnergieQuery(
            """
            query SmartBatteries {
                smartBatteries {
                    ...Lots of fields...
                }
            }
            """,
            "SmartBatteries",
        )

        response = await self._query(query)

        # Handle empty or missing response data
        if not response:
            _LOGGER.warning("Empty or missing GraphQL response for 'smartBatteries'")
            return SmartBatteries([])

        if response.get("errors"):
            _LOGGER.error("Error response for 'smartBatteries': %s", response)
            return SmartBatteries([])

        if not response.get("data"):
            _LOGGER.warning("Empty or missing GraphQL response for 'smartBatteries'")
            return SmartBatteries([])

        _LOGGER.debug("Response data for 'smartBatteries': %s", response)
        batteries_data = response.get("data", {}).get("smartBatteries")

        if not batteries_data:
            _LOGGER.debug("No smart batteries found")
            return SmartBatteries([])

        try:
            smart_batteries = [SmartBattery.from_dict(b) for b in batteries_data]
        except (KeyError, ValueError, TypeError) as err:
            _LOGGER.error("Failed to parse smart batteries: %s", err)
            return SmartBatteries([])

        return SmartBatteries(smart_batteries)

    async def smart_battery_details(self, device_id: str) -> SmartBatteryDetails:
        """Retrieve smart battery details and summary."""
        if self._auth is None:
            raise AuthRequiredException

        if not device_id:
            raise ValueError("Missing required device_id for smart_battery_sessions")

        query = FrankEnergieQuery(
            """
            query SmartBattery($deviceId: String!) {
                smartBattery(deviceId: $deviceId) {
                    ...Lots of fields...
                }
                smartBatterySummary(deviceId: $deviceId) {
                    ...Lots of fields...
                }
            }
            """,
            "SmartBattery",
            {"deviceId": device_id},
        )

        response = await self._query(query)

        if response is None:
            _LOGGER.debug("No response data for 'smartBatteries'")
            raise AuthException(
                "No response data received for smart battery details"
            )
        if (
            "smartBattery" not in response["data"]
            or "smartBatterySummary" not in response["data"]
        ):
            _LOGGER.debug(
                "Incomplete response data for 'smartBattery' or 'smartBatterySummary'"
            )
            raise AuthException(
                "Incomplete response data for smart battery details"
            )
        return SmartBatteryDetails.from_dict(
            {
                "smartBattery": response["data"]["smartBattery"],
                "smartBatterySummary": response["data"]["smartBatterySummary"],
            }
        )

    async def smart_battery_sessions(
        self, device_id: str, start_date: date, end_date: date
    ) -> SmartBatterySessions:
        """List smart battery sessions for a device."""
        if self._auth is None:
            raise AuthRequiredException

        if not device_id:
            raise ValueError("Missing required device_id for smart_battery_sessions")

        query = FrankEnergieQuery(
            """
            query SmartBatterySessions($startDate: String!, $endDate: String!, $deviceId: String!) {
                ...Lots of fields...
            }
            """,
            "SmartBatterySessions",
            {
                "deviceId": device_id,
                "startDate": start_date.isoformat(),
                "endDate": end_date.isoformat(),
            },
        )

        response = await self._query(query)
        return SmartBatterySessions.from_dict(response)

    def _validate_not_future_date(self, value: date) -> None:
        if value > datetime.now(timezone.utc).date():
            raise ValueError("De 'start_date' mag niet in de toekomst liggen.")

    def _validate_start_date_format(self, start_date: str | date) -> None:
        if isinstance(start_date, date):
            start_date = start_date.isoformat()

        if not re.fullmatch(r"\d{4}(-\d{2}){0,2}", start_date):
            raise ValueError(
                "De 'start_date' moet een formaat hebben zoals 'YYYY', 'YYYY-MM' of 'YYYY-MM-DD'."
            )

        if len(start_date) == 10:  # volledige datum
            try:
                date_obj = datetime.strptime(start_date, "%Y-%m-%d").date()
                if date_obj > datetime.now(timezone.utc).date():
                    raise ValueError("De 'start_date' mag niet in de toekomst liggen.")
            except ValueError as e:
                raise ValueError(
                    "De 'start_date' heeft geen geldig datumformaat: %s" % e
                ) from e

    async def __aenter__(self):
        """Async enter.

        Returns:
            The FrankEnergie object.
        """
        return self

    async def __aexit__(self, *_exc_info: Any) -> None:
        """Async exit.

        Args:
            _exc_info: Exec type.
        """
        await self.close()

    def introspect_schema(self):
        query = """
            query IntrospectionQuery {
                __schema {
                    types {
                        name
                        fields {
                            name
                        }
                    }
                }
            }
        """

        with requests.post(
            self.DATA_URL, json={"query": query}, timeout=10
        ) as response:
            response.raise_for_status()
            result = response.json()
            return result

    def get_diagnostic_data(self):
        # Implement the logic to fetch diagnostic data from the FrankEnergie API
        # and return the data as needed for the diagnostic sensor
        return "Diagnostic data"

# frank_energie_instance = FrankEnergie()
# introspection_result = frank_energie_instance.introspect_schema()
# print("Introspection Result:", introspection_result)