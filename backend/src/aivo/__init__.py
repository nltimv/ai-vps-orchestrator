"""aivo: on-demand GPU orchestration for AI generation."""

import os

# Release images carry their version (<VERSION file>.<build number>, set by the release workflow);
# a source checkout says "dev".
__version__ = os.environ.get("AIVO_VERSION") or "dev"
