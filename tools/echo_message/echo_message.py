#!/usr/bin/env python3
import argparse
import os
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Echo a message with optional formatting."
    )

    # Static (can still be passed via JSON if needed, but often pre-set)
    parser.add_argument(
        "--uppercase",
        action="store_true",
        help="Convert the message to uppercase."
    )

    # Dynamic params (from JSON -> command line)
    parser.add_argument(
        "message",
        type=str,
        help="The message to echo."
    )

    parser.add_argument(
        "--repeat",
        type=int,
        metavar="N",
        default=1,
        help="Number of times to repeat the message."
    )

    args = parser.parse_args(argv)

    output_lines = []
    for _ in range(args.repeat):
        msg = args.message.upper() if args.uppercase else args.message
        output_lines.append(msg)

    for line in output_lines:
        print(line)

    # Example structured JSON output (optional)
    return {
        "count": len(output_lines),
        "uppercase": args.uppercase,
        # Include the current value of DUMMY_VAR (or None if not set)
        "DUMMY_KEY": os.environ.get("DUMMY_KEY"),
    }


if __name__ == "__main__":
    try:
        meta = main()
        if meta is not None:
            import json

            print(json.dumps(meta), file=sys.stderr)
    except Exception as e:
        sys.stderr.write(f"Error: {e}\n")
        sys.exit(1)
