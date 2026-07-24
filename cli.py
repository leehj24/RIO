import argparse
import json
from dotenv import load_dotenv

from config import Settings
from toss_client import TossClient
from strategy.symbol_seed_import import (
    SymbolSeedImportError,
    merge_seed_into_symbols,
    preview_seed_merge,
)
from strategy.toss_symbol_verifier import verify_execution_symbols, write_verification_report


def pretty(x):
    print(json.dumps(x, ensure_ascii=False, indent=2))


def make_client():
    settings = Settings()
    return TossClient(
        base_url=settings.toss_base_url,
        access_token=None,
        default_account_seq=settings.toss_account_seq,
        dry_run=settings.dry_run,
        auto_account=settings.toss_auto_account,
    )


def main():
    load_dotenv()
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("token")
    sub.add_parser("accounts")

    p = sub.add_parser("buying-power")
    p.add_argument("--account")

    p = sub.add_parser("positions")
    p.add_argument("--account")
    p.add_argument("--profit", action="store_true")

    p = sub.add_parser("stocks")
    p.add_argument("symbols")

    p = sub.add_parser("prices")
    p.add_argument("symbols")

    p = sub.add_parser("candles")
    p.add_argument("symbol")
    p.add_argument("--interval", default="1d")
    p.add_argument("--count", type=int, default=120)

    p = sub.add_parser("order")
    p.add_argument("--account")
    p.add_argument("--symbol", required=True)
    p.add_argument("--side", required=True, choices=["BUY", "SELL"])
    p.add_argument("--order-type", required=True, choices=["LIMIT", "MARKET"])
    p.add_argument("--quantity")
    p.add_argument("--order-amount")
    p.add_argument("--price")
    p.add_argument("--client-order-id")
    p.add_argument("--confirm-high-value-order", action="store_true")
    p.add_argument("--time-in-force")

    p = sub.add_parser("modify")
    p.add_argument("order_id")
    p.add_argument("--account")
    p.add_argument("--order-type", required=True, choices=["LIMIT", "MARKET"])
    p.add_argument("--quantity")
    p.add_argument("--price")
    p.add_argument("--confirm-high-value-order", action="store_true")

    p = sub.add_parser("cancel")
    p.add_argument("order_id")
    p.add_argument("--account")

    p = sub.add_parser("import-global-seed")
    p.add_argument("--seed", default="global_theme_universe_seed.csv")
    p.add_argument("--symbols", default="data/symbols.csv")
    p.add_argument("--apply", action="store_true", help="Actually append the validated, disabled candidates")

    p = sub.add_parser("verify-execution-symbols")
    p.add_argument("--symbols", default="data/symbols.csv")
    p.add_argument("--output", default="data/toss_execution_verification.csv")
    p.add_argument("--batch-size", type=int, default=200)

    args = parser.parse_args()

    # Seed import is deliberately independent of the broker client.  Imported
    # global rows remain disabled research candidates and cannot place orders.
    if args.cmd == "import-global-seed":
        try:
            result = (
                merge_seed_into_symbols(seed_path=args.seed, symbols_path=args.symbols)
                if args.apply
                else preview_seed_merge(seed_path=args.seed, symbols_path=args.symbols)
            )
            pretty({"applied": bool(args.apply), **result})
        except SymbolSeedImportError as exc:
            pretty({"ok": False, "error": "symbol_seed_import_error", "message": str(exc)})
        return

    client = make_client()

    if args.cmd == "verify-execution-symbols":
        rows = verify_execution_symbols(
            client,
            symbols_path=args.symbols,
            batch_size=args.batch_size,
        )
        output = write_verification_report(rows, args.output)
        verified = sum(1 for row in rows if row["verification_status"] == "verified")
        pretty({
            "output": str(output),
            "candidate_count": len(rows),
            "verified_count": verified,
            "unverified_count": len(rows) - verified,
            "note": "This report does not enable symbols or create orders.",
        })
        return

    if args.cmd == "token":
        pretty(client.issue_token())

    elif args.cmd == "accounts":
        pretty(client.accounts())

    elif args.cmd == "buying-power":
        pretty(client.buying_power(account_seq=args.account))

    elif args.cmd == "positions":
        data = client.positions(account_seq=args.account)
        if args.profit:
            result = data.get("result", data)
            if isinstance(result, list):
                data["_with_estimated_profit"] = [client.estimate_profit_fields(x) for x in result]
        pretty(data)

    elif args.cmd == "stocks":
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        pretty(client.stocks(symbols))

    elif args.cmd == "prices":
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        pretty(client.prices(symbols))

    elif args.cmd == "candles":
        pretty(client.candles(args.symbol, interval=args.interval, count=args.count))

    elif args.cmd == "order":
        pretty(client.create_order(
            account_seq=args.account,
            symbol=args.symbol,
            side=args.side,
            order_type=args.order_type,
            quantity=args.quantity,
            order_amount=args.order_amount,
            price=args.price,
            client_order_id=args.client_order_id,
            confirm_high_value_order=args.confirm_high_value_order,
            time_in_force=args.time_in_force,
        ))

    elif args.cmd == "modify":
        pretty(client.modify_order(
            account_seq=args.account,
            order_id=args.order_id,
            order_type=args.order_type,
            quantity=args.quantity,
            price=args.price,
            confirm_high_value_order=args.confirm_high_value_order,
        ))

    elif args.cmd == "cancel":
        pretty(client.cancel_order(account_seq=args.account, order_id=args.order_id))


if __name__ == "__main__":
    main()
