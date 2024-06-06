"""Validator for Hubble Metrics configuration values."""

from typing import List

from pydantic import BaseModel, validator


class HubbleMetrics(BaseModel):
    """Class to validate the values of the Hubble Metrics provided by the user."""

    metrics: List[str]

    @validator("metrics")
    def validate_metrics(cls, v):
        """Check that the values provided as metrics are in the allowed values.

        These values are sourced from upstream as the allowed metrics. Please
        refer to the following link for further information.
        https://docs.cilium.io/en/stable/observability/metrics/#hubble-metrics.
        """
        allowed_values = {
            "dns",
            "drop",
            "flow",
            "flows-to-world",
            "http",
            "icmp",
            "kafka",
            "port-distribution",
            "tcp",
        }
        for item in v:
            if item not in allowed_values:
                raise ValueError(f"{item} is not an allowed Hubble metric.")
        return v
