from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


@register.filter
def euro(value):
    """Format as European-style EUR: 1.234,56. Empty string for None."""
    if value is None or value == "":
        return ""
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError):
        return value
    us_style = f"{amount:,.2f}"  # 1,234.56
    european = us_style.replace(",", "@").replace(".", ",").replace("@", ".")
    return f"{european} €"
