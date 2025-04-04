"""Validators for Cilium configuration values."""

from typing import Optional

from pydantic import BaseModel, validator

PROTOCOL_DEFAULT_PORTS = {"vxlan": "8472", "geneve": "6081"}


class TunnelEncapsulation(BaseModel):
    """Class to validate the value of the Cilium tunnel encapsulation settings provided by the user."""

    tunnel_protocol: str
    tunnel_port: Optional[str]

    @validator("tunnel_protocol")
    def validate_tunnel_protocol(cls, v):
        """Check that the value provided as tunnel protocol are in the allowed values."""
        allowed_values = set(PROTOCOL_DEFAULT_PORTS.keys())

        if v not in allowed_values:
            raise ValueError(f"{v} is not an allowed Cilium tunnel encapsulation protocol.")
        return v

    @validator("tunnel_port")
    def validate_tunnel_port(cls, v, values):
        """Decide which tunnel encapsulation port to use."""
        if "tunnel_protocol" not in values:
            return v

        if not v:
            return PROTOCOL_DEFAULT_PORTS[values["tunnel_protocol"]]

        if not v.isdigit():
            raise ValueError(f"{v} is not a valid port number (must be an integer).")
        port = int(v)
        if not (1 <= port <= 65535):
            raise ValueError(f"{port} is not a valid port number (must be between 1 and 65535).")
        return v
