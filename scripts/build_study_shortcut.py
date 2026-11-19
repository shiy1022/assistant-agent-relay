"""Build a shareable macOS shortcut using Apple's Shortcuts signing service.

Requires macOS with an Apple Account available to `shortcuts sign`. This is a
Shortcuts signature, not a Developer ID application signature. The artifact must
be tested after browser download, including the quarantine attribute.
"""
import argparse
import plistlib
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def workflow():
    return {
        'WFWorkflowClientVersion': '3036.0.4.2',
        'WFWorkflowMinimumClientVersion': 900,
        'WFWorkflowName': 'Study Connect',
        'WFWorkflowIcon': {'WFWorkflowIconStartColor': 4282601983, 'WFWorkflowIconGlyphNumber': 59511},
        'WFWorkflowTypes': [],
        'WFWorkflowInputContentItemClasses': [],
        'WFWorkflowActions': [{
            'WFWorkflowActionIdentifier': 'is.workflow.actions.runshellscript',
            'WFWorkflowActionParameters': {
                'Script': (ROOT / 'scripts/setup-study-shortcut.sh').read_text(),
                'Shell': '/bin/bash', 'InputMode': 'to stdin',
            },
        }],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'bench/Connect Study - Muse.shortcut')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / 'unsigned.shortcut'
        source.write_bytes(plistlib.dumps(workflow()))
        subprocess.run(['shortcuts', 'sign', '--mode', 'anyone', '--input', str(source), '--output', str(args.output)], check=True)
    print(f'Created {args.output}')


if __name__ == '__main__':
    main()
