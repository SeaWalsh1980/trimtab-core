"""CONTROL for ADR 0054, never merged: this module fails."""

import unittest


class Control(unittest.TestCase):
    def test_fails_as_seeded_for_the_control(self):
        self.fail("seeded")
