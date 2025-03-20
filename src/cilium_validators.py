"""Validators for Cilium configuration values."""

from typing import Optional

from pydantic import BaseModel, validator

protocols = {"vxlan": "4872", "geneve": "6081"}


class TunnelEncapsulation(BaseModel):
    """Class to validate the value of the Cilium tunnel encapsulation settings provided by the user."""

    tunnel_protocol: str
    tunnel_port: Optional[str]

    @validator("tunnel_protocol")
    def validate_tunnel_protocol(cls, v):
        """Check that the value provided as tunnel protocol are in the allowed values."""
        allowed_values = set(protocols.keys())

        if v not in allowed_values:
            raise ValueError(f"{v} is not an allowed Cilium tunnel encapsulation protocol.")
        return v

    @validator("tunnel_port")
    def validate_tunnel_port(cls, v, values):
        """Decide which tunnel encapsulation port to use."""
        if v:
            return v

        return protocols[values.get("tunnel_protocol")]
