# src/cli.py

import json
import click
from .pipeline import AutoFuzzPipeline
from config.loader import load_config


@click.group()
def cli():
    pass


@cli.command()
@click.option("--config", default=None)
@click.option("--dbc", default=None)
@click.option("--db", default=None)
def register(config, dbc, db):
    cfg = load_config(config)

    if dbc:
        cfg["paths"]["dbc"] = dbc
    if db:
        cfg["paths"]["seed_db"] = db

    count = AutoFuzzPipeline.register_seeds(
        dbc_path=cfg["paths"]["dbc"],
        db_path=cfg["paths"]["seed_db"]
    )
    click.echo(f"[✓] {count} seeds registered.")


@cli.command()
@click.option("--config", default=None)
def list(config):
    cfg = load_config(config)
    db_path = cfg["paths"]["seed_db"]

    seeds = AutoFuzzPipeline.list_seeds(db_path)
    if not seeds:
        click.echo("[!] No seeds found.")
        return

    click.echo(f"[+] {len(seeds)} seeds loaded:")
    click.echo("-" * 60)
    for s in seeds:
        click.echo(
            f"[{s.id:03}] {s.signal_name:<25} "
            f"| msg_id={hex(s.message_id)} | priority={s.priority}"
        )
    click.echo("-" * 60)


def print_monitor_results(results: dict):
    click.echo(click.style("\n=== AutoFuzz Monitor Results ===\n", fg="cyan", bold=True))

    for name, v in results.items():
        monitor = name.upper()
        score_val = v.get("score", 0.0)
        status = v.get("status", "unknown")

        if status == "ok":
            status_text = click.style("OK", fg="green")
        elif status == "timeout":
            status_text = click.style("TIMEOUT", fg="yellow")
        elif status == "crashed":
            status_text = click.style("CRASHED", fg="red")
        elif status == "skipped":
            status_text = click.style("SKIPPED", fg="blue")
        else:
            status_text = click.style("UNKNOWN", fg="white")

        score_text = click.style(f"{score_val:.3f}", fg="cyan")

        click.echo(f"{monitor:<10} | Score: {score_text} | Status: {status_text}")

    click.echo()


@cli.command()
@click.option("--config", default=None)
@click.option("--save-json", is_flag=True)
def run(config, save_json):
    cfg = load_config(config)

    can_iface = None
    if cfg["can"].get("enable", False):
        from .interface.can_interface import CANInterface
        from .interface.multi_bus_interface import MultiBusCANInterface

        bus_configs = cfg["can"].get("buses", {})
        if bus_configs:
            interfaces = {
                logical_bus: CANInterface(
                    channel=bus_cfg["channel"],
                    bustype=bus_cfg.get("bustype", "socketcan"),
                    can_id=int(bus_cfg.get("default_id", cfg["can"]["default_id"]), 16),
                    logical_bus=logical_bus,
                )
                for logical_bus, bus_cfg in bus_configs.items()
                if bus_cfg.get("enable", True)
            }
            can_iface = MultiBusCANInterface(interfaces)
        else:
            can_iface = CANInterface(
                channel=cfg["can"]["channel"],
                can_id=int(cfg["can"]["default_id"], 16),
                logical_bus=cfg["can"].get("logical_bus", cfg["can"]["channel"]),
            )

    pipeline = AutoFuzzPipeline(cfg, can_iface=can_iface)
    results = pipeline.run()

    print_monitor_results(results)

    if save_json:
        with open("fuzz_results.json", "w", encoding="utf-8") as f:
            json.dump(results, f, indent=4)
        click.echo(click.style("[✓] Results saved → fuzz_results.json", fg="green"))


if __name__ == "__main__":
    cli()
