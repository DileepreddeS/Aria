"""What a request costs, from the price table in ``prices.yaml``.

Two numbers come from here. :meth:`PriceTable.cost` prices what a provider reports it used.
:meth:`PriceTable.worst_case` bounds what a request can cost before it is sent: its input
tokens, counted exactly by the provider before sending, and the whole output allowance,
which the model cannot exceed. The spend caps are checked against that bound, so a request
that would fit only if it turned out cheap is refused. Only if counting fails does the
gateway fall back to one token per character, the loose bound that stays on the safe side.

Money is :class:`decimal.Decimal` throughout. A float would make the caps compare numbers
that are not quite the ones written in the table.
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass, fields
from functools import cache
from importlib import resources
from typing import Literal

import yaml

__all__ = ["ModelPrice", "PriceTable", "Usage", "load_prices"]

Geo = Literal["global", "us"]
_MILLION = decimal.Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class Usage:
    """Tokens as a provider reports them."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """USD per million tokens."""

    input: decimal.Decimal
    output: decimal.Decimal
    cache_write: decimal.Decimal
    cache_read: decimal.Decimal


@dataclass(frozen=True, slots=True)
class PriceTable:
    version: str
    geo_multipliers: dict[str, decimal.Decimal]
    models: dict[str, ModelPrice]

    def price(self, model: str) -> ModelPrice | None:
        """None for a model the table does not know; the gateway refuses those."""
        return self.models.get(model)

    def _multiplier(self, geo: Geo) -> decimal.Decimal:
        return self.geo_multipliers[geo]

    def cost(self, model: str, geo: Geo, usage: Usage) -> decimal.Decimal:
        price = self.models[model]
        total = (
            usage.input_tokens * price.input
            + usage.output_tokens * price.output
            + usage.cache_write_tokens * price.cache_write
            + usage.cache_read_tokens * price.cache_read
        )
        return (total * self._multiplier(geo) / _MILLION).quantize(decimal.Decimal("0.000001"))

    def worst_case(
        self, model: str, geo: Geo, *, input_tokens: int, max_output_tokens: int
    ) -> decimal.Decimal:
        """The most a request can cost: its input tokens in, the whole output allowance out."""
        return self.cost(model, geo, Usage(input_tokens=input_tokens, output_tokens=max_output_tokens))


def _money(value: object, where: str) -> decimal.Decimal:
    if not isinstance(value, str):
        # A YAML float would arrive already rounded; prices are written as strings.
        raise ValueError(f"{where}: write prices as quoted strings, got {value!r}")
    amount = decimal.Decimal(value)
    if amount < 0:
        raise ValueError(f"{where}: a price cannot be negative")
    return amount


@cache
def load_prices() -> PriceTable:
    raw = yaml.safe_load(
        resources.files("aria_llm_gateway").joinpath("prices.yaml").read_text(encoding="utf-8")
    )
    multipliers = {
        geo: _money(value, f"geo_multipliers.{geo}") for geo, value in raw["geo_multipliers"].items()
    }
    if set(multipliers) != {"global", "us"}:
        raise ValueError(f"geo_multipliers must name global and us, got {sorted(multipliers)}")
    models = {
        model: ModelPrice(
            **{
                field: _money(entry[field], f"{model}.{field}")
                for field in (f.name for f in fields(ModelPrice))
            }
        )
        for model, entry in raw["models"].items()
    }
    return PriceTable(version=str(raw["version"]), geo_multipliers=multipliers, models=models)
