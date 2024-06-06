from typing import Optional

import requests


class Prometheus:
    """A class which abstracts access to a running instance of Prometheus."""

    def __init__(
        self,
        ops_test,
        host: Optional[str] = "localhost",
    ):
        """Manage a Prometheus application.

        Args:
            ops_test: Default instance of ops_test.
            host: Optional host address of Prometheus application, defaults to `localhost`
        """
        self.ops_test = ops_test
        self.base_uri = f"http://{host}/cos-prometheus-0"

    async def is_ready(self) -> bool:
        """Send a request to check readiness.

        Returns:
          True if Prometheus is ready (returned 'Prometheus is Ready.'); False otherwise.
        """
        res = await self.health()
        return "Prometheus Server is Ready." in res

    async def health(self) -> str:
        """Check Prometheus readiness using the MGMT API.

        A convenience method which queries the API to see whether Prometheus is ready
           to serve traffic (i.e. respond to queries).

        Returns:
            Empty :str: if it is not up, otherwise a str containing "Prometheus is Ready"
        """
        api_path = "-/ready"
        uri = f"{self.base_uri}/{api_path}"

        response = requests.get(uri)

        assert response.status_code == 200, f"Failed to get health endpoint: {response.text}"
        return response.text

    async def get_metrics(self) -> list:
        """Try to get all metrics reported to Prometheus by Cilium components.

        Returns:
          Found metrics, if any
        """
        api_path = "api/v1/label/__name__/values"
        uri = f"{self.base_uri}/{api_path}"
        params = {"match[]": ['{__name__=~".+", job!="prometheus"}']}

        response = requests.get(uri, params=params)

        assert response.status_code == 200, f"Failed to get metrics: {response.text}"
        return response.json()["data"]
