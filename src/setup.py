"""Manage tracked items in the database without editing the seed catalog."""

import argparse
import json
import sqlite3
import sys

from dotenv import load_dotenv

import db


def _item_id(value):
    try:
        item_id = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("item ID must be a positive integer") from exc
    if item_id <= 0:
        raise argparse.ArgumentTypeError("item ID must be a positive integer")
    return item_id


def _parser():
    parser = argparse.ArgumentParser(description="Manage RainCheck tracked items")
    commands = parser.add_subparsers(dest="command", required=True)

    listing = commands.add_parser("list", help="show active item configurations")
    listing.add_argument("--all", action="store_true", dest="include_inactive")

    adding = commands.add_parser("add", help="add or reactivate a tracked item")
    adding.add_argument("--name", required=True)
    adding.add_argument("--brand", default="Rick Owens")
    adding.add_argument("--keyword", action="append", required=True)
    adding.add_argument("--exclude-keyword", action="append", default=[])
    adding.add_argument("--search-query")
    adding.add_argument("--manual-price")

    updating = commands.add_parser("update", help="replace selected item fields")
    updating.add_argument("id", type=_item_id)
    updating.add_argument("--keyword", action="append")
    updating.add_argument("--exclude-keyword", action="append")
    updating.add_argument("--clear-exclude-keywords", action="store_true")
    queries = updating.add_mutually_exclusive_group()
    queries.add_argument("--search-query")
    queries.add_argument("--clear-search-query", action="store_true")
    prices = updating.add_mutually_exclusive_group()
    prices.add_argument("--manual-price")
    prices.add_argument("--clear-manual-price", action="store_true")

    removing = commands.add_parser("remove", help="deactivate an item while keeping its history")
    removing.add_argument("id", type=_item_id)
    adopting = commands.add_parser("adopt", help="give one item to catalog management")
    adopting.add_argument("id", type=_item_id)
    return parser


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "update":
        if args.exclude_keyword is not None and args.clear_exclude_keywords:
            parser.error("--exclude-keyword and --clear-exclude-keywords cannot be combined")
        if not any((args.keyword is not None, args.exclude_keyword is not None,
                    args.clear_exclude_keywords, args.search_query is not None,
                    args.clear_search_query, args.manual_price is not None,
                    args.clear_manual_price)):
            parser.error("update requires at least one change flag")

    # Help and syntax errors finish before any database or environment access.
    load_dotenv()
    try:
        if args.command == "list":
            items = db.list_items(args.include_inactive)
            if not items:
                print("No items found.")
            for item in items:
                status = "active" if item["active"] else "inactive"
                print(f'{item["id"]}: {item["brand"]} / {item["name"]} ({status})')
                print(f'  source: {item["config_source"]}')
                print(f'  keywords: {", ".join(item["keywords"])}')
                print(f'  exclude keywords: {", ".join(item["exclude_keywords"])}')
                print(f'  search query: {item["search_query"]}')
                print(f'  manual price USD: {item["manual_price_usd"]}')
        elif args.command == "add":
            item_id = db.add_item(
                args.name, args.keyword, brand=args.brand,
                exclude_keywords=args.exclude_keyword,
                search_query=args.search_query, manual_price_usd=args.manual_price,
            )
            print(f"Added item {item_id}.")
        elif args.command == "update":
            changes = {}
            if args.keyword is not None:
                changes["keywords"] = args.keyword
            if args.exclude_keyword is not None or args.clear_exclude_keywords:
                changes["exclude_keywords"] = args.exclude_keyword or []
            if args.search_query is not None or args.clear_search_query:
                changes["search_query"] = args.search_query
            if args.manual_price is not None or args.clear_manual_price:
                changes["manual_price_usd"] = args.manual_price
            db.update_item(args.id, **changes)
            print(f"Updated item {args.id}.")
        elif args.command == "remove":
            changed = db.deactivate_item(args.id)
            print(f"Removed item {args.id}." if changed else f"Item {args.id} is already inactive.")
        elif args.command == "adopt":
            db.adopt_catalog_item(args.id)
            print(f"Adopted item {args.id} from catalog.json.")
    except (ValueError, db.SchemaVersionError, sqlite3.Error, OSError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
