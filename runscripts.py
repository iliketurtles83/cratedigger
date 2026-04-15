import subprocess
import sys

# scripts to run from the command line

folder_names = [
    'african', 'blues', 'brazil', 'classical'
]


scripts = [
    {
        'name': '01_tag.py',
        'dry_args': ['--no-bpm', '--no-mb'],
        'live_args': [],
    },
    {
        'name': '02_rename.py',
        'dry_args': [],
        'live_args': [],
    },
    {
        'name': '03_folders.py',
        'dry_args': [],
        'live_args': [],
    },
    {
        'name': '04_move.py',
        'dry_args': ['--restructure'],
        'live_args': ['--restructure'],
    },
]

def run_step(script_name: str, args: list[str]) -> subprocess.CompletedProcess:
    """Run a step script with the given arguments. Returns the result."""
    result = subprocess.run(
        [sys.executable, script_name] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return result

for folder in folder_names:
    print(f"\nProcessing folder: {folder}")

    for script in scripts:
        script_name = script['name']
        dry_extra_args = script['dry_args']
        live_extra_args = script['live_args']

        print(f"\n--- {script_name} ---")
        args = ['--dry-run', '--folder', folder] + dry_extra_args

        # Run in dry-run mode
        result = run_step(script_name, args)
        print(result.stdout)

        # Prompt user to run live
        if input(f"Run {script_name} live? (y/n): ").lower() == 'y':
            live_args = ['--no-dry-run', '--folder', folder] + live_extra_args
            result = run_step(script_name, live_args)
            print(result.stdout)
        else:
            print(f"Skipped {script_name}")