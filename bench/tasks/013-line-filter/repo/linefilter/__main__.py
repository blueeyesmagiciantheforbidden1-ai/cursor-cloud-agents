import argparse
import sys

from linefilter.core import filter_lines


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('pattern')
    # missing ignore-case / invert flags
    args = p.parse_args(argv)
    data = sys.stdin.read().splitlines(keepends=True)
    for line in filter_lines(data, args.pattern):
        sys.stdout.write(line)


if __name__ == "__main__":
    main()
