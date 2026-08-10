"""Per-card price scrape — port of the legacy get_card_price().

Bug fix vs the notebook: the legacy code addressed the info sidebar by absolute
positional XPaths (dd[6]/dd[7]/dd[8]), which silently shifts every price by one
field the moment Cardmarket adds or removes a row. Here each dt LABEL is
matched to its dd value.
"""

from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.by import By

from ..browser import navigate, random_delay, scroll_to_bottom
from ..parsers import PriceParseError, parse_price

LABELS = {
    "from": "price_from",
    "price trend": "price_trend",
    "30-days average price": "price_30d_avg",
}


def scrape_card_prices(driver, url: str, log=print) -> dict:
    """Return {price_from?, price_trend?, price_30d_avg?, currency} — keys are
    only present when that label was found AND parsed; a missing field never
    shifts the others."""
    navigate(driver, url, log=log)
    scroll_to_bottom(driver)
    random_delay(1, 2)

    try:
        info = driver.find_element(By.ID, "tabContent-info")
    except NoSuchElementException:
        raise PriceParseError(f"info tab not found on {url}") from None

    dts = info.find_elements(By.CSS_SELECTOR, "dl dt")
    prices: dict = {}
    for dt in dts:
        label = dt.text.strip().lower()
        field = LABELS.get(label)
        if field is None:
            continue
        try:
            dd = dt.find_element(By.XPATH, "following-sibling::dd[1]")
        except NoSuchElementException:
            continue
        try:
            value, currency = parse_price(dd.text)
        except PriceParseError:
            log(f"could not parse {label!r} on {url}: {dd.text!r}")
            continue
        prices[field] = value
        prices["currency"] = currency
    return prices
