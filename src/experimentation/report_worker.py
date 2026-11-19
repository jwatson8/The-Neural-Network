"""Disabled-by-default scheduler-compatible one-shot reporting entry point."""

import argparse
from src.experimentation.report_cli import main as report_main


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument('--enabled', action='store_true', help='Explicitly permit this one-shot run')
    options, remaining = parser.parse_known_args(argv)
    if not options.enabled:
        print('Experiment report worker disabled; no database access.')
        return 0
    return report_main(remaining)


if __name__ == '__main__':
    raise SystemExit(main())
