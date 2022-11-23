# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

import unittest

import ops.testing
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from ops.testing import Harness

from charm import CharmCiliumCharm


class TestCharm(unittest.TestCase):
    pass
