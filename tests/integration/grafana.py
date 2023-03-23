from typing import Optional

import requests


class Grafana:
    """A class which abstracts access to a running instance of Grafana."""

    def __init__(
        self,
        ops_test,
        host: Optional[str] = "localhost",
        username: Optional[str] = "admin",
        password: Optional[str] = "",
    ):
        """Utility to manage a Grafana application.

        Args:
            ops_test: Default instance of ops_test.
            host: Optional host address of Grafana application, defaults to `localhost`.
            username: Optional username to connect with, defaults to `admin`.
            password: Optional password to connect with, defaults to `""`.
        """
        self.ops_test = ops_test
        self.base_uri = "http://{}/cos-grafana".format(host)
        self.username = username
        self.password = password

    async def is_ready(self) -> bool:
        """Send a request to check readiness.

        Returns:
          True if Grafana is ready (returned database information OK); False otherwise.
        """
        res = await self.health()
        return res.get("database", "") == "ok" or False

    async def health(self) -> dict:
        """A convenience method which queries the API to see whether Grafana is really ready.

        Returns:
            Empty :dict: if it is not up, otherwise a dict containing basic API health
        """
        api_path = "api/health"
        uri = "{}/{}".format(self.base_uri, api_path)

        response = requests.get(uri, auth=(self.username, self.password))

        assert response.status_code == 200, f"Failed to get health endpoint: {response.text}"
        return response.json()

    async def dashboards_all(self) -> list:
        """Try to get 'all' dashboards, since relation dashboards are not starred.

        Returns:
          Found dashboards, if any
        """
        api_path = "api/search"
        uri = "{}/{}?starred=false".format(self.base_uri, api_path)

        response = requests.get(uri, auth=(self.username, self.password))

        assert response.status_code == 200, f"Failed to get dashboards endpoint: {response.text}"
        return response.json()
