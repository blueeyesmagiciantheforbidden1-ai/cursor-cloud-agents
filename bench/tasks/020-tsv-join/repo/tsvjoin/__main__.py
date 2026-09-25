import argparse
from pathlib import Path

from tsvjoin.join import join_tables
from tsvjoin.table import dump_tsv, load_tsv


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('left')
    p.add_argument('right')
    p.add_argument('--key', required=True)
    # missing --how
    args = p.parse_args(argv)
    lh, lr = load_tsv(Path(args.left).read_text(encoding='utf-8'))
    rh, rr = load_tsv(Path(args.right).read_text(encoding='utf-8'))
    headers, rows = join_tables(lh, lr, rh, rr, args.key)
    print(dump_tsv(headers, rows))


if __name__ == "__main__":
    main()
