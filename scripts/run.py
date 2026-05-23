"""CLI entry point for polyfon."""
import asyncio
import click
from rich.console import Console

from polyfon.config import settings
from polyfon.database import init_db
from polyfon.collector.orchestrator import CollectionOrchestrator
from polyfon.strategies.base import StrategyRegistry
from polyfon.execution.engine import ExecutionEngine

console = Console()


def _parse_coins(coins_str: str) -> list[str]:
    return [c.strip().upper() for c in coins_str.split(",") if c.strip()]


def _parse_strategy_params(params: tuple[str, ...]) -> dict:
    """Parse ``key=value`` strings into a typed dict for strategy kwargs."""
    kwargs = {}
    for p in params:
        if "=" not in p:
            continue
        key, raw = p.split("=", 1)
        key = key.strip().lstrip("-").replace("-", "_")
        raw = raw.strip()
        # Type inference: int → float → bool → str
        try:
            kwargs[key] = int(raw)
        except ValueError:
            try:
                kwargs[key] = float(raw)
            except ValueError:
                if raw.lower() in ("true", "false"):
                    kwargs[key] = raw.lower() == "true"
                else:
                    kwargs[key] = raw
    return kwargs


@click.group()
def cli():
    """Polyfon — 5-minute crypto prediction market trading system."""
    pass


@cli.command()
@click.option("--coins", default=None, help="Comma-separated coin list (default from env).")
def collect(coins: str | None) -> None:
    """Run collection mode: gather market data into DB."""
    coin_list = _parse_coins(coins) if coins else settings.coin_list
    console.print(f"[bold green]Starting collection for coins: {', '.join(coin_list)}[/]")

    async def _run() -> None:
        await init_db()
        orch = CollectionOrchestrator(coins=coin_list)
        try:
            await orch.run()
        finally:
            console.print("[yellow]Shutting down collector...[/]")
            await orch.stop()

    asyncio.run(_run())


@cli.command()
@click.option("--strategy", required=True, help="Strategy name to run (e.g., SLA).")
@click.option("--coins", default=None, help="Comma-separated coin list filter.")
@click.option("--collect", "do_collect", is_flag=True, help="Also run data collection in parallel.")
@click.option("--param", "params", multiple=True, help="Strategy parameter as key=value (repeatable).")
def dry(strategy: str, coins: str | None, do_collect: bool, params: tuple[str, ...]) -> None:
    """Run dry mode: simulate strategy on historical DB data."""
    coin_list = _parse_coins(coins) if coins else settings.coin_list
    strat_class = StrategyRegistry.get(strategy)
    if strat_class is None:
        available = ", ".join(StrategyRegistry.list_strategies())
        console.print(f"[bold red]Unknown strategy '{strategy}'. Available: {available}[/]")
        raise click.BadParameter(f"strategy must be one of: {available}")

    strat_kwargs = _parse_strategy_params(params)
    console.print(f"[bold green]Dry mode: strategy={strategy}, coins={', '.join(coin_list)}[/]")
    if strat_kwargs:
        console.print(f"  params: {strat_kwargs}")

    async def _run() -> None:
        await init_db()
        orch: CollectionOrchestrator | None = None
        if do_collect:
            orch = CollectionOrchestrator(coins=coin_list)
            orch.spot.start()

        engine = ExecutionEngine(mode="dry", strategy=strat_class(**strat_kwargs), coins=coin_list)
        try:
            await engine.run_dry()
        finally:
            await engine.stop()
            if orch:
                await orch.stop()

    asyncio.run(_run())


@cli.command()
@click.option("--strategy", required=True, help="Strategy name to run.")
@click.option("--coins", default=None, help="Comma-separated coin list.")
@click.option("--collect", is_flag=True, help="Also run collection in parallel.")
@click.option("--param", "params", multiple=True, help="Strategy parameter as key=value (repeatable).")
def shadow(strategy: str, coins: str | None, collect: bool, params: tuple[str, ...]) -> None:
    """Run shadow mode: real-time simulated trading."""
    coin_list = _parse_coins(coins) if coins else settings.coin_list
    strat_class = StrategyRegistry.get(strategy)
    if strat_class is None:
        available = ", ".join(StrategyRegistry.list_strategies())
        console.print(f"[bold red]Unknown strategy '{strategy}'. Available: {available}[/]")
        raise click.BadParameter(f"strategy must be one of: {available}")

    strat_kwargs = _parse_strategy_params(params)
    console.print(f"[bold green]Shadow mode: strategy={strategy}, coins={', '.join(coin_list)}[/]")
    if strat_kwargs:
        console.print(f"  params: {strat_kwargs}")

    async def _run() -> None:
        await init_db()
        orch: CollectionOrchestrator | None = None
        if collect:
            orch = CollectionOrchestrator(coins=coin_list)
            await orch.run()

        engine = ExecutionEngine(mode="shadow", strategy=strat_class(**strat_kwargs), coins=coin_list)
        try:
            await engine.run_shadow()
        except KeyboardInterrupt:
            console.print("[yellow]Shutting down shadow...[/]")
        finally:
            await engine.stop()
            if orch:
                await orch.stop()

    asyncio.run(_run())


@cli.command()
def list_strategies() -> None:
    """List available strategies."""
    names = StrategyRegistry.list_strategies()
    console.print("[bold cyan]Available strategies:[/]")
    for n in names:
        console.print(f"  - {n}")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
