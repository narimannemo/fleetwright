"""`python -m superagentic`, for when the console script is not on PATH.

A wheel installed with `pip install --target`, a zipapp, or a virtualenv whose
bin directory is not exported all have the package importable and the command
missing. This is one line so that case is not a dead end.
"""

from .cli import main

raise SystemExit(main())
