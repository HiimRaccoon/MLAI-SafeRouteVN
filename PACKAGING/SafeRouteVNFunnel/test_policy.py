from pathlib import Path
import subprocess

root = Path(__file__).resolve().parent
jdk = Path('C:/Program Files/Android/Android Studio/jbr/bin')
out = root / 'build/policy-tests'
out.mkdir(parents=True, exist_ok=True)
subprocess.run([str(jdk / 'javac.exe'), '-encoding', 'UTF-8', '-d', str(out),
    str(root / 'src/vn/saferoute/driver/FunnelPolicy.java'),
    str(root / 'tests/FunnelPolicyTest.java')], check=True)
subprocess.run([str(jdk / 'java.exe'), '-cp', str(out), 'vn.saferoute.driver.FunnelPolicyTest'], check=True)
