import sys


def should_drop(line: str) -> bool:
    return "?logs=container" in line and "__sign=" in line


def main() -> None:
    for line in sys.stdin:
        if should_drop(line):
            continue
        sys.stdout.write(line)
        sys.stdout.flush()


if __name__ == "__main__":
    main()
