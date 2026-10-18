"""Build the focused Driver APK with installed Android SDK tools; no Gradle downloads."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parent
SDK = Path(os.environ.get('ANDROID_HOME', str(Path.home() / 'AppData/Local/Android/Sdk')))
JDK = Path('C:/Program Files/Android/Android Studio/jbr')
TOOLS = SDK / 'build-tools/36.0.0'
ANDROID = SDK / 'platforms/android-36/android.jar'
PRIVATE = ROOT.parents[1] / 'SafeRouteVN_Driver_Accounts/signing-funnel'
BUILD = ROOT / 'build'
DIST = ROOT / 'dist'
env = dict(os.environ, JAVA_HOME=str(JDK))
env['PATH'] = str(JDK / 'bin') + os.pathsep + env['PATH']

def run(*args):
    subprocess.run([str(a) for a in args], cwd=ROOT, env=env, check=True)

for directory in (BUILD / 'classes', BUILD / 'dex', DIST, PRIVATE): directory.mkdir(parents=True, exist_ok=True)
key = PRIVATE / 'driver-release.jks'
password_file = PRIVATE / 'password.txt'
if not key.exists():
    password = secrets.token_urlsafe(32)
    password_file.write_text(password, encoding='utf-8')
    env['SAFEROUTE_SIGNING_PASSWORD'] = password
    run(JDK / 'bin/keytool.exe', '-genkeypair', '-keystore', key, '-storepass:env', 'SAFEROUTE_SIGNING_PASSWORD',
        '-keypass:env', 'SAFEROUTE_SIGNING_PASSWORD', '-alias', 'saferoute-driver', '-keyalg', 'RSA', '-keysize', '3072',
        '-validity', '10000', '-dname', 'CN=SafeRouteVN Driver, O=SafeRouteVN, C=VN')
env['SAFEROUTE_SIGNING_PASSWORD'] = password_file.read_text(encoding='utf-8').strip()
run(TOOLS / 'aapt.exe', 'package', '-f', '-M', ROOT / 'AndroidManifest.xml', '-S', ROOT / 'res', '-I', ANDROID, '-F', BUILD / 'resources.apk')
sources = list((ROOT / 'src').rglob('*.java'))
run(JDK / 'bin/javac.exe', '-encoding', 'UTF-8', '-source', '8', '-target', '8', '-bootclasspath', str(ANDROID) + os.pathsep + str(TOOLS / 'core-lambda-stubs.jar'), '-d', BUILD / 'classes', *sources)
classes = list((BUILD / 'classes').rglob('*.class'))
run(TOOLS / 'd8.bat', '--lib', ANDROID, '--min-api', '23', '--output', BUILD / 'dex', *classes)
unsigned = BUILD / 'unsigned.apk'
shutil.copyfile(BUILD / 'resources.apk', unsigned)
with zipfile.ZipFile(unsigned, 'a', compression=zipfile.ZIP_DEFLATED) as apk:
    apk.write(BUILD / 'dex/classes.dex', 'classes.dex')
run(TOOLS / 'zipalign.exe', '-f', '-p', '4', unsigned, BUILD / 'aligned.apk')
output = DIST / 'SafeRouteVN-Driver-Funnel.apk'
run(TOOLS / 'apksigner.bat', 'sign', '--ks', key, '--ks-key-alias', 'saferoute-driver', '--ks-pass', 'env:SAFEROUTE_SIGNING_PASSWORD',
    '--key-pass', 'env:SAFEROUTE_SIGNING_PASSWORD', '--out', output, BUILD / 'aligned.apk')
run(TOOLS / 'apksigner.bat', 'verify', '--verbose', '--print-certs', output)
run(TOOLS / 'aapt.exe', 'dump', 'badging', output)
digest = hashlib.sha256(output.read_bytes()).hexdigest()
(DIST / 'SHA256SUMS.txt').write_text(f'{digest}  {output.name}\n', encoding='utf-8')
print(json.dumps({'apk':str(output), 'sha256':digest, 'bytes':output.stat().st_size}))
