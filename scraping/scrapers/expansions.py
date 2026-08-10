"""Expansion discovery — port of the legacy get_expansions().

Walks https://www.cardmarket.com/en/{segment}/Expansions and collects the
expansion URL slugs for the most recent N year-groups.
"""

from selenium.webdriver.common.by import By

from ..browser import accept_cookies, navigate, random_delay, scroll_to_bottom


def discover_expansions(driver, segment: str, last_n_years: int = 2, log=print) -> list[str]:
    url = f"https://www.cardmarket.com/en/{segment}/Expansions"
    navigate(driver, url, log=log)
    accept_cookies(driver)
    scroll_to_bottom(driver)
    random_delay(2, 4)

    container = driver.find_element(By.ID, "ExpansionList")
    groups = container.find_elements(By.CLASS_NAME, "expansion-group")
    slugs: list[str] = []
    for group in groups[:last_n_years]:
        try:
            year = group.find_element(By.TAG_NAME, "h2").text
        except Exception:
            year = "?"
        rows = group.find_elements(By.CLASS_NAME, "expansion-row")
        found = 0
        for row in rows:
            data_url = row.get_attribute("data-url")
            if not data_url:
                continue
            slug = data_url.rstrip("/").rsplit("/", 1)[-1]
            if slug:
                slugs.append(slug)
                found += 1
        log(f"year group {year}: {found} expansions")
    return slugs
