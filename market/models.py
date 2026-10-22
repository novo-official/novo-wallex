from django.db import models

# Prices, volumes and quantities are DecimalFields. Never store them as floats.
DECIMAL_FIELD = {"max_digits": 40, "decimal_places": 18}


class Market(models.Model):
    """A Wallex spot market, refreshed by `sync_markets`."""

    symbol = models.CharField(max_length=32, unique=True)  # API symbol, e.g. BTCTMN
    base_asset = models.CharField(max_length=16)
    quote_asset = models.CharField(max_length=16)
    amount_precision = models.PositiveSmallIntegerField()
    price_precision = models.PositiveSmallIntegerField()
    # The spot Swagger has no minimum order value field. This stays NULL until the
    # docs or the team confirm the source. The execution layer must refuse orders
    # when it is NULL (fail closed).
    min_order_value = models.DecimalField(null=True, blank=True, **DECIMAL_FIELD)
    last_price = models.DecimalField(null=True, blank=True, **DECIMAL_FIELD)
    volume_24h = models.DecimalField(null=True, blank=True, **DECIMAL_FIELD)
    quote_volume_24h = models.DecimalField(null=True, blank=True, **DECIMAL_FIELD)
    is_blocked = models.BooleanField(default=False)  # listed in blocked_markets.txt
    is_allowed = models.BooleanField(default=False)  # allowlisted and not blocked, at last sync
    synced_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.symbol


class Candle(models.Model):
    """One OHLCV bar. Written by `download_history`, idempotent per (market, resolution, open_time)."""

    market = models.ForeignKey(Market, on_delete=models.CASCADE, related_name="candles")
    resolution = models.CharField(max_length=8)  # UDF resolution, minutes, e.g. "60"
    open_time = models.DateTimeField()  # UTC bar start
    open_price = models.DecimalField(**DECIMAL_FIELD)
    high_price = models.DecimalField(**DECIMAL_FIELD)
    low_price = models.DecimalField(**DECIMAL_FIELD)
    close_price = models.DecimalField(**DECIMAL_FIELD)
    volume = models.DecimalField(**DECIMAL_FIELD)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["market", "resolution", "open_time"],
                name="unique_candle_per_bar",
            ),
        ]


class OrderbookSnapshot(models.Model):
    """Best bid and ask of a market at one point in time, from `sync_orderbook`."""

    market = models.ForeignKey(Market, on_delete=models.CASCADE, related_name="orderbook_snapshots")
    captured_at = models.DateTimeField(db_index=True)
    best_bid = models.DecimalField(**DECIMAL_FIELD)
    best_ask = models.DecimalField(**DECIMAL_FIELD)
    spread_fraction = models.DecimalField(**DECIMAL_FIELD)  # (ask - bid) / mid

    class Meta:
        ordering = ["-captured_at"]
