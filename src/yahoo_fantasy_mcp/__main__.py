import sys


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        from .auth import interactive_login

        interactive_login()
        return
    from .server import run

    run()


if __name__ == "__main__":
    main()
