import os
import sys
import subprocess

print("=== Python Process Environment ===")
print(f"Python executable: {sys.executable}")
print(f"CWD: {os.getcwd()}")
print(f"USER: {os.environ.get('USER', 'Not set')}")
print(f"USERNAME: {os.environ.get('USERNAME', 'Not set')}")
print(f"TEMP: {os.environ.get('TEMP', 'Not set')}")
print(f"TMP: {os.environ.get('TMP', 'Not set')}")
print(f"PATH length: {len(os.environ.get('PATH', ''))}")
print(f"COMSPEC: {os.environ.get('COMSPEC', 'Not set')}")
print(f"SYSTEMROOT: {os.environ.get('SYSTEMROOT', 'Not set')}")
print()

# Test launching Chrome via different methods
chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
user_data_dir = os.path.join(os.environ.get('TEMP'), 'test_chrome_env')
cmd = f'start "" "{chrome_path}" --remote-debugging-port=9222 --user-data-dir={user_data_dir} --no-first-run --disable-features=ProcessSingleton'

print("=== Testing subprocess.run with shell=True ===")
print(f"Command: {cmd}")
result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
print(f"Return code: {result.returncode}")
print(f"Stdout: {result.stdout}")
print(f"Stderr: {result.stderr}")
print()

print("=== Testing os.startfile with bat ===")
bat_path = os.path.join(os.environ.get('TEMP'), 'test_chrome.bat')
with open(bat_path, 'w') as f:
    f.write(f"@echo off\n{cmd}\n")
print(f"Bat file: {bat_path}")
try:
    os.startfile(bat_path)
    print("os.startfile succeeded")
except Exception as e:
    print(f"os.startfile failed: {e}")
