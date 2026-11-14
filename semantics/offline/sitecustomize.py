"""Refuse every network call a Python process makes, from its start.

Python imports `sitecustomize` at start-up from its import path, so a process started with this
folder on `PYTHONPATH` (as every MetricFlow process the tests start is) installs the refusal of
`refusal.py` before any code of its own runs. Never import this module from a test: it would
install the refusal in the test process itself.
"""

import sys

from refusal import refuse_the_network

sys.addaudithook(refuse_the_network)
