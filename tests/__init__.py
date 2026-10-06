"""The BioSense test suite.

The agents' OS-sandbox check runs bubblewrap for real, and whether a CI machine
has it installed says nothing about BioSense. It is off for the suite; the tests
of the check itself pass their own trial and environment.
"""
import os

os.environ.setdefault('BIOSENSE_SANDBOX_CHECK', '0')
