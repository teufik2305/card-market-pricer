"""Chrome launch and Cloudflare-aware navigation.

Cardmarket is behind Cloudflare, and the decisive finding (2026-08-09) is that
**chromedriver launching Chrome is what gets refused**, not this machine and not
Selenium. Measured on the same IP, same minute, same URL:

    plain Chrome, launched normally  →  challenge auto-cleared in ~5s, real page
    chromedriver-launched Chrome     →  "Sorry, you have been blocked" (403)

chromedriver starts Chrome with its own switches and a throwaway profile, and
that launch signature is what Cloudflare scores. So we start Chrome ourselves —
no automation switches, a persistent profile, a real first page — let it pass
the check like any browser, and only then attach Selenium over the debugging
port. Driving an already-cleared browser is fine; the clearance cookie rides
along in the profile.

Everything else follows from that:

- **The profile persists** (``var/chrome-profile/``). Clearance survives between
  drivers and jobs; a throwaway profile re-triggers the check every time.
- **A challenge is not a scraping result.** ``navigate()`` detects the
  interstitial and raises ``ScrapeBlocked`` rather than letting an empty page be
  recorded as an empty expansion.
"""

import json
import random
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from django.conf import settings
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

from .errors import ScrapeBlocked

COOKIE_BUTTON_XPATH = '//*[@id="CookiesConsent"]/div/div/form/div/button'

# Cloudflare's interstitial: JS runs, usually clears itself, sometimes wants a click.
SOFT_TITLES = ("just a moment", "checking your browser", "ein moment")
SOFT_SELECTORS = ("#challenge-running", "#challenge-form", ".cf-turnstile", "#cf-chl-widget")
# A refusal. Waiting never clears these.
HARD_TITLES = ("attention required", "access denied", "you have been blocked")
HARD_SELECTORS = (".cf-error-code", "#cf-error-details")


def random_delay(min_delay: float = 3, max_delay: float = 10) -> None:
    time.sleep(random.uniform(min_delay, max_delay))


def home_url(segment: str) -> str:
    return f"https://www.cardmarket.com/en/{segment}"


