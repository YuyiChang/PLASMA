import multiprocessing
import sys
import traceback

multiprocessing.freeze_support()

exit_code = 0
try:
    from plasma.__main__ import main
    main()
except SystemExit as e:
    exit_code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
except KeyboardInterrupt:
    exit_code = 130
except Exception as e:
    traceback.print_exc()
    print(f"An error occurred: {e}")
    exit_code = 1

# Pause on a desktop double-click (stdin is a real console); never in CI / pipes.
if sys.stdin is not None and sys.stdin.isatty():
    input("Press Enter to exit...")

sys.exit(exit_code)
