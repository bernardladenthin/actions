# SPDX-FileCopyrightText: 2026 Bernard Ladenthin <bernard.ladenthin@gmail.com>
#
# SPDX-License-Identifier: MIT OR Apache-2.0
"""Shared fixtures of the actionlib tests."""

import os

from actionlib.testing import upload, workflow_text  # noqa: F401 -- re-exported for the tests

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
