"""aivo GPU worker agent."""

import os

# Same scheme as the backend: the release workflow bakes AIVO_VERSION into the image.
__version__ = os.environ.get("AIVO_VERSION") or "dev"
