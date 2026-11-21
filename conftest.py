"""Repository-level pytest configuration.

The application code (``wastetransit``) uses only the Python standard
library.  When the image provides pytest, this file is collected normally.
When pytest is missing entirely, ``python -m pytest`` resolves to the
in-repo ``pytest/`` shim package instead, which runs the same suite via
``unittest``; see ``pytest/__main__.py``.
"""
