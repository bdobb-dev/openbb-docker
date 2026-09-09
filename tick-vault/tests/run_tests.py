import sys
import os
import traceback

# Add tick-vault to sys.path so tick_vault imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import all test modules
test_dir = os.path.dirname(os.path.abspath(__file__))
test_modules = []
for filename in sorted(os.listdir(test_dir)):
    if filename.startswith('test_') and filename.endswith('.py'):
        module_name = filename[:-3]
        try:
            module = __import__(module_name)
            test_modules.append(module)
        except ImportError as e:
            print(f"FAIL: Failed to import {module_name}: {e}")
            traceback.print_exc()
            sys.exit(1)

# Run all test_* functions in definition order
passed = 0
failed = 0

for module in test_modules:
    for attr_name in dir(module):
        if attr_name.startswith('test_'):
            test_func = getattr(module, attr_name)
            if callable(test_func):
                try:
                    test_func()
                    print(f"PASS: {module.__name__}.{attr_name}")
                    passed += 1
                except Exception as e:
                    print(f"FAIL: {module.__name__}.{attr_name}")
                    traceback.print_exc()
                    failed += 1

print(f"\n{passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