def profile_dir() -> Path:
    path = Path(settings.SCRAPE_PROFILE_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


class ManagedDriver:
    """A Selenium driver attached to a Chrome process we own.

    Proxies everything to the real driver; ``quit()`` also stops the browser we
    launched, which Selenium won't do for a session it merely attached to.
    """

    def __init__(self, driver, process):
        self._driver = driver
        self._process = process

    def __getattr__(self, name):
        return getattr(self._driver, name)

    def quit(self):
        try:
            self._driver.quit()
        except Exception:
            pass
        finally:
            _terminate(self._process)


def _terminate(process) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except Exception:
        process.kill()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _cdp_title(port: int) -> str | None:
    """The active page's title, read passively over CDP — no JS evaluated, so
    nothing is injected into the page before Cloudflare has judged it."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5) as fh:
            targets = json.load(fh)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    for target in targets:
        if target.get("type") == "page":
            return target.get("title", "")
    return None


def chrome_binary() -> str:
    """Where Chrome lives. The operator setting wins over the platform default,
    so a Mac with Chrome installed somewhere unusual (or Chromium, or a Windows
    box) is a form field rather than a code change."""
    from .models import ScraperSettings
    try:
        return ScraperSettings.load().chrome_path
    except Exception:
        # Settings table missing (early migration) — fall back to the default.
        return settings.SCRAPE_CHROME_BINARY


def challenge_timeout() -> int:
    from .models import ScraperSettings
    try:
        return ScraperSettings.load().challenge_timeout
    except Exception:
        return settings.SCRAPE_CHALLENGE_TIMEOUT


def build_driver(
    headless: bool = False,
    profile: str | Path | None = None,
    warm_url: str | None = None,
    log=print,
) -> ManagedDriver:
    """Launch Chrome like a person would, wait out Cloudflare, then attach.

    warm_url is the page Chrome opens on its own — give it the game's front page
    so the run's challenge is met once, before Selenium is anywhere near it.
    headless is off by default and should stay off: Cloudflare fails headless
    Chrome far more aggressively, and you can't click a checkbox you can't see.
    """
    port = _free_port()
    user_data_dir = Path(profile) if profile else profile_dir()
    binary = chrome_binary()
    command = [
        binary,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        f"--window-size={random.randint(1360, 1500)},{random.randint(900, 1040)}",
    ]
    if headless:
        command.append("--headless=new")
    command.append(warm_url or "about:blank")

    try:
        process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except (FileNotFoundError, PermissionError, NotADirectoryError) as exc:
        raise ScrapeBlocked(
            f"Chrome could not be started from {binary!r} ({type(exc).__name__}). "
            "Set the Chrome path on the scrape panel."
        ) from exc

    try:
        _await_cdp(process, port)
        if warm_url:
            _await_launch_clearance(port, warm_url, log=log)
        options = Options()
        options.debugger_address = f"127.0.0.1:{port}"
        try:
            # chromedriver still drives the session — it just didn't start the
            # browser, which is the part Cloudflare scores.
            driver = webdriver.Chrome(options=options)
        except WebDriverException as exc:
            raise _attach_error(exc) from exc
    except Exception:
        _terminate(process)
        raise

    managed = ManagedDriver(driver, process)
    if warm_url:
        accept_cookies(managed)
        random_delay(1, 3)
    return managed


def _await_cdp(process, port: int, timeout: int = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ScrapeBlocked(
                "Chrome exited immediately. If another Chrome is using the scraper "
                "profile, close it (or delete var/chrome-profile/) and retry."
            )
        if _cdp_title(port) is not None:
            return
        time.sleep(0.5)
    raise ScrapeBlocked(f"Chrome did not open its debugging port within {timeout}s.")


def _await_launch_clearance(port: int, url: str, *, log=print) -> None:
    """Watch the freshly-launched browser pass Cloudflare, over CDP only."""
    timeout = challenge_timeout()
    deadline = time.monotonic() + timeout
    announced = False
    while time.monotonic() < deadline:
        title = (_cdp_title(port) or "").strip().lower()
        if any(marker in title for marker in HARD_TITLES):
            raise _blocked(url, "access refused")
        if title and not any(title.startswith(m) for m in SOFT_TITLES):
            if announced:
                log("challenge cleared, continuing")
            return
        if title and not announced:
            log(f"Cloudflare check on {url} — waiting up to {timeout}s. "
                "It usually clears on its own; if a checkbox appears, click it.")
            announced = True
        time.sleep(2)
    raise _blocked(url, f"challenge did not clear within {timeout}s", recoverable=True)


def accept_cookies(driver) -> None:
    """Click the consent banner if present; tolerate its absence or redesign."""
    try:
        driver.find_element(By.XPATH, COOKIE_BUTTON_XPATH).click()
        random_delay(1, 2)
    except NoSuchElementException:
        pass
    except Exception:
        pass


def scroll_to_bottom(driver) -> None:
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")


def challenge_state(driver) -> str | None:
    """'soft' (interstitial), 'hard' (refused), or None (real page)."""
    title = (driver.title or "").strip().lower()
    if any(marker in title for marker in HARD_TITLES):
        return "hard"
    if any(driver.find_elements(By.CSS_SELECTOR, sel) for sel in HARD_SELECTORS):
        return "hard"
    if any(title.startswith(marker) for marker in SOFT_TITLES):
        return "soft"
    if any(driver.find_elements(By.CSS_SELECTOR, sel) for sel in SOFT_SELECTORS):
        return "soft"
    return None


def needs_a_human(driver_or_port) -> bool:
    """True when the interactive widget is up, as opposed to the interstitial
    that clears on its own. Only the first needs you."""
    if isinstance(driver_or_port, int):
        return False  # CDP title-only view can't see into the page
    try:
        return any(
            driver_or_port.find_elements(By.CSS_SELECTOR, sel)
            for sel in (".cf-turnstile", "#cf-chl-widget")
        )
    except Exception:
        return False


def alert_human(message: str, log=print) -> None:
    """Get the operator's attention. A 930-card refresh runs for hours and
    nobody watches the window; a challenge that needs a click at minute 40 is
    otherwise found an hour later, timed out."""
    log("\a" + message)  # terminal bell
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{message}" with title "Cardvault" sound name "Ping"'],
            check=False, timeout=5, capture_output=True,
        )
    except Exception:
        pass


def _blocked(url: str, detail: str, *, recoverable: bool = False) -> ScrapeBlocked:
    return ScrapeBlocked(
        f"Cardmarket is blocking the scraper ({detail}) at {url}. "
        "Nothing was marked as scraped. The check usually clears by itself in 5–15 "
        "seconds, so if the wait was short, raise “Cloudflare wait” on the data-jobs "
        "panel. Otherwise leave the browser window visible and click 'Verify you are "
        "human' when it appears; if the block persists, wait 15–60 minutes before "
        "retrying — the run was probably going too fast.",
        recoverable=recoverable,
    )


def await_clearance(driver, url: str, *, log=print, timeout: int | None = None) -> None:
    """Block until the page is real, or raise ScrapeBlocked."""
    state = challenge_state(driver)
    if state is None:
        return
    if state == "hard":
        raise _blocked(url, "access refused")

    timeout = challenge_timeout() if timeout is None else timeout
    log(f"Cloudflare check on {url} — waiting up to {timeout}s. "
        "It usually clears on its own.")
    deadline = time.monotonic() + timeout
    alerted = False
    while time.monotonic() < deadline:
        time.sleep(3)
        state = challenge_state(driver)
        if state is None:
            log("challenge cleared, continuing")
            random_delay(1, 3)
            return
        if state == "hard":
            raise _blocked(url, "access refused after a challenge")
        if not alerted and needs_a_human(driver):
            # It stopped self-solving and is asking for a click. Say so loudly,
            # once — the operator may be in another room.
            alert_human("Cardmarket needs you to tick “Verify you are human”.", log=log)
            alerted = True
    raise _blocked(url, f"challenge did not clear within {timeout}s", recoverable=True)


def navigate(driver, url: str, *, log=print, settle: tuple[float, float] = (1.5, 3)) -> None:
    """The only way the scrapers should load a page: get, settle, clear checks."""
    driver.get(url)
    random_delay(*settle)
    await_clearance(driver, url, log=log)


def _attach_error(exc: WebDriverException) -> Exception:
    """Translate Selenium's attach failures into something actionable."""
    if "Status code was: -9" in str(exc):
        return ScrapeBlocked(
            "macOS killed chromedriver before it started (Gatekeeper rejects "
            "unnotarized downloads). Clear the driver cache and let Selenium "
            "fetch it again: rm -rf ~/.wdm ~/Library/Caches/selenium"
        )
    return exc
