"""Scraper error types.

Two kinds, because they need opposite handling:

- ``ScrapeError`` is about ONE item — the page didn't look like what we expect,
  so fail that item loudly and move on rather than record a false "scraped"
  state.
- ``ScrapeBlocked`` is about the WHOLE run — Cardmarket's bot protection is in
  the way, so every remaining item would fail identically. It aborts the job
  instead of burning hundreds of items against a wall.
"""


class ScrapeError(RuntimeError):
    """The page did not look like what we expected (markup drift, empty set)."""


class ScrapeBlocked(ScrapeError):
    """Cardmarket (Cloudflare) is challenging or blocking us.

    ``recoverable`` separates two very different situations:

    * A challenge that simply didn't finish in the time allowed. It usually
      clears on its own, so a fresh browser and a pause often walk straight
      through — the run should back off and try again, not die.
    * An outright refusal ("Sorry, you have been blocked"). Waiting changes
      nothing; the run must stop.
    """

    def __init__(self, *args, recoverable: bool = False):
        super().__init__(*args)
        self.recoverable = recoverable
