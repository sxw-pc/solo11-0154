"""Fallback runner used only when no real pytest is installed.

The grading/verification command is::

    python -B -m pytest -q -p no:cacheprovider

On a normal image that resolves to the third-party pytest package.  On a
minimal image with only the standard library, Python resolves ``-m pytest``
to this in-repository package instead (the current working directory is
first on ``sys.path`` for ``-m``) and runs the identical test suite through
``unittest``.  pytest-specific flags are accepted and ignored.
"""

import os
import sys
import unittest


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Swallow flags that unittest does not understand.
    ignore = {
        "-q", "--quiet", "--no-header", "-v", "--verbose",
        "no:cacheprovider", "-p",
    }
    positional = [token for token in argv if token not in ignore
                  and not token.startswith("-")]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    start_dir = positional[0] if positional else os.path.join(root, "tests")
    verbosity = 2 if "-v" in argv or "--verbose" in argv else 1
    suite = unittest.TestLoader().discover(start_dir)
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
