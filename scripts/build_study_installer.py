"""Build a clickable Mac installer. Supply Developer ID and notary profile for distribution."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
from urllib.parse import urlsplit

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--origin', required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--identity')
parser.add_argument('--notary-profile')
args = parser.parse_args()
url = urlsplit(args.origin)
if url.scheme != 'https' or not url.hostname or url.username or args.origin != f'https://{url.netloc}':
    parser.error('Use the HTTPS Study origin without a path.')
if bool(args.identity) != bool(args.notary_profile):
    parser.error('Distribution requires both a Developer ID identity and a notary profile.')
args.output.mkdir(parents=True, exist_ok=True)
app = args.output.resolve() / 'Install Study Connector.app'
if app.exists():
    parser.error('Choose an empty output directory.')
source = '''on run
    set siteName to ORIGIN
    display dialog "Install Study Connector for " & siteName & "? This downloads the connector and its Python dependencies. Terminal will show installation progress. Return to Study when it finishes." buttons {"Cancel", "Install"} default button "Install" with title "Study Connector"
    set scriptPath to POSIX path of (path to resource "install.sh")
    tell application "Terminal"
        activate
        do script "/bin/bash " & quoted form of scriptPath & " " & quoted form of siteName
    end tell
end run
'''.replace('ORIGIN', json.dumps(args.origin))
subprocess.run(['osacompile', '-o', str(app), '-e', source], check=True)
shutil.copy(Path(__file__).with_name('install-study-connector.sh'), app / 'Contents/Resources/install.sh')
subprocess.run(['codesign', '--force', '--options', 'runtime', '--sign', args.identity or '-', str(app)], check=True)
archive = args.output.resolve() / 'Study-Connector-Installer.zip'
subprocess.run(['ditto', '-c', '-k', '--keepParent', str(app), str(archive)], check=True)
if args.notary_profile:
    subprocess.run(['xcrun', 'notarytool', 'submit', str(archive), '--keychain-profile', args.notary_profile, '--wait'], check=True)
    subprocess.run(['xcrun', 'stapler', 'staple', str(app)], check=True)
    subprocess.run(['ditto', '-c', '-k', '--keepParent', str(app), str(archive)], check=True)
    subprocess.run(['spctl', '--assess', '--type', 'execute', str(app)], check=True)
else:
    print('LOCAL PREVIEW ONLY: Developer ID signing and notarization are still required for distribution.')
print(archive)
