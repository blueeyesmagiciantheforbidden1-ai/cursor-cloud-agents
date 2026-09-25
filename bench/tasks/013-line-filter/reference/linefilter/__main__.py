import argparse
import sys

from linefilter.core import filter_lines


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('-i', '--ignore-case', action='store_true')
    p.add_argument('-v', '--invert', action='store_true')
    p.add_argument('pattern')
    args = p.parse_args(argv)
    data = sys.stdin.read().splitlines(keepends=True)
    for line in filter_lines(
        data, args.pattern, ignore_case=args.ignore_case, invert=args.invert
    ):
        sys.stdout.write(line)


if __name__ == "__main__":
    main()
