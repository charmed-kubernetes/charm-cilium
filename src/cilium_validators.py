"""Validators for Cilium configuration values."""

from pydantic import BaseModel, validator


class TunnelEncapsulationProtocol(BaseModel):
    """Class to validate the value of the Cilium tunnel encapsulation protocol provided by the user."""

    tunnel_protocol: str

    @validator("tunnel_protocol")
    def validate_tunnel_protocol(cls, v):
        """Check that the value provided as tunnel protocol are in the allowed values."""
        allowed_values = {
            "vxlan",
            "geneve",
        }

        if v not in allowed_values:
            raise ValueError(f"{v} is not an allowed Cilium tunnel encapsulation protocol.")
        return v
