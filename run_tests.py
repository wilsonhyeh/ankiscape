import compileall
import glob
import os
import sys
import unittest


def main() -> int:
    # Ensure project root is importable for tests
    root = os.path.dirname(os.path.abspath(__file__))
    if root not in sys.path:
        sys.path.insert(0, root)

    # Compile check over shipped + dev Python first: unittest discovery only
    # imports tests/, so a syntax error anywhere else would slip through.
    # NEVER walk "." recursively: .dev/, artifacts/ and dist/ contain
    # extracted add-on copies and would make this check walk forever.
    for subdir in ("dev", "evolved", "scripts", "server", "tests"):
        target = os.path.join(root, subdir)
        if not os.path.isdir(target):
            continue
        if not compileall.compile_dir(target, maxlevels=12, quiet=1):
            print(f"run_tests: compile failed under {subdir}", file=sys.stderr)
            return 1
    for path in sorted(glob.glob(os.path.join(root, "*.py"))):
        if not compileall.compile_file(path, quiet=1):
            print(f"run_tests: compile failed: {path}", file=sys.stderr)
            return 1

    # Discover and run tests under tests/
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=os.path.join(root, "tests"), pattern="test_*.py")

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
