from django.apps import AppConfig


class ExchangeConfig(AppConfig):
    name = "exchange"

    def ready(self) -> None:
        # Importing registers the market-policy startup check.
        from . import checks  # noqa: F401
